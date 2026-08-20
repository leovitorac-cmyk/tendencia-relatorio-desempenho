"""
Sincroniza um arquivo .html local com a tabela Bubble `Claude` (campos
Nome/Html) — mesmo padrão usado pra `fechamento-mensal` (Simple). A página
Bubble só precisa ter um elemento HTML com "source" = Search Claude
(Nome = <nome>) :first item's Html — o conteúdo em si vive aqui no repo,
versionado, e é empurrado pro Bubble sempre que houver ajuste.

Uso:
  python3 sync_html_claude.py justificar-atraso.html "Justificativa Atraso" --env test
  python3 sync_html_claude.py justificar-atraso.html "Justificativa Atraso" --env live
  python3 sync_html_claude.py justificar-atraso.html "Justificativa Atraso" --env test --env live
"""

import argparse
import json

import requests

from check_relatorios_pendentes import BUBBLE_BASE_URLS, get_bubble_key

logging_prefix = "sync_html_claude"


def buscar_registro(env_name, nome):
    base_url = BUBBLE_BASE_URLS[env_name]
    key = get_bubble_key(env_name)
    constraints = [{"key": "Nome", "constraint_type": "equals", "value": nome}]
    resp = requests.get(
        f"{base_url}/obj/claude",
        headers={"Authorization": f"Bearer {key}"},
        params={"constraints": json.dumps(constraints)},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()["response"]["results"]
    return results[0] if results else None


def sincronizar(env_name, nome, html):
    base_url = BUBBLE_BASE_URLS[env_name]
    key = get_bubble_key(env_name)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    existente = buscar_registro(env_name, nome)
    if existente:
        resp = requests.patch(
            f"{base_url}/obj/claude/{existente['_id']}",
            headers=headers,
            json={"Html": html},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[{env_name}] atualizado: {existente['_id']}")
    else:
        resp = requests.post(
            f"{base_url}/obj/claude",
            headers=headers,
            json={"Nome": nome, "Html": html},
            timeout=30,
        )
        resp.raise_for_status()
        novo_id = resp.json().get("id")
        print(f"[{env_name}] criado: {novo_id}")


def main():
    parser = argparse.ArgumentParser(description="Sincroniza .html local com registro Bubble Claude")
    parser.add_argument("arquivo", help="Caminho do arquivo .html")
    parser.add_argument("nome", help="Valor do campo Nome no registro Claude")
    parser.add_argument("--env", choices=["test", "live"], action="append", required=True, dest="envs")
    args = parser.parse_args()

    with open(args.arquivo, "r", encoding="utf-8") as f:
        html = f.read()

    for env_name in args.envs:
        sincronizar(env_name, args.nome, html)


if __name__ == "__main__":
    main()
