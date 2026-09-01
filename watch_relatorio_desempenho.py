"""
Watcher: relatório de desempenho chega no Gmail (caixa "aws",
awstendenciaenergiatendenciaen@gmail.com) → extrai UCs do anexo PDF + mês/ano
do assunto do email → upsert em `relatorios_recebidos` (Supabase).

Validado em 2026-08-10 contra emails reais da caixa. Assunto sempre no
formato "[Fwd:/FW:] Relatório de Desempenho Mercado Livre <Mês>/<Ano> |
<CLIENTE>" (ex.: "Relatório de Desempenho Mercado Livre Janeiro/2026 | SPR
INDUSTRIA..."), vindo de 2 remetentes possíveis: a própria conta aws
(encaminhamento manual) ou relatoriosci@tendenciaenergia.com.br (forward
automático interno) — ambos com o PDF anexado. Mês/ano vem do assunto
(mais confiável — o texto do PDF pode conter outras datas de referência que
não são o período do relatório, ver docstring de extractor.py).

Credenciais Gmail próprias desta conta em email/client_secret.json e
email/token.json (gerado no primeiro login) — não usa as da conta em
simple/email/ (conta diferente).

Uso:
  python3 watch_relatorio_desempenho.py --dry-run
  python3 watch_relatorio_desempenho.py --max 20
  python3 watch_relatorio_desempenho.py              # uso via cron
"""

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "email"))

from gmail_client import download_attachments, get_message, get_service, list_messages  # noqa: E402

from extractor import extract_mes_ano, extract_report  # noqa: E402
from supabase_client import select, upsert  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_DIR = os.path.join(BASE_DIR, "email")
DOWNLOAD_DIR = os.path.join(EMAIL_DIR, "downloads")
STATE_FILE = os.path.join(DOWNLOAD_DIR, "processed_relatorio.json")

QUERY = 'subject:"Relatório de Desempenho Mercado Livre" has:attachment'

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watch_relatorio_desempenho")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _only_digits(s):
    # lstrip pq o PDF às vezes zero-preenche o código num campo de largura
    # fixa (ex.: "000185456903195" pro cadastro "185456903195") — sem isso
    # o match "só-dígitos" nunca bate (CARTROM/CHAMPAGNAT/FLORAIS, 2026-08-27).
    return re.sub(r"\D", "", s).lstrip("0") or "0"


NOME_STOP_WORDS = {
    "DE", "DA", "DO", "DOS", "DAS", "E", "LTDA", "EIRELI", "LTD", "SA", "S",
    "A", "ME", "EPP", "MEI",
}


def _tokens_nome(nome):
    if not nome:
        return frozenset()
    s = unicodedata.normalize("NFKD", nome.upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[.\-,/&]", " ", s)
    return frozenset(w for w in s.split() if w not in NOME_STOP_WORDS)


def match_por_nome(nome, nomes_idx, min_score=0.8):
    """Fallback final: casa por razão social quando nem código exato, nem
    parte de composto, nem só-dígitos bateram. Existe porque a distribuidora
    às vezes renumera a conta/UC do cliente e o PDF passa a trazer um código
    totalmente diferente do cadastrado em `ucs_gestor` (não é diferença de
    formatação — é outro número mesmo) — mas o nome do cliente continua
    igual.

    Score = contenção do nome CADASTRADO (lado curto, ex. "BLUE LOGISTICA")
    dentro do nome da LINHA do PDF (ex. "BLUE LOGISTICA INTEGRADA EIRE") —
    mesma convenção usada no fallback por CNPJ. Antes era Jaccard simétrico
    (interseção / MAIOR dos dois lados), que MATEMATICAMENTE nunca passava
    de 0.6 quando o cadastro tinha só 2 tokens e a linha do PDF trazia o
    nome completo/razão social (4+ tokens) — bug real descoberto
    2026-09-01 (BLUE LOGISTICA, B CIRILO ALBINO, e outros: nome cadastrado
    abreviado 100% contido na linha do PDF, mas marcado "sem match" e
    ficando pendente por engano). Score mínimo mais alto (0.8, era 0.6)
    porque a nova fórmula sempre dá 1.0 pra match verdadeiro — ainda
    protege contra homônimo parcial."""
    alvo = _tokens_nome(nome)
    if not alvo:
        return None, 0.0
    melhor_uc, melhor_score = None, 0.0
    for tk, uc in nomes_idx:
        if not tk:
            continue
        score = len(alvo & tk) / len(tk)
        if score > melhor_score:
            melhor_score, melhor_uc = score, uc
    if melhor_score >= min_score:
        return melhor_uc, melhor_score
    return None, melhor_score


def match_por_nome_grupo(nome, grupos_por_nome, min_score=0.75):
    """Fallback de último recurso, em cima do nome do CLIENTE tirado do
    ASSUNTO do email (não da linha do PDF). Existe porque cliente com várias
    UCs (filial/unidade) às vezes tem cada linha do relatório identificada
    pelo nome da FILIAL (ex.: "AV SAUDADE", "EINHELL"), não pela razão
    social cadastrada ("ANCORA GROUP") — `match_por_nome` falha nesse caso
    porque compara contra o nome da linha, não o do assunto.

    Sem CNPJ por UC em `ucs_gestor` pra saber qual linha é qual filial, não
    dá pra saber qual UC exata da empresa corresponde a qual linha — então
    credita TODAS as UCs cadastradas daquela empresa como recebidas nesse
    mês. Pode super-creditar (empresa manda relatório de só 1 filial mas
    creditamos todas) — troca-off aceito (Leo, 2026-08-20): preferir isso a
    marcar como pendente um cliente que claramente mandou o relatório
    (assunto do email prova isso).

    Score = contenção do nome cadastrado no assunto (mesma correção de
    2026-09-01 do `match_por_nome` acima — Jaccard simétrico antigo nunca
    passava quando o assunto trazia razão social completa e o cadastro só
    tinha apelido curto, ex. "AABB SE"/"ASSOCIACAO CRISTA MOCOS SP")."""
    alvo = _tokens_nome(nome)
    if not alvo:
        return None, 0.0
    melhor_tk, melhor_score = None, 0.0
    for tk in grupos_por_nome:
        if not tk:
            continue
        score = len(alvo & tk) / len(tk)
        if score > melhor_score:
            melhor_score, melhor_tk = score, tk
    if melhor_score >= min_score:
        return grupos_por_nome[melhor_tk], melhor_score
    return None, melhor_score


SUBJECT_CLIENTE_PATTERN = re.compile(r"\|\s*(.+?)\s*$")


def extract_subject_cliente(subject):
    m = SUBJECT_CLIENTE_PATTERN.search(subject or "")
    return m.group(1) if m else None


def known_uc_codes():
    """Retorna (set com códigos exatos, dict só-dígitos -> canônico, dict
    parte-de-código-composto -> canônico, lista (tokens-do-nome, canônico)
    pra fallback por razão social).

    3 problemas resolvidos sem mudar o código canônico gravado (sempre usa
    o que já está em `ucs_gestor`):
    - Pontuação diferente entre PDF e planilha (ex.: "558.598.053-10" no PDF
      vs "55859805310" em `ucs_gestor`) → fallback só-dígitos.
    - ~106 UCs em `ucs_gestor` têm código composto "A/B" (2 identificadores
      pro mesmo ponto — às vezes "A/A" repetido, às vezes 2 sistemas
      diferentes), mas o PDF só traz 1 dos 2 lados → fallback por parte.
    - Distribuidora renumerou a conta e o PDF traz um código diferente do
      cadastrado (confirmado em casos reais: TREVO, GAUCHA DE PESCA, SCAPOL
      — 2026-08-20) → fallback por razão social (`match_por_nome`).
    - Cliente com várias UCs/filiais tem cada linha identificada pelo nome
      da FILIAL, não da empresa (confirmado: ANCORA GROUP — linhas "AV
      SAUDADE"/"EINHELL", cadastro só tem "ANCORA GROUP" — 2026-08-20) →
      último fallback, por nome do cliente tirado do ASSUNTO do email
      (`match_por_nome_grupo`), credita todas as UCs da empresa."""
    rows = select("ucs_gestor", filters={"ativo": "eq.true"}, select_cols="uc_codigo,cliente_nome")
    exatas = {row["uc_codigo"] for row in rows}
    por_digitos = {}
    por_parte = {}
    nomes_idx = []
    grupos_por_nome = {}
    for row in rows:
        code = row["uc_codigo"]
        d = _only_digits(code)
        if d:
            por_digitos.setdefault(d, code)
        if "/" in code:
            for parte in code.split("/"):
                parte = parte.strip()
                if not parte:
                    continue
                por_parte.setdefault(parte, code)
                dp = _only_digits(parte)
                if dp:
                    por_digitos.setdefault(dp, code)
        tk = _tokens_nome(row.get("cliente_nome"))
        nomes_idx.append((tk, code))
        grupos_por_nome.setdefault(tk, []).append(code)
    return exatas, por_digitos, por_parte, nomes_idx, grupos_por_nome


def run(max_results=50, dry_run=False):
    service = get_service(base_dir=EMAIL_DIR)
    state = load_state()
    conhecidas, conhecidas_por_digitos, conhecidas_por_parte, conhecidas_por_nome, conhecidas_por_grupo = (
        known_uc_codes() if not dry_run else (None, None, None, None, None)
    )

    messages = list_messages(service, query=QUERY, max_results=max_results)
    log.info("%d mensagens encontradas pra query: %s", len(messages), QUERY)

    novos = ucs_ok = ucs_desconhecidas = ucs_via_nome = ucs_via_assunto = falha = 0

    for m in messages:
        msg_id = m["id"]
        if state.get(msg_id, {}).get("status") == "ok":
            continue

        novos += 1
        msg = get_message(service, msg_id)
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("Subject", "")

        saved = download_attachments(service, msg_id, msg["payload"], DOWNLOAD_DIR, subject)
        if not saved:
            log.warning("msg=%s assunto=%r sem anexo — pulando", msg_id, subject)
            state[msg_id] = {"status": "skipped_no_attachment", "subject": subject}
            save_state(state)
            continue

        entry = {"status": "pending", "subject": subject, "attachments": saved, "ucs": []}

        try:
            mes, ano = extract_mes_ano(subject)
            if mes is None or ano is None:
                log.warning("msg=%s assunto=%r não achou mês/ano no assunto — pulando", msg_id, subject)
                entry["status"] = "sem_mes_ano"
                state[msg_id] = entry
                save_state(state)
                continue

            relevantes = [p for p in saved if p.lower().rsplit(".", 1)[-1] in ("pdf", "xlsx", "xlsm", "csv")]

            all_identificacoes = []
            seen_codes = set()
            for path in relevantes:
                result = extract_report(path)
                for ident in result["identificacoes"]:
                    if ident["uc"] not in seen_codes:
                        seen_codes.add(ident["uc"])
                        all_identificacoes.append(ident)
            all_ucs = [i["uc"] for i in all_identificacoes]

            entry["mes"], entry["ano"] = mes, ano

            if dry_run:
                log.info("[DRY-RUN] msg=%s assunto=%r ucs=%s mes=%s ano=%s", msg_id, subject, all_ucs, mes, ano)
                entry["status"] = "dry_run"
                state[msg_id] = entry
                save_state(state)
                continue

            rows = []
            nao_resolvidos = []
            for ident in all_identificacoes:
                uc = ident["uc"]
                nome = ident.get("nome")
                if uc in conhecidas:
                    uc_canonico = uc
                elif uc in conhecidas_por_parte:
                    uc_canonico = conhecidas_por_parte[uc]
                    log.info("msg=%s UC=%s casou por parte de código composto com cadastro %s", msg_id, uc, uc_canonico)
                else:
                    uc_canonico = conhecidas_por_digitos.get(_only_digits(uc))
                    if uc_canonico:
                        log.info("msg=%s UC=%s casou por so-digitos com cadastro %s", msg_id, uc, uc_canonico)
                    else:
                        uc_canonico, score = match_por_nome(nome, conhecidas_por_nome)
                        if uc_canonico:
                            ucs_via_nome += 1
                            log.warning(
                                "msg=%s UC=%s (nome=%r) SEM MATCH DE CODIGO — casou por RAZAO SOCIAL com "
                                "cadastro %s (score=%.2f). Código do PDF diverge do cadastrado em "
                                "ucs_gestor, revisar/corrigir cadastro.",
                                msg_id, uc, nome, uc_canonico, score,
                            )
                        else:
                            nao_resolvidos.append(ident)
                            continue
                rows.append({
                    "uc_codigo": uc_canonico,
                    "mes": mes,
                    "ano": ano,
                    "recebido": True,
                    "data_recebimento": datetime.now(timezone.utc).isoformat(),
                    "gmail_message_id": msg_id,
                })

            # Dispara também quando `all_identificacoes` veio TOTALMENTE vazio (nenhuma linha
            # "nome - código" achada no PDF) — bug real descoberto 2026-09-01 (CASAS DO OLEO,
            # distribuidora com layout tipo dashboard sem nenhum código no texto): antes esse
            # fallback só rodava se `nao_resolvidos` tivesse item, o que exige que o loop acima
            # tenha rodado pelo menos 1 vez — se a extração não achou NENHUMA identificação, o
            # loop nunca roda, `nao_resolvidos` fica vazio e o fallback por assunto (que resolveria
            # o caso trivialmente, já que o assunto tem o nome do cliente) nunca era tentado.
            if nao_resolvidos or not all_identificacoes:
                subject_cliente = extract_subject_cliente(subject)
                ja_gravados = {r["uc_codigo"] for r in rows}
                grupo_ucs, score_grupo = match_por_nome_grupo(subject_cliente, conhecidas_por_grupo)
                if grupo_ucs:
                    for uc_c in grupo_ucs:
                        if uc_c in ja_gravados:
                            continue
                        ucs_via_assunto += 1
                        rows.append({
                            "uc_codigo": uc_c,
                            "mes": mes,
                            "ano": ano,
                            "recebido": True,
                            "data_recebimento": datetime.now(timezone.utc).isoformat(),
                            "gmail_message_id": msg_id,
                        })
                    log.warning(
                        "msg=%s assunto-cliente=%r SEM MATCH POR LINHA (nomes de filial no PDF ou "
                        "nenhuma identificação extraída: %s) — "
                        "casou por NOME DO ASSUNTO com %d UC(s) cadastradas dessa empresa (score=%.2f). "
                        "Creditando todas — sem CNPJ por UC não dá pra saber qual linha é qual filial.",
                        msg_id, subject_cliente, [i.get("nome") for i in nao_resolvidos], len(grupo_ucs), score_grupo,
                    )
                else:
                    for ident in nao_resolvidos:
                        ucs_desconhecidas += 1
                        log.warning(
                            "msg=%s UC desconhecida (fora de ucs_gestor, sem match por código/nome/assunto): %s (nome=%r)",
                            msg_id, ident["uc"], ident.get("nome"),
                        )

            if rows:
                upsert("relatorios_recebidos", rows, on_conflict="uc_codigo,mes,ano")
                ucs_ok += len(rows)

            entry["status"] = "ok"
            entry["ucs"] = [r["uc_codigo"] for r in rows]
        except Exception as e:
            entry["status"] = "error"
            entry["error"] = str(e)
            falha += 1
            log.error("msg=%s erro: %s", msg_id, e)

        state[msg_id] = entry
        save_state(state)

    log.info(
        "resumo: %d encontradas, %d novas, %d UCs gravadas (%d via razão social da linha, %d via nome do "
        "assunto — cliente multi-UC), %d UCs desconhecidas, %d falhas",
        len(messages), novos, ucs_ok, ucs_via_nome, ucs_via_assunto, ucs_desconhecidas, falha,
    )


def main():
    parser = argparse.ArgumentParser(description="Monitora Gmail (conta aws) por relatório de desempenho")
    parser.add_argument("--max", type=int, default=50, help="Máximo de mensagens por execução")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve no Supabase, só mostra o que faria")
    args = parser.parse_args()
    run(max_results=args.max, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
