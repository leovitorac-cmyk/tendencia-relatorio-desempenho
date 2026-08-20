"""
Roda no 1o dia útil do mês: fecha o ciclo de checagem do mês anterior.
Pra cada UC que ficou o mês inteiro sem relatório (`recebido=false` em
`relatorios_recebidos`, mês de referência = 2 meses atrás), cria 1 linha em
`justificativa_itens` (Supabase) por UC, 1 `Solicitacao` no Bubble (prazo:
próximo dia útil) e manda 1 e-mail ao gestor com o link pra justificar.

Ref. de mês: em 01/09, "relatório de julho" (mes=7) é o que devia ter
chegado em agosto e continua pendente — mesmo mes que `check_relatorios_pendentes.py`
usava durante os dias de checagem de agosto.

Idempotência: 1 linha em `justificativa_ciclos` por (mes, ano, gestor_email)
— rodar de novo no mesmo ciclo não duplica (gestor já processado é pulado).

Uso:
  python3 enviar_justificativa_atraso.py --dry-run              # não escreve nada
  python3 enviar_justificativa_atraso.py --force --dry-run      # ignora checagem de dia útil
  python3 enviar_justificativa_atraso.py                        # roda de verdade (uso via cron, dia 1)
  python3 enviar_justificativa_atraso.py --env live             # default é test
"""

import argparse
import logging
import os
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from check_relatorios_pendentes import (
    BUBBLE_BASE_URLS,
    email_template,
    enviar_email,
    get_automacao_user_id,
    get_bubble_key,
    montar_uc_cards_html,
)
from supabase_client import load_env, select, upsert

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = Path(BASE_DIR).parent.parent / ".env.local"

PORTAL_BASE_URL = "https://plataforma.tendenciaenergia.com.br"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enviar_justificativa_atraso")


def mes_referencia(hoje):
    """2 meses atrás — mês que ficou pendente o mês de checagem inteiro."""
    mes = hoje.month - 2
    ano = hoje.year
    if mes <= 0:
        mes += 12
        ano -= 1
    return mes, ano


def proximo_dia_util(hoje):
    d = hoje + timedelta(days=1)
    while d.weekday() >= 5:  # 5=sábado, 6=domingo
        d += timedelta(days=1)
    return d


def buscar_pendentes_do_mes(mes, ano):
    """Todas as UCs ainda `recebido=false` no mes/ano — notificadas ou não
    durante o ciclo de checagem, diferente de `buscar_pendentes()` do
    check_relatorios_pendentes.py (que pula quem já foi notificado)."""
    ucs = select("ucs_gestor", filters={"ativo": "eq.true"})
    recebidos = select(
        "relatorios_recebidos",
        filters={"mes": f"eq.{mes}", "ano": f"eq.{ano}"},
    )
    por_uc = {r["uc_codigo"]: r for r in recebidos}

    pendentes = []
    for uc in ucs:
        existente = por_uc.get(uc["uc_codigo"])
        if existente and existente.get("recebido"):
            continue
        pendentes.append(uc)
    return pendentes


def ciclos_existentes(mes, ano):
    rows = select("justificativa_ciclos", filters={"mes": f"eq.{mes}", "ano": f"eq.{ano}"})
    return {r["gestor_email"] for r in rows}


def criar_justificativas_supabase(ucs_pendentes, token, mes, ano):
    rows = [{
        "uc_codigo": uc["uc_codigo"],
        "cliente_nome": uc.get("cliente_nome") or "",
        "gestor_nome": uc["gestor_nome"],
        "gestor_email": uc["gestor_email"],
        "mes": mes,
        "ano": ano,
        "token": token,
        "respondido": False,
    } for uc in ucs_pendentes]
    upsert("justificativa_itens", rows, on_conflict="uc_codigo,mes,ano")


def criar_solicitacao_justificativa_bubble(env_name, gestor_nome, gestor_email, gestor_bubble_user_id, ucs_pendentes, mes, ano, link):
    base_url = BUBBLE_BASE_URLS[env_name]
    key = get_bubble_key(env_name)
    automacao_user_id = get_automacao_user_id(env_name)
    prazo = proximo_dia_util(date.today())

    payload = {
        "Titulo": f"Justificar atrasos de relatório — {mes:02d}/{ano}",
        "Descricao": (
            f"{len(ucs_pendentes)} UC(s) suas ficaram sem relatório de desempenho de {mes:02d}/{ano}. "
            f"Justifique cada uma pelo link: {link}"
        ),
        "Status": "Solicitado",
        "Tipo_Responsavel": "Pessoa",
        "Responsavel_Departamento": "Gestão",
        "data_solicitada": datetime.now(timezone.utc).isoformat(),
        "data entrega": datetime(prazo.year, prazo.month, prazo.day, tzinfo=timezone.utc).isoformat(),
        "veio_do_cliente": False,
        "veio_nexi": True,
    }
    if gestor_bubble_user_id:
        payload["Responsavel_Pessoa"] = gestor_bubble_user_id
    if automacao_user_id:
        payload["Criador"] = automacao_user_id

    resp = requests.post(
        f"{base_url}/obj/solicitacao",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("id")


def enviar_email_justificativa(gestor_nome, gestor_email, ucs_pendentes, mes, ano, link):
    heading = (
        f"Olá, {gestor_nome}! {len(ucs_pendentes)} UC(s) sua(s) ficaram sem relatório de "
        f"{mes:02d}/{ano}. Justifique cada atraso pelo link abaixo."
    )
    body = (
        montar_uc_cards_html(ucs_pendentes)
        + f'<p style="margin:20px 0 0;"><a href="{link}" '
        f'style="display:inline-block;background:#0B4A63;color:#ffffff;text-decoration:none;'
        f'padding:11px 20px;border-radius:8px;font-weight:700;font-size:13.5px;">Justificar agora</a></p>'
    )
    html = email_template("Justificativa pendente", heading, body)
    return enviar_email(
        gestor_nome, gestor_email,
        f"Justifique o atraso de relatório — {len(ucs_pendentes)} UC(s) — {mes:02d}/{ano}",
        html,
    )


def run(env_name="test", force=False, dry_run=False):
    hoje = date.today()
    if not force and hoje.weekday() >= 5:
        log.info("hoje é fim de semana — nada a fazer (use --force pra ignorar)")
        return
    if not force and hoje.day != 1:
        log.info("hoje não é dia 1 do mês — nada a fazer (use --force pra ignorar)")
        return

    mes, ano = mes_referencia(hoje)
    pendentes = buscar_pendentes_do_mes(mes, ano)
    log.info("%d UCs ficaram sem relatório em %02d/%d", len(pendentes), mes, ano)
    if not pendentes:
        return

    ja_processados = ciclos_existentes(mes, ano)

    por_gestor = defaultdict(list)
    for uc in pendentes:
        por_gestor[uc["gestor_email"]].append(uc)

    total_ciclos = 0
    for gestor_email, ucs_pendentes in por_gestor.items():
        gestor_nome = ucs_pendentes[0]["gestor_nome"]
        gestor_bubble_user_id = ucs_pendentes[0].get("gestor_bubble_user_id")

        if gestor_email in ja_processados:
            log.info("%s já tem ciclo de justificativa em %02d/%d — pulando", gestor_nome, mes, ano)
            continue

        token = str(uuid.uuid4())
        link = f"{PORTAL_BASE_URL}/justificar-atraso?token={token}"

        if dry_run:
            log.info(
                "[DRY-RUN] criaria ciclo pra %s <%s> com %d UC(s), link %s",
                gestor_nome, gestor_email, len(ucs_pendentes), link,
            )
            continue

        # ciclo precisa existir ANTES dos itens — justificativa_itens.token
        # tem FK pra justificativa_ciclos.token (ver migration_justificativa_itens.sql).
        upsert("justificativa_ciclos", [{
            "mes": mes,
            "ano": ano,
            "gestor_email": gestor_email,
            "gestor_nome": gestor_nome,
            "token": token,
            "total_itens": len(ucs_pendentes),
        }], on_conflict="mes,ano,gestor_email")

        criar_justificativas_supabase(ucs_pendentes, token, mes, ano)
        solicitacao_id = criar_solicitacao_justificativa_bubble(
            env_name, gestor_nome, gestor_email, gestor_bubble_user_id, ucs_pendentes, mes, ano, link,
        )
        enviar_email_justificativa(gestor_nome, gestor_email, ucs_pendentes, mes, ano, link)

        upsert("justificativa_ciclos", [{
            "mes": mes,
            "ano": ano,
            "gestor_email": gestor_email,
            "gestor_nome": gestor_nome,
            "token": token,
            "total_itens": len(ucs_pendentes),
            "solicitacao_bubble_id": solicitacao_id,
        }], on_conflict="mes,ano,gestor_email")
        total_ciclos += 1

    log.info("resumo: %d ciclos de justificativa criados em %02d/%d", total_ciclos, mes, ano)


def main():
    parser = argparse.ArgumentParser(description="Cria ciclo de justificativa de atraso e notifica gestores")
    parser.add_argument("--env", choices=["test", "live"], default="test", help="Ambiente Bubble (default: test)")
    parser.add_argument("--force", action="store_true", help="Roda mesmo fora do dia 1 útil")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve nada, só mostra o que faria")
    args = parser.parse_args()
    run(env_name=args.env, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
