"""
Cliente Supabase mínimo (REST/PostgREST) pro módulo relatorio-desempenho.
Projeto czqvenpkfsbepggdktfn — mesmo do simple-uno, mas tabelas próprias
(ucs_gestor, relatorios_recebidos), sem tocar nas tabelas do motor de auditoria.
"""

import os
from pathlib import Path

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = Path(BASE_DIR).parent.parent / ".env.local"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _config():
    env = load_env()
    url = env.get("SUPABASE_RELATORIO_URL") or os.environ.get("SUPABASE_RELATORIO_URL")
    key = env.get("SUPABASE_RELATORIO_SERVICE_KEY") or os.environ.get("SUPABASE_RELATORIO_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("ERRO: SUPABASE_RELATORIO_URL/SUPABASE_RELATORIO_SERVICE_KEY não encontrados em .env.local")
    # .strip() defende contra secret do GitHub Actions colado com quebra de
    # linha sobrando no final (copiar de um bloco de código no navegador às
    # vezes traz o \n junto) — sem isso vira %0a colado na URL/host.
    return url.strip().rstrip("/"), key.strip()


def _headers(key, prefer=None):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def select(table, filters=None, select_cols="*"):
    """filters: dict de {coluna: 'eq.valor'} no formato de query PostgREST."""
    url, key = _config()
    params = {"select": select_cols}
    if filters:
        params.update(filters)
    resp = requests.get(f"{url}/rest/v1/{table}", headers=_headers(key), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upsert(table, rows, on_conflict):
    """Insere ou atualiza (merge) por on_conflict (nome da coluna/constraint única)."""
    if not rows:
        return []
    url, key = _config()
    headers = _headers(key, prefer="resolution=merge-duplicates,return=representation")
    resp = requests.post(
        f"{url}/rest/v1/{table}",
        headers=headers,
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
