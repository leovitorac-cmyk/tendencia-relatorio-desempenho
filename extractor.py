"""
Extrai, de um anexo de relatório de desempenho (PDF ou Excel), a lista de UCs
cobertas e o mês/ano de referência.

Validado em 2026-08-10 contra PDFs reais da caixa "aws" (single-UC e multi-UC,
várias distribuidoras, com e sem CNPJ na linha). A UC aparece numa linha
curta no formato `<identificação, pode ter vários segmentos com "-"> - <UC>`,
ex.:
  "09.610.618/0001-48 - SPR INDUSTRIA - MTE0014489"                    (Enel, com CNPJ, alfanumérica)
  "10.800.180/0001-40 - SALTO PLAZA - 4000176346"                      (CPFL, com CNPJ, só dígitos)
  "05.035.532/0001-88 - MATRIZ - LONDRINA - 56712324"                  (Copel, nome com 2 segmentos)
  "05.035.532/0009-35 - JOINVILLE - 45196739 - JOINVILLE"              (Celesc, nome repetido DEPOIS do código)
  "PROMETAL ESTRUTURAS METALICAS LTDA - 9006997"                       (sem CNPJ, só nome — código)

Não dá pra assumir nem "sempre tem CNPJ" nem "código = último segmento" (o
exemplo Celesc acima quebra a segunda regra — o nome da filial se repete
depois do código). A regra que bateu em todos os casos reais vistos: o
código é, dentre os segmentos separados por " - " numa linha curta, o
último que não tem espaço e contém pelo menos 1 dígito — nome de filial
sempre tem espaço (ex. "SANTA MONICA", "CJ 2") ou é só letras (ex.
"JOINVILLE", "VALESUL"); o código nunca tem espaço, mesmo com pontuação
interna ("10/3147655-9", "1.907.461.011-17").

O mês/ano do anexo NÃO é usado como fonte confiável: o texto do PDF pode ter
outras datas (ex. "perfil CL - I5 - SE/CO (04/2026)") que não são o período
do relatório. `watch_relatorio_desempenho.py` usa o **assunto do email**
para isso (formato confirmado real: "Relatório de Desempenho Mercado Livre
<Mês>/<Ano> | <CLIENTE>", ex.: "Janeiro/2026") via `extract_mes_ano()`.
`extract_report()` ainda tenta achar mês/ano no PDF como último recurso, mas
não deve ser a fonte principal.

Dependências: pip3 install pdfplumber openpyxl
"""

import re

SEGMENT_SPLIT_PATTERN = re.compile(r"\s+-\s+")
MAX_LINHA_IDENTIFICACAO = 150  # linha de identificação de UC é curta; descarta parágrafos


MIN_TAMANHO_CODIGO = 5  # descarta abreviações alfanuméricas tipo "I5" (perfil de carga)
MIN_TAMANHO_CODIGO_NUMERICO = 3  # UC 100% numérica pode ser bem curta de verdade
CNPJ_SHAPE = re.compile(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$")  # linha pode citar o CNPJ do cliente à parte, isso não é UC


def _looks_like_code(segment):
    """Bug real descoberto 2026-08-20: BCS RESTAURANTE ("2220") e LETOM
    MOTEL ("270") têm UC de 3-4 dígitos — o filtro antigo (mínimo 5
    caracteres pra qualquer coisa) descartava a linha inteira sem log
    nenhum, achando que era abreviação tipo "I5" (perfil de carga, sempre
    letra+dígito). Diferença: código 100% numérico pode ser curto de
    verdade; abreviação alfanumérica tipo "I5"/"A4" não passa de 2
    caracteres — mínimo mais baixo só pra dígito puro não reabre esse
    problema."""
    if CNPJ_SHAPE.match(segment):
        return False
    if " " in segment or not any(c.isdigit() for c in segment):
        return False
    if segment.isdigit():
        return len(segment) >= MIN_TAMANHO_CODIGO_NUMERICO
    return len(segment) >= MIN_TAMANHO_CODIGO


CODE_CITY_GLUED_PATTERN = re.compile(r"^([\w./]+?)-([A-ZÀ-Ü][A-ZÀ-Ü\s]*)$")


def _strip_glued_city_suffix(segment):
    """Alguns PDFs colam a UC direto no nome da cidade sem espaço em volta
    do hífen (ex.: "204803095-VARGEM GRANDE PAULISTA", "200964261-COTIA")
    — diferente de hífen que É parte do código (ex. "0616541-9", dígito
    verificador — sempre número dos 2 lados). Se o que vem depois do hífen
    começa com letra maiúscula, é nome de lugar, não parte do código.

    Bug real descoberto 2026-08-20: cidade de 1 palavra só ("COTIA") ainda
    colava com o código mas casava por acidente no fallback só-dígitos;
    cidade de 2+ palavras ("VARGEM GRANDE PAULISTA") tinha espaço, falhava
    `_looks_like_code` e a linha inteira era descartada — UC sumia sem
    nenhum log, nem virava "desconhecida"."""
    m = CODE_CITY_GLUED_PATTERN.match(segment)
    if m:
        return m.group(1)
    return segment

MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}
MES_ANO_PATTERN = re.compile(r"\b(\d{1,2})[/\-](\d{4})\b")
MES_NOME_ANO_PATTERN = re.compile(
    r"\b(" + "|".join(MESES_PT) + r")\s*(?:/|de|-)?\s*(\d{4})\b", re.IGNORECASE
)


def _extract_text(file_path):
    file_path = str(file_path)
    ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else ""

    if ext == "pdf":
        import pdfplumber

        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)

    if ext in ("xlsx", "xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(file_path, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                parts.append(" ".join(str(c) for c in row if c is not None))
        return "\n".join(parts)

    if ext == "csv":
        with open(file_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    with open(file_path, "rb") as f:
        return f.read().decode("utf-8", errors="ignore")


def extract_mes_ano(text):
    """Extrai (mes, ano) de um texto — usar preferencialmente com o assunto
    do email (formato real: "... Mercado Livre Janeiro/2026 | ...")."""
    m = MES_ANO_PATTERN.search(text)
    if m:
        mes, ano = int(m.group(1)), int(m.group(2))
        if 1 <= mes <= 12:
            return mes, ano

    m = MES_NOME_ANO_PATTERN.search(text)
    if m:
        mes = MESES_PT[m.group(1).lower()]
        ano = int(m.group(2))
        return mes, ano

    return None, None


def extract_report(file_path):
    """Retorna {"ucs": [str, ...], "mes": int|None, "ano": int|None,
    "identificacoes": [{"uc", "nome", "cnpj"}, ...]}. UCs deduplicadas,
    mantendo ordem de aparição.

    "identificacoes" guarda nome/CNPJ junto com cada UC pra permitir
    fallback de matching (o código impresso no PDF às vezes não bate com o
    cadastrado em `ucs_gestor` — cliente com conta renumerada pela
    distribuidora, por exemplo — mas o nome/CNPJ seguem batendo)."""
    text = _extract_text(file_path)

    seen = set()
    ucs = []
    identificacoes = []
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > MAX_LINHA_IDENTIFICACAO or " - " not in line:
            continue
        segments = [s.strip() for s in SEGMENT_SPLIT_PATTERN.split(line)]
        if len(segments) < 2:
            continue
        segments = [_strip_glued_city_suffix(s) for s in segments]
        candidatos = [s for s in segments if _looks_like_code(s)]
        if not candidatos:
            continue
        code = candidatos[-1]
        if code not in seen:
            seen.add(code)
            ucs.append(code)
            cnpj = next((s for s in segments if CNPJ_SHAPE.match(s)), None)
            nome_segs = [s for s in segments if s != code and s != cnpj]
            nome = " ".join(nome_segs) if nome_segs else None
            identificacoes.append({"uc": code, "nome": nome, "cnpj": cnpj})

    mes, ano = extract_mes_ano(text)
    return {"ucs": ucs, "mes": mes, "ano": ano, "identificacoes": identificacoes}
