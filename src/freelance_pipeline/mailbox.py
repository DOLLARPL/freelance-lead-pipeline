"""Read-only mailbox access for the Oferteo notification stream.

Oferteo pushes one e-mail per matching request (``powiadomienie@oferteo.pl``),
plus a second one after the contact is unlocked (``☎ … Zadzwoń``). Reading that
stream is strictly cheaper and safer than scraping the logged-in panel: no
session, no ``robots.txt`` path, no points.

The Gmail API has a per-minute quota, so the adapter is incremental by design:
it lists metadata first, skips ids already seen, and only then fetches the full
body for the new ones. Every 403/429 is retried with exponential backoff.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

OFERTEO_SENDERS = ("powiadomienie@oferteo.pl", "kontakty@oferteo.pl")
OFERTEO_SENDER = OFERTEO_SENDERS[0]  # backwards-compatible alias
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class MailboxError(RuntimeError):
    """Raised when the mailbox cannot be read."""


@dataclass(frozen=True)
class MailMessage:
    """One e-mail, already decoded to HTML when it has an HTML part."""

    message_id: str
    thread_id: str
    received_at: datetime
    subject: str
    from_addr: str
    html: str


class Mailbox(Protocol):
    """Minimal contract the importer needs."""

    def fetch(
        self,
        *,
        since: timedelta = timedelta(days=7),
        limit: int | None = None,
        known_ids: Iterable[str] = (),
    ) -> list[MailMessage]: ...


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")


def _html_of(payload: dict) -> str:
    """Concatenate every text/html part of a Gmail payload."""
    chunks: list[str] = []

    def walk(part: dict) -> None:
        body = part.get("body") or {}
        if part.get("mimeType") == "text/html" and body.get("data"):
            chunks.append(_decode(body["data"]))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return "".join(chunks)


class GmailMailbox:
    """Incremental reader over the Gmail API.

    Two senders matter: ``powiadomienie@`` for new requests and ``kontakty@``
    for the "☎ … Zadzwoń" mail sent after the contact is unlocked. The account
    manager (``kolbinski@``) is deliberately not included.
    """

    def __init__(
        self,
        token_path: str,
        *,
        senders: tuple[str, ...] = OFERTEO_SENDERS,
        sleep=time.sleep,
    ) -> None:
        self.token_path = token_path
        self.senders = senders
        self._sleep = sleep
        self._service = None

    def _svc(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MailboxError(
                "brak google-api-python-client; doinstaluj grupę zależności 'mail': "
                "uv sync --group mail (albo uv pip install --python .venv/bin/python "
                "google-api-python-client)"
            ) from exc
        creds = Credentials.from_authorized_user_file(self.token_path, [GMAIL_SCOPE])
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _call(self, fn, **kwargs):
        """Run one API call, backing off on quota errors."""
        delay = 2.0
        for attempt in range(5):
            try:
                return fn(**kwargs).execute()
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status not in (403, 429) or attempt == 4:
                    raise MailboxError(str(exc)) from exc
                # Gmail quota is per user per minute, so the backoff must cross
                # a full window instead of a few seconds.
                self._sleep(max(delay, 30.0))
                delay *= 2
        raise MailboxError("quota retries exhausted")

    def fetch(
        self,
        *,
        since: timedelta = timedelta(days=7),
        limit: int | None = None,
        known_ids: Iterable[str] = (),
    ) -> list[MailMessage]:
        known = set(known_ids)
        days = max(1, int(since.total_seconds() // 86400))
        svc = self._svc()
        only = " OR ".join(f"from:{addr}" for addr in self.senders)
        query = f"({only}) newer_than:{days}d"
        ids: list[str] = []
        token = None
        while True:
            page = self._call(
                svc.users().messages().list,
                userId="me",
                q=query,
                maxResults=500,
                pageToken=token,
            )
            ids.extend(m["id"] for m in page.get("messages", []))
            token = page.get("nextPageToken")
            if not token:
                break
        fresh = [mid for mid in ids if mid not in known]
        if limit is not None:
            fresh = fresh[:limit]
        out: list[MailMessage] = []
        for mid in fresh:
            msg = self._call(
                svc.users().messages().get, userId="me", id=mid, format="full"
            )
            headers = {
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            out.append(
                MailMessage(
                    message_id=mid,
                    thread_id=msg.get("threadId", ""),
                    received_at=datetime.fromtimestamp(
                        int(msg.get("internalDate", "0")) / 1000, tz=UTC
                    ),
                    subject=headers.get("Subject", ""),
                    from_addr=headers.get("From", ""),
                    html=_html_of(msg.get("payload", {})),
                )
            )
        return out
