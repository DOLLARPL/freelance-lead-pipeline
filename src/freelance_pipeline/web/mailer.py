"""Wysylka maila z panelu przez Gmail API (konto Jana).

Token OAuth lezy w ``/opt/data/google_token.json`` i ma scope ``gmail.send``.
Panel wysyla wiadomosc dopiero po kliknieciu przycisku przez uzytkownika:
to nie jest automatyczna wysylka oferty, tylko reczna decyzja Jana.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

DEFAULT_TOKEN = Path("/opt/data/google_token.json")


class MailError(RuntimeError):
    """Wysylka nie doszla do skutku; tresc bledu idzie do panelu."""


@dataclass
class SentMail:
    message_id: str
    to: str
    subject: str


def send(
    *,
    to: str,
    subject: str,
    body: str,
    token_path: Path = DEFAULT_TOKEN,
    sender: str = "me",
) -> SentMail:
    to = (to or "").strip()
    if "@" not in to:
        raise MailError(f"niepoprawny adres: {to!r}")
    if not Path(token_path).exists():
        raise MailError(f"brak tokenu Gmaila: {token_path}")

    try:
        import google.auth.transport.requests
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - zaleznosc opcjonalna
        raise MailError("brak biblioteki Gmaila w srodowisku") from exc

    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        if not creds.refresh_token:
            raise MailError("token wygasl i nie ma refresh_token")
        creds.refresh(google.auth.transport.requests.Request())

    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId=sender, body={"raw": raw}).execute()
    return SentMail(message_id=str(sent.get("id") or ""), to=to, subject=subject)
