"""
Importa a planilha de UCs + gestor pra tabela `ucs_gestor` (Supabase).
Upsert por `uc_codigo` — rodar de novo sempre que a planilha mudar.

Colunas esperadas no CSV: uc_codigo, cliente_nome, gestor_nome, gestor_email
Coluna opcional: gestor_bubble_user_id

Uso:
  python3 import_ucs_gestor.py --csv ucs.csv              # roda de verdade
  python3 import_ucs_gestor.py --csv ucs.csv --dry-run    # só mostra o que faria
"""

import argparse
import csv
import logging

from supabase_client import upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_ucs_gestor")

REQUIRED_COLS = ["uc_codigo", "cliente_nome", "gestor_nome", "gestor_email"]


def load_rows(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"ERRO: CSV sem colunas obrigatórias: {missing}. Colunas encontradas: {reader.fieldnames}")

        rows = []
        for line in reader:
            uc_codigo = (line.get("uc_codigo") or "").strip()
            if not uc_codigo:
                continue
            row = {
                "uc_codigo": uc_codigo,
                "cliente_nome": (line.get("cliente_nome") or "").strip() or None,
                "gestor_nome": (line.get("gestor_nome") or "").strip(),
                "gestor_email": (line.get("gestor_email") or "").strip(),
                "ativo": True,
            }
            bubble_id = (line.get("gestor_bubble_user_id") or "").strip()
            if bubble_id:
                row["gestor_bubble_user_id"] = bubble_id
            rows.append(row)
        return rows


def run(csv_path, dry_run=False):
    rows = load_rows(csv_path)
    log.info("%d UCs lidas de %s", len(rows), csv_path)

    if dry_run:
        for row in rows[:10]:
            log.info("[DRY-RUN] %s", row)
        if len(rows) > 10:
            log.info("[DRY-RUN] ... e mais %d linhas", len(rows) - 10)
        return

    result = upsert("ucs_gestor", rows, on_conflict="uc_codigo")
    log.info("upsert concluído: %d linhas gravadas em ucs_gestor", len(result))


def main():
    parser = argparse.ArgumentParser(description="Importa planilha de UCs + gestor pro Supabase")
    parser.add_argument("--csv", required=True, help="Caminho do CSV")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve no Supabase, só mostra o que faria")
    args = parser.parse_args()
    run(args.csv, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
