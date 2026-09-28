"""DSR-0.3 · prova de acesso do app à caixa dos XMLs via Microsoft Graph (somente leitura).

Lê do .env.local: MS_DSR_TENANT_ID, MS_DSR_CLIENT_ID, MS_DSR_CLIENT_SECRET, MS_DSR_MAILBOX.
(Prefixo MS_DSR_ porque MS_CLIENT_ID e MS_TENANT_ID já existem no .env.local, de outra
integração: não sobrescrever.)

Obtém token por client credentials e faz um GET na caixa. Imprime só o status HTTP e a
contagem; nunca o token, o segredo ou o conteúdo dos e-mails.

  PASSA: 200 na caixa dos XMLs e 403 em --outra-caixa.
  401: segredo ou tenant errado. 403 na caixa certa: política não propagou ou falta consentimento.

Uso:
  python3 simple/dashboard-relatorios/captura/graph_teste.py
  python3 simple/dashboard-relatorios/captura/graph_teste.py --outra-caixa alguem@tendenciaenergia.com.br
"""

import argparse
import os
import sys
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parents[3] / ".env.local"
CHAVES = ["MS_DSR_TENANT_ID", "MS_DSR_CLIENT_ID", "MS_DSR_CLIENT_SECRET", "MS_DSR_MAILBOX"]
GRAPH = "https://graph.microsoft.com/v1.0"


def ler_env():
    env = {}
    if ENV_PATH.exists():
        for linha in ENV_PATH.read_text().splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            env[chave.strip()] = valor.strip().strip('"').strip("'")
    # No GitHub Actions não há .env.local: os segredos chegam como variáveis de ambiente.
    # O arquivo continua valendo primeiro; o ambiente só completa o que faltar.
    for chave in (*CHAVES, "SUPABASE_RELATORIO_URL", "SUPABASE_RELATORIO_SERVICE_KEY"):
        if not env.get(chave) and os.environ.get(chave):
            env[chave] = os.environ[chave].strip()
    faltando = [c for c in CHAVES if not env.get(c)]
    if faltando:
        sys.exit(f"Faltam no .env.local ou no ambiente: {', '.join(faltando)}")
    return env


def obter_token(env):
    url = f"https://login.microsoftonline.com/{env['MS_DSR_TENANT_ID']}/oauth2/v2.0/token"
    r = requests.post(
        url,
        data={
            "client_id": env["MS_DSR_CLIENT_ID"],
            "client_secret": env["MS_DSR_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Token: HTTP {r.status_code} (401/400 = segredo, client ou tenant errado)")
    return r.json()["access_token"]


def testar_caixa(token, caixa):
    url = f"{GRAPH}/users/{caixa}/mailFolders/inbox/messages?$top=1&$select=id,subject"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    contagem = len(r.json().get("value", [])) if r.status_code == 200 else "-"
    print(f"{caixa}: HTTP {r.status_code}, mensagens retornadas: {contagem}")
    return r.status_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outra-caixa", help="caixa que deve ser NEGADA (403), para provar a restrição")
    args = ap.parse_args()

    env = ler_env()
    token = obter_token(env)
    ok = testar_caixa(token, env["MS_DSR_MAILBOX"]) == 200
    if args.outra_caixa:
        negada = testar_caixa(token, args.outra_caixa) == 403
        print("PASSA" if ok and negada else "FALHA", "(esperado: 200 na caixa dos XMLs e 403 na outra)")
    else:
        print("PASSA parcial (só 200)" if ok else "FALHA", "- rode com --outra-caixa para provar o 403")


if __name__ == "__main__":
    main()
