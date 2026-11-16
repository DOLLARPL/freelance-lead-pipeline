from __future__ import annotations

from pathlib import Path
from typing import Protocol

STDOUT_TOKEN = "-"


class DeliveryError(Exception):
    """A delivery could not be confirmed; candidates stay uncommitted."""


class Delivery(Protocol):
    """Confirm/send the plain-text report. Raising means "not confirmed"."""

    def send_report(self, text: str) -> None:
        ...


class ReportDelivery:
    """Report sink: a caller-supplied file or stdout.

    ``send_report`` only returns after the report was durably written, so the
    pipeline can treat a normal return as the delivery-confirmation callback
    and only then commit the seen-state.
    """

    def __init__(self, *, path: str | Path = STDOUT_TOKEN) -> None:
        self.path = str(path)

    def send_report(self, text: str) -> None:
        if self.path == STDOUT_TOKEN:
            if text:
                print(text)
            return
        path = Path(self.path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise DeliveryError(f"cannot write report {self.path}: {exc}") from exc


class TelegramConfigError(ValueError):
    """Provided Telegram configuration is unusable."""


class TelegramDelivery:
    """Minimal, configurable-but-nevering-sending Telegram stub.

    The CLI may accept a token and chat id from env/CLI and call :meth:
    `validate` to prove the configuration is well-formed; this build never
    performs a single Telegram request and never reads any credential file.
    ``send_report`` always refuses, so a full pipeline can never accidentally
    message anyone until an explicit gate is added in a later slice.
    """

    def __init__(self, *, token: str | None = None, chat_id: str | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self.messages_sent = 0

    def validate(self) -> None:
        token = self._token
        if not isinstance(token, str) or not token.strip():
            raise TelegramConfigError("telegram token must be a non-empty string")
        chat_id = self._chat_id
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise TelegramConfigError("telegram chat_id must be a non-empty string")
        normalized = chat_id.lstrip("-")
        if not normalized.isdigit():
            raise TelegramConfigError(f"telegram chat_id must be numeric, got {chat_id!r}")

    def send_report(self, text: str) -> None:
        self.messages_sent = 0
        raise DeliveryError(
            "Telegram sending is disabled in this build; nothing was transmitted. "
            "The report was written to the configured report sink instead."
        )


__all__ = [
    "Delivery",
    "DeliveryError",
    "ReportDelivery",
    "TelegramConfigError",
    "TelegramDelivery",
]