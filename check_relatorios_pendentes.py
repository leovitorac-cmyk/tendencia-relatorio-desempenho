"""
Regras definitivas (Leo, 2026-08-20). Roda 2x por dia (manhã ~7:30 e tarde
~13:30, horário de Brasília — a 2ª rodada existe pra pegar quem a equipe
enviou o relatório entre as duas), nos dias 20, 22, 25, 26, 27, 28, 29, 30
e 31 de cada mês. Se um desses dias cair em sábado/domingo, desloca só
aquele dia pro próximo dia útil (não afeta os outros dias já configurados
— ver `_dia_efetivo`). Cruza `ucs_gestor` × `relatorios_recebidos` do mês
de referência (mês anterior ao atual — em agosto checa o relatório de
julho) e notifica (Brevo) cada gestor com UC(s) pendente(s).

Cria Solicitacao no Bubble (1 por UC pendente) **só na rodada da manhã do
dia 20** (ajustado pro próximo dia útil se cair fim de semana) — em todas
as outras rodadas (tarde do dia 20, e qualquer rodada dos outros dias) é
só o e-mail, sem card no CRM.

Sem dedupe entre rodadas: cada rodada (mesmo no mesmo dia) reenvia email
pra quem continuar pendente — decisão do Leo, pra manter o gestor ciente
a cada checagem, mesmo que já tenha sido avisado antes no mesmo ciclo.
`notificado_em` é só registro histórico (última vez que essa UC apareceu
numa notificação), não bloqueia mais reenvio.

Trava só na criação de Solicitacao: se a UC já tem `solicitacao_bubble_id`
gravado pra esse mes/ano (de uma rodada anterior), não cria outra — evita
card duplicado no CRM se a rodada de criação rodar 2x por engano.

Uso:
  python3 check_relatorios_pendentes.py --turno manha --dry-run   # não escreve nada
  python3 check_relatorios_pendentes.py --turno manha --force --dry-run  # ignora a checagem de dia
  python3 check_relatorios_pendentes.py --turno manha             # roda de verdade (uso via cron)
  python3 check_relatorios_pendentes.py --turno tarde --env live  # rodada da tarde, sem Solicitacao
"""

import argparse
import calendar
import logging
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from supabase_client import load_env, select, upsert

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = Path(BASE_DIR).parent.parent / ".env.local"

BUBBLE_BASE_URLS = {
    "test": "https://plataforma.tendenciaenergia.com.br/version-test/api/1.1",
    "live": "https://plataforma.tendenciaenergia.com.br/api/1.1",
}

DIAS_CHECAGEM_BASE = {20, 22, 25, 26, 27, 28, 29, 30, 31}
DIA_CRIACAO_CARD = 20  # dia (não ajustado) — card só na rodada de manhã desse dia


def _proximo_dia_util(d):
    while d.weekday() >= 5:  # 5=sábado, 6=domingo
        d += timedelta(days=1)
    return d


def _dia_efetivo(ano, mes, dia_original):
    """Dia real em que a checagem do `dia_original` acontece nesse mês —
    igual ao original, exceto se cair em fim de semana, aí desloca pro
    próximo dia útil. Retorna None se o mês não tem esse dia (ex.: 30/31
    em fevereiro)."""
    try:
        d = date(ano, mes, dia_original)
    except ValueError:
        return None
    return _proximo_dia_util(d)


def _dias_checagem_efetivos(ano, mes):
    return {d for d in (_dia_efetivo(ano, mes, dia) for dia in DIAS_CHECAGEM_BASE) if d}

# Coordenadores — recebem, em todo dia de checagem, 1 e-mail consolidado com
# as pendências de TODOS os gestores (separado por seção por gestor). Diogo
# também é gestor (recebe UCs em branco na planilha), então ele recebe 2
# e-mails no total: o consolidado abaixo + o individual dele (mesmo fluxo
# que qualquer outro gestor, no loop de `run()`).
COORDENADORES = [
    {"nome": "Felipe", "email": "felipe@tendenciaenergia.com.br"},
    {"nome": "Diogo Tavares", "email": "diogo.tavares@tendenciaenergia.com.br"},
]

# Julio (CEO) + financeiro — recebem, em todo dia de checagem, 1 e-mail
# consolidado só com a LISTA DE CLIENTES pendentes (sem separar por gestor,
# decisão do Leo 2026-08-20 — esse público não precisa ver quem é o gestor
# responsável, só quais clientes estão faltando).
DESTINATARIOS_FINANCEIRO = [
    {"nome": "Júlio", "email": "julio@tendenciaenergia.com.br"},
    {"nome": "Ana Alkimin", "email": "ana.alkimin@tendenciaenergia.com.br"},
    {"nome": "Fernanda Souza", "email": "fernanda.souza@tendenciaenergia.com.br"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("check_relatorios_pendentes")


def get_bubble_key(env_name):
    env = load_env()
    var = f"BUBBLE_API_KEY_{env_name.upper()}"
    key = env.get(var) or os.environ.get(var)
    if not key:
        raise SystemExit(f"ERRO: {var} não encontrada em .env.local")
    return key.strip()


def get_automacao_user_id(env_name):
    env = load_env()
    var = f"BUBBLE_AUTOMACAO_USER_ID_{env_name.upper()}"
    value = env.get(var) or os.environ.get(var)
    return value.strip() if value else value


def get_brevo_config():
    env = load_env()
    api_key = env.get("BREVO_API_KEY") or os.environ.get("BREVO_API_KEY")
    sender_email = env.get("BREVO_SENDER_EMAIL") or os.environ.get("BREVO_SENDER_EMAIL")
    sender_nome = env.get("BREVO_SENDER_NOME") or os.environ.get("BREVO_SENDER_NOME") or "Tendência Energia"
    if not api_key or not sender_email:
        raise SystemExit("ERRO: BREVO_API_KEY/BREVO_SENDER_EMAIL não encontrados em .env.local")
    return api_key.strip(), sender_email.strip(), sender_nome.strip()


def buscar_pendentes(mes, ano):
    """Retorna (pendentes, total_geral, totais_por_gestor, totais_por_tipo,
    totais_por_gestor_tipo) — os totais são de UCs ativas (recebidas +
    pendentes), usados pra estatística "restam X de Y (Z% enviado)" e pro
    resumo de entrega Total/Variável/Fixa nos e-mails."""
    ucs = select("ucs_gestor", filters={"ativo": "eq.true"})
    recebidos = select(
        "relatorios_recebidos",
        filters={"mes": f"eq.{mes}", "ano": f"eq.{ano}"},
    )
    por_uc = {r["uc_codigo"]: r for r in recebidos}

    totais_por_gestor = defaultdict(int)
    totais_por_tipo = defaultdict(int)
    totais_por_gestor_tipo = defaultdict(lambda: defaultdict(int))
    pendentes = []
    for uc in ucs:
        tipo = tipo_cobranca_label(uc)
        totais_por_gestor[uc["gestor_email"]] += 1
        totais_por_tipo[tipo] += 1
        totais_por_gestor_tipo[uc["gestor_email"]][tipo] += 1
        existente = por_uc.get(uc["uc_codigo"])
        if existente and existente.get("recebido"):
            continue
        uc = dict(uc)
        uc["_solicitacao_existente"] = existente.get("solicitacao_bubble_id") if existente else None
        pendentes.append(uc)
    return pendentes, len(ucs), totais_por_gestor, totais_por_tipo, totais_por_gestor_tipo


# Mesma identidade do brandbook (branding/brandbook.md) e do template usado
# em crm/solicitacoes/automacao/cobranca_diaria.py — header azul petróleo,
# selo verde, card branco. Mantém os e-mails do CRM visualmente consistentes.
def email_template(badge, heading, body_html):
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#F5F6FB;font-family:'Helvetica Neue',Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F6FB;padding:36px 16px;">
<tr><td align="center">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;background:#FFFFFF;border-radius:14px;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,0.08);">
  <tr><td style="background:#0B4A63;padding:22px 32px;">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="width:32px;height:32px;background:#24A77A;border-radius:8px;text-align:center;vertical-align:middle;font:800 13px Arial,sans-serif;color:#ffffff;">TE</td>
      <td style="padding-left:11px;color:#ffffff;font-size:14.5px;font-weight:700;">Tendência Energia</td>
    </tr></table>
  </td></tr>
  <tr><td style="padding:32px 32px 28px;">
    <div style="display:inline-block;background:#FFE0E0;color:#EF3E4A;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:5px 12px;border-radius:999px;margin-bottom:16px;">{badge}</div>
    <h1 style="margin:0 0 14px;font-size:19px;font-weight:700;color:#1F2430;line-height:1.35;">{heading}</h1>
    <div style="font-size:14px;line-height:1.65;color:#667085;">{body_html}</div>
  </td></tr>
  <tr><td style="padding:16px 32px;background:#F8FAFF;border-top:1px solid #E7ECF3;">
    <p style="margin:0;font-size:11.5px;color:#98A2B3;">Notificação automática — Relatório de Desempenho Mercado Livre. Não responda este e-mail.</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


# Valores brutos da coluna "Custo Serviço" da planilha original — trazida
# pra `ucs_gestor.custo_servico` em 2026-08-20 (migration_custo_servico.sql
# + import_custo_servico.py). Confirmado pelo Leo: "% sobre Economia" =
# cliente variável, "Valor Fixo" = cliente fixo.
TIPO_COBRANCA_LABEL = {
    "% sobre Economia": "Variável",
    "Valor Fixo": "Fixa",
    "Valor Fixo + % sobre Economia": "Híbrida",
    "Inexistente": "Sem custo definido",
}

# Variável sempre primeiro em qualquer lista/seção — cobrança variável só é
# emitida quando o relatório sai (diferente da fixa, que sai numa data fixa
# independente do relatório), então é o tipo mais urgente de destravar.
ORDEM_TIPO = ["Variável", "Fixa", "Híbrida", "Sem custo definido", "Não informado"]

TIPO_COR = {
    "Variável": "#EF3E4A",
    "Fixa": "#667085",
    "Híbrida": "#2E6FE0",
    "Sem custo definido": "#98A2B3",
    "Não informado": "#98A2B3",
}

TEXTO_EXPLICATIVO_TIPO = (
    '<p style="margin:0 0 16px;font-size:12px;color:#98A2B3;">'
    'UCs <b>Variável</b> aparecem primeiro: a cobrança delas só é emitida quando '
    'o relatório sai. UCs <b>Fixa</b> são cobradas numa data fixa, independente '
    'do relatório.</p>'
)


def tipo_cobranca_label(uc):
    return TIPO_COBRANCA_LABEL.get((uc.get("custo_servico") or "").strip(), "Não informado")


def _ordenar_por_tipo(ucs_pendentes):
    ordem_idx = {tipo: i for i, tipo in enumerate(ORDEM_TIPO)}
    return sorted(ucs_pendentes, key=lambda uc: ordem_idx.get(tipo_cobranca_label(uc), len(ORDEM_TIPO)))


def montar_uc_cards_html(ucs_pendentes, mostrar_gestor=False, mostrar_tipo=False):
    return "".join(
        f'<div style="border:1px solid #DDE4EE;border-radius:10px;padding:10px 14px;margin-bottom:8px;background:#FAFCFF;">'
        f'<span style="font-size:13.5px;font-weight:600;color:#1F2430;">{uc.get("cliente_nome") or "cliente não identificado"}</span>'
        + (
            f'<span style="float:right;font-size:10.5px;font-weight:700;'
            f'color:{TIPO_COR.get(tipo_cobranca_label(uc), "#98A2B3")};">{tipo_cobranca_label(uc)}</span>'
            if mostrar_tipo else ''
        )
        + f'<div style="font-size:12px;color:#667085;margin-top:3px;">UC {uc["uc_codigo"]}'
        + (f' · Gestor: <b>{uc["gestor_nome"]}</b>' if mostrar_gestor else '')
        + '</div></div>'
        for uc in ucs_pendentes
    )


def stats_texto(pendente, total):
    """"restam X de Y UC(s) (Z% enviado, W% faltando)" — pedido do Leo
    2026-08-20 pra dar noção de progresso do ciclo, não só a contagem crua."""
    recebido = total - pendente
    pct_recebido = (recebido / total * 100) if total else 0
    pct_pendente = 100 - pct_recebido
    return f"restam {pendente} de {total} UC(s) ({pct_recebido:.0f}% enviado, {pct_pendente:.0f}% faltando)"


def stats_entrega_texto(label, pendente, total):
    """"Label: X de Y (P% entregue, Q% pendente)" — 1 linha do resumo de
    entrega (pedido do Leo 2026-08-27, reformulação do resumo anterior:
    agora fala em "entregue" em vez de "restam", mais direto)."""
    entregues = total - pendente
    pct_entregue = (entregues / total * 100) if total else 0
    pct_pendente = 100 - pct_entregue
    return f"{label}: {entregues} de {total} ({pct_entregue:.0f}% entregue, {pct_pendente:.0f}% pendente)"


def resumo_entrega_html(total_geral, pendente_geral, totais_tipo, pendentes_tipo):
    """3 linhas no heading do email: Total, Variável, Fixa — cada uma com
    contagem absoluta e %. Variável/Fixa aparecem sempre, mesmo com 0
    entregue/0 pendente, pra não sumir uma categoria da leitura. Híbrida e
    "sem custo definido" não ganham linha própria (pedido do Leo foi só
    Total/Variável/Fixa) mas continuam contados dentro do Total."""
    linhas = [stats_entrega_texto("Total de relatórios entregues", pendente_geral, total_geral)]
    for tipo, label in (("Variável", "Relatórios variáveis entregues"), ("Fixa", "Relatórios fixos entregues")):
        linhas.append(stats_entrega_texto(label, pendentes_tipo.get(tipo, 0), totais_tipo.get(tipo, 0)))
    return (
        '<div style="margin-top:10px;font-size:12.5px;font-weight:400;color:#475467;line-height:1.8;">'
        + "<br/>".join(linhas)
        + "</div>"
    )


def montar_secao_gestor_html(gestor_nome, ucs_pendentes, total_gestor):
    return (
        f'<p style="margin:18px 0 8px;">'
        f'<span style="display:inline-block;background:#E7F7F0;color:#1F946D;font-size:11px;font-weight:700;'
        f'padding:3px 10px;border-radius:999px;">{gestor_nome} ({len(ucs_pendentes)})</span>'
        f'<span style="margin-left:8px;font-size:11.5px;color:#98A2B3;">{stats_texto(len(ucs_pendentes), total_gestor)}</span></p>'
        f'{montar_uc_cards_html(_ordenar_por_tipo(ucs_pendentes), mostrar_tipo=True)}'
    )


def montar_secao_tipo_html(tipo_label, ucs_pendentes):
    return (
        f'<p style="margin:18px 0 8px;">'
        f'<span style="display:inline-block;background:#E7F7F0;color:#1F946D;font-size:11px;font-weight:700;'
        f'padding:3px 10px;border-radius:999px;">{tipo_label} ({len(ucs_pendentes)})</span></p>'
        f'{montar_uc_cards_html(ucs_pendentes)}'
    )


def montar_corpo_por_tipo_html(pendentes):
    """Agrupa UCs por tipo de cobrança (Variável/Fixa/Híbrida), Variável
    sempre primeiro — reaproveitado no email do gestor individual e no do
    financeiro."""
    por_tipo = defaultdict(list)
    for uc in pendentes:
        por_tipo[tipo_cobranca_label(uc)].append(uc)
    secoes = "".join(
        montar_secao_tipo_html(tipo, por_tipo[tipo])
        for tipo in ORDEM_TIPO
        if por_tipo.get(tipo)
    )
    return TEXTO_EXPLICATIVO_TIPO + secoes


def enviar_email(to_nome, to_email, assunto, corpo_html):
    api_key, sender_email, sender_nome = get_brevo_config()
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": api_key, "Content-Type": "application/json", "accept": "application/json"},
        json={
            "sender": {"name": sender_nome, "email": sender_email},
            "to": [{"email": to_email, "name": to_nome}],
            "subject": assunto,
            "htmlContent": corpo_html,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def enviar_email_brevo(gestor_nome, gestor_email, ucs_pendentes, mes, ano, total_gestor, totais_gestor_tipo):
    pendentes_tipo = Counter(tipo_cobranca_label(uc) for uc in ucs_pendentes)
    heading = (
        f"Olá, {gestor_nome}! Relatório de {mes:02d}/{ano}."
        + resumo_entrega_html(total_gestor, len(ucs_pendentes), totais_gestor_tipo, pendentes_tipo)
    )
    html = email_template("Relatório pendente", heading, montar_corpo_por_tipo_html(ucs_pendentes))
    return enviar_email(
        gestor_nome, gestor_email,
        f"Relatório de desempenho pendente — {len(ucs_pendentes)} UC(s) — {mes:02d}/{ano}",
        html,
    )


def enviar_email_consolidado_brevo(to_nome, to_email, por_gestor, mes, ano, totais_por_gestor, total_geral, totais_por_tipo):
    total_pendente = sum(len(ucs) for ucs in por_gestor.values())
    todas_ucs_pendentes = [uc for ucs in por_gestor.values() for uc in ucs]
    pendentes_tipo = Counter(tipo_cobranca_label(uc) for uc in todas_ucs_pendentes)
    heading = (
        f"Olá, {to_nome}! Resumo consolidado — {mes:02d}/{ano}, por gestor."
        + resumo_entrega_html(total_geral, total_pendente, totais_por_tipo, pendentes_tipo)
    )
    corpo = TEXTO_EXPLICATIVO_TIPO + "".join(
        montar_secao_gestor_html(ucs[0]["gestor_nome"], ucs, totais_por_gestor.get(gestor_email, len(ucs)))
        for gestor_email, ucs in por_gestor.items()
    )
    html = email_template("Relatório pendente — consolidado", heading, corpo)
    return enviar_email(
        to_nome, to_email,
        f"Relatório de desempenho pendente — consolidado ({total_pendente} UC(s), todos os gestores) — {mes:02d}/{ano}",
        html,
    )


def enviar_email_financeiro_brevo(to_nome, to_email, pendentes, mes, ano, total_geral, totais_por_tipo):
    """Igual ao consolidado dos coordenadores, mas SEM separar por gestor —
    Julio/financeiro só precisam saber quais clientes estão pendentes, não
    quem é o gestor responsável (decisão do Leo, 2026-08-20). Separado por
    tipo de cobrança (Variável/Fixa/Híbrida) em vez disso — pedido do Leo
    no mesmo dia, depois de descobrir que a planilha original já tinha
    essa info (coluna "Custo Serviço", nunca importada pro Supabase até
    então — ver `import_custo_servico.py`)."""
    corpo = montar_corpo_por_tipo_html(pendentes)
    pendentes_tipo = Counter(tipo_cobranca_label(uc) for uc in pendentes)
    heading = (
        f"Olá, {to_nome}! Relatório de desempenho — {mes:02d}/{ano}."
        + resumo_entrega_html(total_geral, len(pendentes), totais_por_tipo, pendentes_tipo)
    )
    html = email_template("Relatório pendente", heading, corpo)
    return enviar_email(
        to_nome, to_email,
        f"Relatório de desempenho pendente — {len(pendentes)} UC(s) — {mes:02d}/{ano}",
        html,
    )


def _ultimo_dia_do_mes_atual():
    hoje = date.today()
    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    return datetime(hoje.year, hoje.month, ultimo_dia, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def criar_solicitacao_bubble(env_name, uc, mes, ano):
    base_url = BUBBLE_BASE_URLS[env_name]
    key = get_bubble_key(env_name)
    automacao_user_id = get_automacao_user_id(env_name)
    cliente_nome = uc.get("cliente_nome") or "cliente não identificado"

    payload = {
        "Titulo": f"Relatório de desempenho pendente — {cliente_nome} (UC {uc['uc_codigo']})",
        "Descricao": (
            f"UC {uc['uc_codigo']} (cliente: {cliente_nome}) sem relatório "
            f"de desempenho de {mes:02d}/{ano}. Gestor: {uc['gestor_nome']} ({uc['gestor_email']})."
        ),
        "Status": "Solicitado",
        "Tipo_Responsavel": "Pessoa",
        "Responsavel_Departamento": "Gestão",
        "data_solicitada": datetime.now(timezone.utc).isoformat(),
        "data entrega": _ultimo_dia_do_mes_atual(),
        "veio_do_cliente": False,
    }
    if uc.get("gestor_bubble_user_id"):
        payload["Responsavel_Pessoa"] = uc["gestor_bubble_user_id"]
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


def run(env_name="test", turno="manha", force=False, dry_run=False):
    today = date.today()
    dias_efetivos = _dias_checagem_efetivos(today.year, today.month)
    if not force and today not in dias_efetivos:
        log.info(
            "dia %s não é dia de checagem efetivo esse mês (%s) — nada a fazer (use --force pra ignorar)",
            today, sorted(dias_efetivos),
        )
        return

    mes = today.month - 1 or 12
    ano = today.year if today.month > 1 else today.year - 1
    pendentes, total_geral, totais_por_gestor, totais_por_tipo, totais_por_gestor_tipo = buscar_pendentes(mes, ano)
    log.info("%d UCs pendentes de relatório em %02d/%d (turno=%s)", len(pendentes), mes, ano, turno)

    if not pendentes:
        return

    por_gestor = defaultdict(list)
    for uc in pendentes:
        por_gestor[uc["gestor_email"]].append(uc)

    dia_20_efetivo = _dia_efetivo(today.year, today.month, DIA_CRIACAO_CARD)
    cria_solicitacao = turno == "manha" and today == dia_20_efetivo
    total_notificados = total_solicitacoes = 0

    for gestor_email, ucs_pendentes in por_gestor.items():
        gestor_nome = ucs_pendentes[0]["gestor_nome"]

        if dry_run:
            log.info(
                "[DRY-RUN] notificaria %s <%s> sobre %d UC(s) (%s): %s",
                gestor_nome, gestor_email, len(ucs_pendentes),
                "email + Solicitacao" if cria_solicitacao else "só email",
                [uc["uc_codigo"] for uc in ucs_pendentes],
            )
            continue

        enviar_email_brevo(
            gestor_nome, gestor_email, ucs_pendentes, mes, ano,
            totais_por_gestor.get(gestor_email, len(ucs_pendentes)),
            totais_por_gestor_tipo.get(gestor_email, {}),
        )
        total_notificados += 1

        rows_upsert = []
        for uc in ucs_pendentes:
            row = {
                "uc_codigo": uc["uc_codigo"],
                "mes": mes,
                "ano": ano,
                "recebido": False,
                "notificado_em": datetime.now(timezone.utc).isoformat(),
            }
            if cria_solicitacao and not uc.get("_solicitacao_existente"):
                row["solicitacao_bubble_id"] = criar_solicitacao_bubble(env_name, uc, mes, ano)
                total_solicitacoes += 1
            rows_upsert.append(row)
        upsert("relatorios_recebidos", rows_upsert, on_conflict="uc_codigo,mes,ano")

    total_consolidados = 0
    for coord in COORDENADORES:
        if dry_run:
            log.info(
                "[DRY-RUN] mandaria consolidado pra %s <%s> sobre %d gestor(es), %d UC(s) no total",
                coord["nome"], coord["email"], len(por_gestor), sum(len(u) for u in por_gestor.values()),
            )
            continue
        enviar_email_consolidado_brevo(coord["nome"], coord["email"], por_gestor, mes, ano, totais_por_gestor, total_geral, totais_por_tipo)
        total_consolidados += 1

    total_financeiro = 0
    for dest in DESTINATARIOS_FINANCEIRO:
        if dry_run:
            log.info(
                "[DRY-RUN] mandaria (sem gestor) pra %s <%s> sobre %d UC(s) no total",
                dest["nome"], dest["email"], len(pendentes),
            )
            continue
        enviar_email_financeiro_brevo(dest["nome"], dest["email"], pendentes, mes, ano, total_geral, totais_por_tipo)
        total_financeiro += 1

    log.info(
        "resumo: %d UCs pendentes, %d gestores notificados, %d Solicitacoes criadas, %d consolidados enviados, %d financeiro/Julio enviados",
        len(pendentes), total_notificados, total_solicitacoes, total_consolidados, total_financeiro,
    )


def main():
    parser = argparse.ArgumentParser(description="Verifica UCs sem relatório de desempenho e notifica gestores")
    parser.add_argument("--env", choices=["test", "live"], default="test", help="Ambiente Bubble (default: test)")
    parser.add_argument(
        "--turno", choices=["manha", "tarde"], default="manha",
        help="Rodada do dia — só a de manhã do dia 20 cria Solicitacao no Bubble (default: manha)",
    )
    parser.add_argument("--force", action="store_true", help="Roda mesmo fora dos dias de checagem")
    parser.add_argument("--dry-run", action="store_true", help="Não envia email nem escreve nada, só mostra o que faria")
    args = parser.parse_args()
    run(env_name=args.env, turno=args.turno, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
