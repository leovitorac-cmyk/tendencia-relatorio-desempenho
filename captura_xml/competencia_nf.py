"""DSR-2.1 · subtarefa 4 · competência (mês de referência) de uma NF de geradora.

Regra do Leo (28/09/2026): ler o mês que a NF indica; havendo vários, vale o que mais aparece; se nada indicar,
"sem indicação". Fontes, nesta ordem (a primeira que indicar vence): texto complementar do XML (infCpl), assunto do
e-mail, prévia do corpo do e-mail. O nome do arquivo não entra: é só a chave da NF (24 de 24 conferidos).

Módulo puro (sem rede, sem banco), usado por leitor_outlook.py e por preencher_competencia.py.
"""

import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date

NOMES = {
    "janeiro": 1, "jan": 1, "fevereiro": 2, "fev": 2, "março": 3, "marco": 3, "mar": 3, "abril": 4, "abr": 4,
    "maio": 5, "mai": 5, "junho": 6, "jun": 6, "julho": 7, "jul": 7, "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9, "outubro": 10, "out": 10, "novembro": 11, "nov": 11, "dezembro": 12, "dez": 12,
}
# 8/2026, 08/2026, 08-2026 (não pega dia/mês/ano: o que vem antes não pode ser dígito, "/" ou "-").
NUMERICO = re.compile(r"(?<![\d/\-])(0?[1-9]|1[0-2])\s*[/\-]\s*(20\d{2})(?!\d)")
# agosto/2026, ago-26, agosto de 2026, ago 2026
NOMINAL = re.compile(
    r"\b(" + "|".join(sorted(NOMES, key=len, reverse=True)) + r")\b\.?\s*(?:de\s+|[/\-]\s*)?(20\d{2}|\d{2})(?!\d)",
    re.IGNORECASE,
)


def _meses_no_texto(texto):
    achados = []
    for m, a in NUMERICO.findall(texto or ""):
        achados.append((int(a), int(m)))
    for nome, a in NOMINAL.findall(texto or ""):
        ano = int(a) if len(a) == 4 else 2000 + int(a)
        achados.append((ano, NOMES[nome.lower()]))
    return achados


def _plausivel(ano_mes, emissao):
    """Descarta lixo (ex.: número de decreto): de 12 meses antes até 1 mês depois da emissão."""
    if emissao is None:
        return 2020 <= ano_mes[0] <= 2035
    a, m = ano_mes
    dist = (a * 12 + m) - (emissao.year * 12 + emissao.month)
    return -12 <= dist <= 1


def competencia_do_texto(texto, emissao=None):
    """(AAAA-MM ou None, nº de ocorrências do vencedor, empate?). Empate no mais frequente devolve None."""
    vistos = [x for x in _meses_no_texto(texto) if _plausivel(x, emissao)]
    if not vistos:
        return None, 0, False
    (a, m), n = Counter(vistos).most_common(1)[0]
    if sum(1 for _, k in Counter(vistos).items() if k == n) > 1:
        return None, n, True
    return f"{a:04d}-{m:02d}", n, False


def infcpl_do_xml(conteudo):
    """Texto de <infAdic><infCpl> (e infAdFisco) de uma NF-e, sem namespace. Vazio se não houver."""
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError:
        return ""
    textos = []
    for el in raiz.iter():
        if el.tag.rsplit("}", 1)[-1] in ("infCpl", "infAdFisco") and el.text:
            textos.append(el.text)
    return "\n".join(textos)


def competencia_da_nf(conteudo_xml, emissao=None, assunto=None, corpo=None):
    """Aplica as fontes em ordem. Devolve dict: competencia (AAAA-MM ou None), origem ('xml','assunto','corpo' ou None),
    ocorrencias, empate. `emissao` é date (ou None)."""
    for origem, texto in (("xml", infcpl_do_xml(conteudo_xml) if conteudo_xml else ""), ("assunto", assunto), ("corpo", corpo)):
        comp, n, empate = competencia_do_texto(texto, emissao)
        if comp or empate:
            return {"competencia": comp, "origem": origem, "ocorrencias": n, "empate": empate}
    return {"competencia": None, "origem": None, "ocorrencias": 0, "empate": False}


if __name__ == "__main__":
    # Autoteste mínimo, sem rede.
    e = date(2026, 9, 17)
    casos = [
        ("referencia: [8/2026] ... Decreto 7212/2010 ... Lei 14/2022", "2026-08"),
        ("Consumo ref. 07/2026 e 07/2026, parcela 06/2026", "2026-07"),
        ("Vencimento 15/08/2026", None),
        ("Fatura de agosto de 2026", "2026-08"),
        ("ago/26", "2026-08"),
        ("junho/2026 e julho/2026", None),  # empate
        ("Decreto 7212/2010 art. 245", None),
    ]
    for texto, esperado in casos:
        comp, _, _ = competencia_do_texto(texto, e)
        print("OK " if comp == esperado else "ERRO", repr(texto)[:60], "->", comp)
