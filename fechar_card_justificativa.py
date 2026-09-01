"""
Roda a cada 30min (junto com o watcher): pra cada ciclo de justificativa de
atraso (`justificativa_ciclos`) cujo gestor já respondeu TODAS as UCs dele
(`justificativa_itens.respondido=true` em 100%), fecha a Solicitação
correspondente no CRM Bubble (Status -> "Finalizado").

Diferente de `enviar_analise_atrasos.py` (que só age quando TODOS os
gestores do mês terminam, pra mandar a análise aos coordenadores): aqui é
por gestor individual — assim que ele termina de justificar as dele, o
card dele fecha na hora, sem esperar os outros.

Sem coluna nova no Supabase: em vez de guardar "já fechei esse card" em
`justificativa_ciclos`, cada rodada busca o Status atual da Solicitação no
Bubble antes de decidir se fecha — evita reabrir/repatchar à toa e evita
depender de migration nova.

Uso:
  python3 fechar_card_justificativa.py --dry-run
  python3 fechar_card_justificativa.py --env live
"""

import argparse
import logging

import requests

from check_relatorios_pendentes import BUBBLE_BASE_URLS, get_bubble_key
from supabase_client import select

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fechar_card_justificativa")

STATUS_FINALIZADO = "Finalizado"


def gestor_concluiu(token):
    pendentes = select("justificativa_itens", filters={"token": f"eq.{token}", "respondido": "eq.false"})
    return len(pendentes) == 0


def buscar_status_solicitacao(env_name, solicitacao_id):
    base_url = BUBBLE_BASE_URLS[env_name]
    key = get_bubble_key(env_name)
    resp = requests.get(
        f"{base_url}/obj/solicitacao/{solicitacao_id}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("response", {}).get("Status")


def fechar_solicitacao(env_name, solicitacao_id):
    base_url = BUBBLE_BASE_URLS[env_name]
    key = get_bubble_key(env_name)
    resp = requests.patch(
        f"{base_url}/obj/solicitacao/{solicitacao_id}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"Status": STATUS_FINALIZADO},
        timeout=30,
    )
    resp.raise_for_status()


def run(env_name="test", dry_run=False):
    ciclos = select("justificativa_ciclos", filters={"solicitacao_bubble_id": "not.is.null"})
    log.info("%d ciclo(s) com Solicitacao no Bubble pra checar", len(ciclos))

    fechados = 0
    for ciclo in ciclos:
        gestor_nome = ciclo["gestor_nome"]
        solicitacao_id = ciclo["solicitacao_bubble_id"]
        mes, ano = ciclo["mes"], ciclo["ano"]

        if not gestor_concluiu(ciclo["token"]):
            continue

        try:
            status_atual = buscar_status_solicitacao(env_name, solicitacao_id)
        except Exception as e:
            log.error("%s (%02d/%d): erro ao buscar Solicitacao %s: %s", gestor_nome, mes, ano, solicitacao_id, e)
            continue

        if status_atual == STATUS_FINALIZADO:
            continue

        log.info(
            "%s (%02d/%d) terminou de justificar (%d UC(s)) — fechando Solicitacao %s (status atual: %r)",
            gestor_nome, mes, ano, ciclo["total_itens"], solicitacao_id, status_atual,
        )
        if dry_run:
            continue

        try:
            fechar_solicitacao(env_name, solicitacao_id)
            fechados += 1
        except Exception as e:
            log.error("%s (%02d/%d): erro ao fechar Solicitacao %s: %s", gestor_nome, mes, ano, solicitacao_id, e)

    log.info("resumo: %d Solicitacao(oes) fechada(s)", fechados)


def main():
    parser = argparse.ArgumentParser(description="Fecha no CRM a Solicitacao de justificativa do gestor que ja respondeu tudo")
    parser.add_argument("--env", choices=["test", "live"], default="test", help="Ambiente Bubble (default: test)")
    parser.add_argument("--dry-run", action="store_true", help="Nao escreve nada, so mostra o que faria")
    args = parser.parse_args()
    run(env_name=args.env, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
