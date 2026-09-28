"""DSR-1.4 · leitor de XMLs de NF da caixa dos XMLs via Microsoft Graph.

Subtarefa 1: esqueleto. Subtarefa 2: listar e-mails novos (marca de corte, paginação, renovação do token).
Subtarefa 3: baixar anexos .xml em memória e filtrar só NF.
Subtarefa 4 (DSR-1.4) e DSR-1.5: extrair os campos da NF-e do XML e gravar em captura_nf (sql/006), registrar
(ok/duplicado/erro/ignorado) sem reprocessar. Não usa parse-nf-xml (o cabeçalho só nasce com job; decisoes.md, 25/09/2026).

Reaproveita ler_env e obter_token de graph_teste.py (variáveis MS_DSR_*).
Nunca imprime nem loga token, segredo, assunto ou corpo de e-mail. Do remetente usa só o domínio.
Assunto e prévia do corpo são lidos só para achar o mês de referência da NF quando o XML não o traz (competencia_nf.py); não são gravados.

Uso:
  python3 simple/dashboard-relatorios/captura/leitor_outlook.py --ensaio            # só lista, não grava
  python3 simple/dashboard-relatorios/captura/leitor_outlook.py --ensaio --limite 5
  python3 simple/dashboard-relatorios/captura/leitor_outlook.py --enviar --limite 3     # GRAVA em captura_nf (produção)
"""

import argparse
import json
import logging
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph_teste import GRAPH, ler_env, obter_token  # noqa: E402,F401
import competencia_nf as cnf  # noqa: E402

TABELA = "captura_emails_processados"
BUCKET_XML = "xml-nf"
DIAS_PRIMEIRA_EXECUCAO = 7
POR_PAGINA = 50
RAIZES_NF = {"nfeProc", "NFe"}
DIAS_RETENTATIVA_ERRO = 7
LOG_ARQUIVO = Path(__file__).resolve().parent / "leitor.log"

log = logging.getLogger("leitor_outlook")


def configurar_log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_ARQUIVO, encoding="utf-8"), logging.StreamHandler()],
    )


def _rest(env):
    """(url, headers) do REST do Supabase do relatório, ou (None, None) se faltar configuração."""
    url, chave = env.get("SUPABASE_RELATORIO_URL"), env.get("SUPABASE_RELATORIO_SERVICE_KEY")
    if not url or not chave:
        return None, None
    return f"{url}/rest/v1", {"apikey": chave, "Authorization": f"Bearer {chave}"}


def marca_de_corte(env):
    """Maior recebido_em já registrado; sem tabela ou vazia, 7 dias atrás. Se houver erro dos últimos
    7 dias, a marca recua até o erro mais antigo, para ele ser tentado de novo."""
    agora = datetime.now(timezone.utc)
    padrao = agora - timedelta(days=DIAS_PRIMEIRA_EXECUCAO)
    base, hdr = _rest(env)
    if not base:
        log.warning("SUPABASE_RELATORIO_URL/SERVICE_KEY ausentes: usando %s dias atrás.", DIAS_PRIMEIRA_EXECUCAO)
        return padrao
    r = requests.get(f"{base}/{TABELA}?select=recebido_em&order=recebido_em.desc&limit=1", headers=hdr, timeout=30)
    if r.status_code != 200:
        log.warning("Tabela %s não lida (HTTP %s; aplicou o sql/003?): usando %s dias atrás.",
                    TABELA, r.status_code, DIAS_PRIMEIRA_EXECUCAO)
        return padrao
    linhas = r.json()
    if not linhas or not linhas[0].get("recebido_em"):
        return padrao
    marca = datetime.fromisoformat(linhas[0]["recebido_em"].replace("Z", "+00:00"))
    limite_erro = (agora - timedelta(days=DIAS_RETENTATIVA_ERRO)).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = requests.get(
        f"{base}/{TABELA}?select=recebido_em&resultado=eq.erro&recebido_em=gte.{limite_erro}&order=recebido_em.asc&limit=1",
        headers=hdr, timeout=30)
    if e.status_code == 200 and e.json() and e.json()[0].get("recebido_em"):
        erro_mais_antigo = datetime.fromisoformat(e.json()[0]["recebido_em"].replace("Z", "+00:00"))
        if erro_mais_antigo < marca:
            log.info("Há erro recente a tentar de novo: marca recuada para %s.", erro_mais_antigo.isoformat())
            marca = erro_mais_antigo
    return marca


def ja_concluidos(env, message_id):
    """attachment_ids deste e-mail já com resultado ok ou ignorado (erro não conta: é tentado de novo)."""
    base, hdr = _rest(env)
    if not base:
        return set()
    r = requests.get(
        f"{base}/{TABELA}?select=attachment_id&message_id=eq.{requests.utils.quote(message_id, safe='')}"
        f"&resultado=in.(ok,ignorado)", headers=hdr, timeout=30)
    if r.status_code != 200:
        log.error("Consulta de controle falhou (HTTP %s): parando por segurança, para não reprocessar.", r.status_code)
        sys.exit("Consulta de controle falhou.")
    return {x["attachment_id"] for x in r.json()}


def _get(sessao, url):
    """GET com renovação de token uma vez em caso de 401. sessao = {'env':..., 'token':...}."""
    for tentativa in (1, 2):
        r = requests.get(url, headers={"Authorization": f"Bearer {sessao['token']}"}, timeout=60)
        for _ in range(3):
            if r.status_code not in (429, 503):
                break
            espera = min(int(r.headers.get("Retry-After", "5") or 5), 60)
            log.warning("Graph pediu pausa (HTTP %s): esperando %ss.", r.status_code, espera)
            time.sleep(espera)
            r = requests.get(url, headers={"Authorization": f"Bearer {sessao['token']}"}, timeout=60)
        if r.status_code != 401:
            return r
        if tentativa == 1:
            log.info("401 do Graph: renovando o token e repetindo uma vez.")
            sessao["token"] = obter_token(sessao["env"])
    log.error("401 de novo após renovar o token: parando.")
    sys.exit("Graph negou o acesso (401) mesmo com token novo.")


def listar_emails(sessao, marca, limite=None):
    """E-mails com anexo recebidos a partir da marca, do mais antigo ao mais novo, com paginação."""
    caixa = sessao["env"]["MS_DSR_MAILBOX"]
    desde = marca.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"{GRAPH}/users/{caixa}/mailFolders/inbox/messages"
        f"?$filter=receivedDateTime ge {desde} and hasAttachments eq true"
        f"&$select=id,from,receivedDateTime,subject,bodyPreview&$orderby=receivedDateTime asc&$top={POR_PAGINA}"
    )
    emails, paginas = [], 0
    while url:
        r = _get(sessao, url)
        if r.status_code != 200:
            codigo = r.json().get("error", {}).get("code", "?") if r.headers.get("content-type", "").startswith("application/json") else "?"
            log.error("Listagem: HTTP %s (%s).", r.status_code, codigo)
            sys.exit(f"Listagem falhou: HTTP {r.status_code}")
        dados = r.json()
        paginas += 1
        for m in dados.get("value", []):
            endereco = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
            emails.append({
                "id": m["id"],
                "dominio": endereco.split("@")[-1] if "@" in endereco else "",
                "recebido_em": m["receivedDateTime"],
                "assunto": m.get("subject") or "",
                "corpo": m.get("bodyPreview") or "",
            })
            if limite and len(emails) >= limite:
                log.info("Limite de %s e-mails atingido (%s páginas lidas).", limite, paginas)
                return emails
        url = dados.get("@odata.nextLink")
    log.info("Listagem: %s e-mails em %s páginas (a partir de %s).", len(emails), paginas, desde)
    return emails


def baixar_anexos(sessao, email):
    """Anexos .xml do e-mail, em memória. Lista os anexos sem conteúdo e baixa só os .xml (via /$value,
    que também serve anexos grandes). Retorna (lista de dicts, n_zip). Não grava em disco."""
    caixa = sessao["env"]["MS_DSR_MAILBOX"]
    base = f"{GRAPH}/users/{caixa}/messages/{email['id']}/attachments"
    r = _get(sessao, f"{base}?$select=id,name,contentType,size,isInline")
    if r.status_code != 200:
        log.error("Anexos do e-mail %s...: HTTP %s.", email["id"][:12], r.status_code)
        return [], 0
    anexos, n_zip = [], 0
    for a in r.json().get("value", []):
        if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        nome = a.get("name") or ""
        if nome.lower().endswith((".zip", ".rar", ".7z", ".gz")):
            n_zip += 1
            continue
        if not nome.lower().endswith(".xml"):
            continue
        conteudo = _get(sessao, f"{base}/{a['id']}/$value")
        if conteudo.status_code != 200:
            log.error("Anexo %s...: HTTP %s.", a["id"][:12], conteudo.status_code)
            continue
        anexos.append({
            "message_id": email["id"], "attachment_id": a["id"], "nome": nome,
            "recebido_em": email["recebido_em"], "bytes": conteudo.content,
        })
    return anexos, n_zip


def eh_nf(conteudo):
    """True se o XML tem raiz nfeProc ou NFe. Recusa DTD/entidades (XML malicioso)."""
    if b"<!DOCTYPE" in conteudo or b"<!ENTITY" in conteudo:
        return False
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError:
        return False
    return raiz.tag.rsplit("}", 1)[-1] in RAIZES_NF


def filtrar_nf(anexos):
    """Mantém só os XMLs cuja raiz é nfeProc ou NFe. Retorna (nfs, n_outros_xml)."""
    nfs = [a for a in anexos if eh_nf(a["bytes"])]
    return nfs, len(anexos) - len(nfs)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _filho(el, *caminho):
    """Desce por nomes de tag (sem namespace). Devolve o elemento ou None."""
    for nome in caminho:
        if el is None:
            return None
        el = next((f for f in el if _local(f.tag) == nome), None)
    return el


def _texto(el, *caminho):
    alvo = _filho(el, *caminho)
    return (alvo.text or "").strip() or None if alvo is not None else None


def extrair_nf(conteudo):
    """Campos da NF-e a partir do XML (nfeProc ou NFe). Levanta ValueError sem chave de 44 dígitos."""
    raiz = ET.fromstring(conteudo)
    inf = next((e for e in raiz.iter() if _local(e.tag) == "infNFe"), None)
    if inf is None:
        raise ValueError("sem infNFe")
    prot = next((e for e in raiz.iter() if _local(e.tag) == "infProt"), None)
    chave = (inf.get("Id") or "").removeprefix("NFe") or (_texto(prot, "chNFe") or "")
    if not (len(chave) == 44 and chave.isdigit()):
        raise ValueError("chave de acesso ausente ou inválida")
    emissao = _texto(inf, "ide", "dhEmi") or _texto(inf, "ide", "dEmi")
    valor = _texto(inf, "total", "ICMSTot", "vNF")
    return {
        "chave_acesso": chave,
        "emitente_cnpj": _texto(inf, "emit", "CNPJ") or _texto(inf, "emit", "CPF"),
        "emitente_nome": _texto(inf, "emit", "xNome"),
        "destinatario_cnpj": _texto(inf, "dest", "CNPJ") or _texto(inf, "dest", "CPF"),
        "destinatario_nome": _texto(inf, "dest", "xNome"),
        "numero": _texto(inf, "ide", "nNF"),
        "serie": _texto(inf, "ide", "serie"),
        "data_emissao": emissao,
        "valor_total": float(valor) if valor else None,
        "protocolo": _texto(prot, "nProt"),
        "cstat": _texto(prot, "cStat"),
    }


def competencia_da_nf(conteudo, campos, email):
    """Mês de referência da NF (sql/015): texto do XML, senão assunto e corpo do e-mail. Devolve o dict de competencia_nf."""
    try:
        emissao = datetime.fromisoformat(campos["data_emissao"]).date() if campos.get("data_emissao") else None
    except ValueError:
        emissao = None
    return cnf.competencia_da_nf(conteudo, emissao, email.get("assunto"), email.get("corpo"))


def caminho_xml(chave):
    """Caminho do XML no bucket: <ano>/<mes>/<chave>.xml. Ano e mês vêm da chave (posições 3 a 6 = AAMM)."""
    return f"20{chave[2:4]}/{chave[4:6]}/{chave}.xml"


def subir_xml(env, chave, conteudo):
    """Sobe o XML ao bucket privado xml-nf (sobrescreve o mesmo caminho: é a mesma NF). Retorna o caminho."""
    url = env.get("SUPABASE_RELATORIO_URL")
    _, hdr = _rest(env)
    caminho = caminho_xml(chave)
    r = requests.post(
        f"{url}/storage/v1/object/{BUCKET_XML}/{caminho}",
        headers={**hdr, "Content-Type": "application/xml", "x-upsert": "true"},
        data=conteudo, timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code} ao subir o XML ao Storage")
    return caminho


def gravar_nf(env, nf, campos):
    """Sobe o XML ao Storage e grava a NF em captura_nf. Retorna 'ok' (linha nova) ou 'duplicado' (chave já existia)."""
    base, hdr = _rest(env)
    xml_path = subir_xml(env, campos["chave_acesso"], nf["bytes"])
    r = requests.post(
        f"{base}/captura_nf?on_conflict=chave_acesso",
        headers={**hdr, "Content-Type": "application/json", "Prefer": "resolution=ignore-duplicates,return=representation"},
        data=json.dumps({**campos, "message_id": nf["message_id"], "attachment_id": nf["attachment_id"],
                         "arquivo_nome": nf["nome"], "recebido_em": nf["recebido_em"], "xml_path": xml_path}),
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code} ao gravar captura_nf")
    resultado = "ok" if r.json() else "duplicado"
    atribuir_pasta(env, campos["chave_acesso"])
    return resultado


def atribuir_pasta(env, chave):
    """Coloca a NF na pasta aberta do gestor (função atribuir_xml_a_pasta, sql/011). Idempotente: no 'duplicado' repete
    sem efeito. Se falhar, a NF vira 'erro' e é tentada de novo (a NF já gravada volta como 'duplicado')."""
    base, hdr = _rest(env)
    r = requests.post(f"{base}/rpc/atribuir_xml_a_pasta", headers={**hdr, "Content-Type": "application/json"},
                      json={"p_chave_acesso": chave}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code} ao atribuir a pasta")


def registrar(env, message_id, attachment_id, arquivo_nome, recebido_em, resultado, erro=None):
    """Grava (ou atualiza) a linha em captura_emails_processados. Nunca guarda o XML (ele vai ao Storage)."""
    base, hdr = _rest(env)
    r = requests.post(
        f"{base}/{TABELA}?on_conflict=message_id,attachment_id",
        headers={**hdr, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
        data=json.dumps({
            "message_id": message_id, "attachment_id": attachment_id, "arquivo_nome": arquivo_nome,
            "recebido_em": recebido_em, "resultado": resultado, "erro": erro,
            "processado_em": datetime.now(timezone.utc).isoformat(),
        }),
        timeout=30,
    )
    if r.status_code >= 300:
        log.error("Registro falhou (HTTP %s): parando, para não enviar de novo sem anotar.", r.status_code)
        sys.exit("Registro em captura_emails_processados falhou.")


def registrar_batida(env, c, limite_atingido):
    """Grava a 'batida' da execução em captura_execucoes (sql/018): o alerta (captura-xml-alerta.yml) olha se ela existe
    e como terminou. Se falhar, só avisa no log: a captura já foi feita, e o alerta pega a falta de batida."""
    base, hdr = _rest(env)
    try:
        r = requests.post(
            f"{base}/captura_execucoes",
            headers={**hdr, "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={"emails_lidos": c["emails"], "xml_novos": c["xml"], "nf_gravadas": c["ok"],
                  "erros": c["erro"], "limite_atingido": limite_atingido},
            timeout=30,
        )
    except requests.RequestException as exc:
        log.error("Batida não gravada (%s).", type(exc).__name__)
        return
    if r.status_code >= 300:
        log.error("Batida não gravada (HTTP %s): aplicou o sql/018?", r.status_code)


def main():
    ap = argparse.ArgumentParser(description="Lê XMLs de NF da caixa dos XMLs pelo Graph e grava em captura_nf.")
    ap.add_argument("--ensaio", action="store_true", help="padrão: só lista e extrai, não grava nada")
    ap.add_argument("--enviar", action="store_true", help="GRAVA em captura_nf e no controle, banco de PRODUÇÃO (exige --limite)")
    ap.add_argument("--limite", type=int, default=None, help="máximo de e-mails a ler (obrigatório com --enviar)")
    args = ap.parse_args()
    if args.enviar and args.ensaio:
        sys.exit("Use --ensaio ou --enviar, não os dois.")
    if args.enviar and not args.limite:
        sys.exit("--enviar exige --limite (ex.: --limite 3): o envio grava no banco de produção.")
    args.ensaio = not args.enviar  # sem --enviar é sempre ensaio

    configurar_log()
    env = ler_env()
    sessao = {"env": env, "token": obter_token(env)}  # falha cedo se segredo, client ou tenant estiverem errados
    log.info("Token obtido. Caixa: %s. Ensaio: %s. Limite: %s.", env["MS_DSR_MAILBOX"], args.ensaio, args.limite)

    marca = marca_de_corte(env)
    emails = listar_emails(sessao, marca, args.limite)
    c = {"emails": len(emails), "ja_feitos": 0, "xml": 0, "nf": 0, "ok": 0, "duplicado": 0, "erro": 0, "ignorado": 0, "zip": 0}
    for e in emails:
        concluidos = ja_concluidos(env, e["id"])
        anexos, n_zip = baixar_anexos(sessao, e)
        c["zip"] += n_zip
        for a in anexos:
            if a["attachment_id"] in concluidos:
                c["ja_feitos"] += 1
                continue
            c["xml"] += 1
            if not eh_nf(a["bytes"]):
                c["ignorado"] += 1
                if not args.ensaio:
                    registrar(env, a["message_id"], a["attachment_id"], a["nome"], a["recebido_em"], "ignorado")
                continue
            c["nf"] += 1
            try:
                campos = extrair_nf(a["bytes"])
            except (ValueError, ET.ParseError) as exc:
                erro = str(exc)
                if not args.ensaio:
                    registrar(env, a["message_id"], a["attachment_id"], a["nome"], a["recebido_em"], "erro", erro)
                c["erro"] += 1
                log.error("NF do e-mail %s...: %s", e["id"][:12], erro)
                continue
            comp = competencia_da_nf(a["bytes"], campos, e)
            campos["competencia"], campos["competencia_origem"] = comp["competencia"], comp["origem"]
            if args.ensaio:
                print(f"{e['recebido_em']}  {e['dominio']:<26} chave {campos['chave_acesso'][:6]}…{campos['chave_acesso'][-4:]}"
                      f"  dest {(campos['destinatario_cnpj'] or '-')[:8]}…  competência {comp['competencia'] or 'sem indicação'}"
                      f" ({comp['origem'] or ('empate' if comp['empate'] else '-')})")
                continue
            try:
                resultado = gravar_nf(env, a, campos)
                erro = None
            except (requests.RequestException, RuntimeError) as exc:
                resultado, erro = "erro", str(exc)[:200]
            registrar(env, a["message_id"], a["attachment_id"], a["nome"], a["recebido_em"], resultado, erro)
            c[resultado] += 1
            if erro:
                log.error("NF do e-mail %s...: %s", e["id"][:12], erro)
    log.info("Resumo: e-mails lidos %(emails)s | XMLs novos %(xml)s | já concluídos antes %(ja_feitos)s | "
             "NF %(nf)s | gravadas %(ok)s | duplicadas %(duplicado)s | erro %(erro)s | outros XML ignorados %(ignorado)s | ZIPs %(zip)s", c)
    if not args.ensaio:
        registrar_batida(env, c, bool(args.limite) and len(emails) >= args.limite)


if __name__ == "__main__":
    main()
