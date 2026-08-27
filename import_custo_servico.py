"""
Popula `ucs_gestor.custo_servico` a partir da planilha original
("solicitação-ucs" — a mesma usada na importação inicial de uc_codigo/
gestor, coluna "Custo Serviço" nunca tinha sido trazida pro Supabase).
Faz UPDATE (não upsert/insert) agrupado por valor de custo_servico — as
810 UCs já existem em `ucs_gestor`, só falta preencher esse campo.

Requer rodar antes `migration_custo_servico.sql` no Supabase (adiciona a
coluna) — sem isso o update falha com "column does not exist".

Uso:
  python3 import_custo_servico.py --csv "caminho/solicitação-ucs - Worksheet.csv" --dry-run
  python3 import_custo_servico.py --csv "caminho/solicitação-ucs - Worksheet.csv"
"""

import argparse
import csv
import logging
from collections import defaultdict

from supabase_client import update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_custo_servico")

LOTE = 100  # tamanho do filtro "in.(...)" por chamada, pra não estourar limite de URL


def load_rows(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for line in reader:
            uc_codigo = (line.get("UC") or "").strip()
            custo = (line.get("Custo Serviço") or "").strip()
            if not uc_codigo or not custo:
                continue
            rows.append({"uc_codigo": uc_codigo, "custo_servico": custo})
        return rows


def run(csv_path, dry_run=False):
    rows = load_rows(csv_path)
    log.info("%d UCs com custo_servico lidas de %s", len(rows), csv_path)

    por_custo = defaultdict(list)
    for row in rows:
        por_custo[row["custo_servico"]].append(row["uc_codigo"])

    if dry_run:
        for custo, codigos in por_custo.items():
            log.info("[DRY-RUN] %d UC(s) com custo_servico=%r — ex.: %s", len(codigos), custo, codigos[:3])
        return

    total_chamadas = 0
    for custo, codigos in por_custo.items():
        for i in range(0, len(codigos), LOTE):
            lote = codigos[i:i + LOTE]
            filtro = "in.(" + ",".join(lote) + ")"
            update("ucs_gestor", {"uc_codigo": filtro}, {"custo_servico": custo})
            total_chamadas += 1
    log.info("update concluído: %d UCs em %d chamada(s) (agrupadas por valor de custo_servico)", len(rows), total_chamadas)


def main():
    parser = argparse.ArgumentParser(description="Popula ucs_gestor.custo_servico a partir da planilha original")
    parser.add_argument("--csv", required=True, help="Caminho do CSV original (solicitação-ucs)")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve no Supabase, só mostra o que faria")
    args = parser.parse_args()
    run(args.csv, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
