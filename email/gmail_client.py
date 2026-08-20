"""
Cliente Gmail reusável (somente leitura) — base pra qualquer automação
do tipo "monitora Gmail e processa anexo".

Ver skill `skills/watch-gmail-attachment/` pra como montar uma automação
nova em cima deste módulo.
"""

import base64
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")


def get_credentials(base_dir=None):
    """base_dir: pasta com client_secret.json/token.json próprios, pra autenticar
    uma segunda conta Gmail sem mexer nos arquivos da conta padrão. Default:
    comportamento original (conta configurada em simple/email/)."""
    base_dir = base_dir or BASE_DIR
    client_secret_file = os.path.join(base_dir, "client_secret.json")
    token_file = os.path.join(base_dir, "token.json")

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                pass  # refresh_token em si expirou/revogado — cai pro login manual abaixo

        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return creds


def get_service(base_dir=None):
    return build("gmail", "v1", credentials=get_credentials(base_dir=base_dir))


def list_messages(service, query="", max_results=10):
    """Pagina até juntar max_results mensagens ou esgotar a query.

    A API do Gmail limita cada chamada a 500 resultados e não pagina
    sozinha — sem esse loop, max_results > 500 (ou uma query com mais de
    500 resultados) trunca silenciosamente na primeira página."""
    messages = []
    page_token = None
    while len(messages) < max_results:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(500, max_results - len(messages)),
            pageToken=page_token,
        ).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message(service, msg_id):
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def extract_body(payload):
    if "parts" in payload:
        for part in payload["parts"]:
            body = extract_body(part)
            if body:
                return body
    elif payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    return ""


def _iter_parts(payload):
    if "parts" in payload:
        for part in payload["parts"]:
            yield from _iter_parts(part)
    else:
        yield payload


def download_attachments(service, msg_id, payload, out_dir, subject=""):
    saved = []
    for part in _iter_parts(payload):
        filename = part.get("filename")
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        if not filename or not attachment_id:
            continue

        att = service.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=attachment_id
        ).execute()
        data = base64.urlsafe_b64decode(att["data"])

        msg_dir = os.path.join(out_dir, msg_id)
        os.makedirs(msg_dir, exist_ok=True)
        path = os.path.join(msg_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        saved.append(path)
    return saved
