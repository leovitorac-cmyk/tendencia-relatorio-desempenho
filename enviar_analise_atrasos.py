"""
Cron diário: fecha ciclos de justificativa de atraso quando TODOS os
gestores do ciclo (mes,ano) já responderam todas as UCs, e manda 1 e-mail
consolidado de análise (motivo mais comum + ranking de gestor com mais
atraso) pros coordenadores (Felipe e Diogo).

Fonte da resposta = Supabase `justificativa_itens` (motivo/respondido),
preenchido pelo gestor na página `justificar-atraso` (grava direto via
REST/RLS, sem passar pelo Bubble). `justificativa_ciclos` guarda o
token/controle de cada ciclo.

Uso:
  python3 enviar_analise_atrasos.py --dry-run       # não escreve nada
  python3 enviar_analise_atrasos.py                 # roda de verdade (uso via cron, diário)
"""

import argparse
import logging
from collections import Counter, defaultdict

from check_relatorios_pendentes import COORDENADORES, email_template, enviar_email
from supabase_client import select, upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enviar_analise_atrasos")


def ciclos_pendentes_de_analise():
    rows = select("justificativa_ciclos", filters={"analise_enviada": "eq.false"})
    por_mes_ano = defaultdict(list)
    for r in rows:
        por_mes_ano[(r["mes"], r["ano"])].append(r)
    return por_mes_ano


def gestor_concluiu(token):
    pendentes = select("justificativa_itens", filters={"token": f"eq.{token}", "respondido": "eq.false"})
    return len(pendentes) == 0


def montar_analise_html(itens):
    por_motivo = Counter(i.get("motivo") or "(sem motivo)" for i in itens)
    por_gestor = Counter(i.get("gestor_nome") or "(sem gestor)" for i in itens)

    linhas_motivo = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #E7ECF3;">'
        f'<span style="font-size:13.5px;color:#1F2430;">{motivo}</span>'
        f'<span style="font-size:13.5px;font-weight:700;color:#0B4A63;">{qtd}</span></div>'
        for motivo, qtd in por_motivo.most_common()
    )
    linhas_gestor = "".join(
        f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #E7ECF3;">'
        f'<span style="font-size:13.5px;color:#1F2430;">{gestor}</span>'
        f'<span style="font-size:13.5px;font-weight:700;color:#0B4A63;">{qtd}</span></div>'
        for gestor, qtd in por_gestor.most_common()
    )

    return (
        f'<p style="margin:0 0 8px;font-weight:700;color:#1F2430;">Motivos mais comuns</p>{linhas_motivo}'
        f'<p style="margin:22px 0 8px;font-weight:700;color:#1F2430;">Ranking de gestores (mais atrasos)</p>{linhas_gestor}'
    )


def run(dry_run=False):
    por_mes_ano = ciclos_pendentes_de_analise()
    if not por_mes_ano:
        log.info("nenhum ciclo pendente de análise")
        return

    for (mes, ano), ciclos in por_mes_ano.items():
        pendentes = [c for c in ciclos if not gestor_concluiu(c["token"])]
        if pendentes:
            log.info(
                "%02d/%d ainda não fechou — %d gestor(es) pendente(s): %s",
                mes, ano, len(pendentes), [c["gestor_nome"] for c in pendentes],
            )
            continue

        itens = []
        for ciclo in ciclos:
            itens.extend(select("justificativa_itens", filters={"token": f"eq.{ciclo['token']}"}))
        log.info("%02d/%d fechou — %d gestor(es), %d UC(s) justificadas", mes, ano, len(ciclos), len(itens))

        if dry_run:
            log.info("[DRY-RUN] mandaria análise consolidada pra %s", [c["email"] for c in COORDENADORES])
            continue

        heading = f"Ciclo de justificativa de {mes:02d}/{ano} fechado — {len(itens)} UC(s), {len(ciclos)} gestor(es)."
        html = email_template("Análise de atrasos", heading, montar_analise_html(itens))
        for coord in COORDENADORES:
            enviar_email(
                coord["nome"], coord["email"],
                f"Análise de atrasos de relatório — {mes:02d}/{ano}",
                html,
            )

        upsert(
            "justificativa_ciclos",
            [{**{k: c[k] for k in ("mes", "ano", "gestor_email", "gestor_nome", "token", "total_itens", "solicitacao_bubble_id")}, "analise_enviada": True} for c in ciclos],
            on_conflict="mes,ano,gestor_email",
        )


def main():
    parser = argparse.ArgumentParser(description="Fecha ciclos de justificativa concluídos e manda análise aos coordenadores")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve/envia nada, só mostra o que faria")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
