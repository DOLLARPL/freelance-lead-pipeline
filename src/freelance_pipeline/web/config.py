"""Ustawienia panelu czytane ze srodowiska (bez nowych zaleznosci)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "tak", "yes", "on")


@dataclass(frozen=True)
class Settings:
    cache_dir: Path
    reports_dir: Path
    db_path: Path
    ledger_path: Path
    cennik_path: Path
    owner_login: str
    allow_local: bool
    host: str
    port: int
    auto_import_seconds: int
    openrouter_key: str
    vapid_private_path: Path
    vapid_public_path: Path
    vapid_subject: str
    public_host: str

    @classmethod
    def from_env(cls) -> "Settings":
        cache_dir = Path(os.environ.get("SNAJPER_CACHE_DIR", "/opt/data/cache/snajper-v2"))
        return cls(
            cache_dir=cache_dir,
            reports_dir=Path(os.environ.get("SNAJPER_REPORTS_DIR", str(cache_dir / "reports"))),
            db_path=Path(
                os.environ.get("SNAJPER_WEB_DB", "/opt/data/cache/snajper-web/snajper.db")
            ),
            ledger_path=Path(os.environ.get("SNAJPER_LEDGER", str(cache_dir / "ledger.json"))),
            cennik_path=Path(
                os.environ.get("SNAJPER_CENNIK", "/opt/data/projects/snajper-ofert/config/cennik.yaml")
            ),
            owner_login=os.environ.get("SNAJPER_OWNER_LOGIN", "").strip(),
            allow_local=_env_flag("SNAJPER_ALLOW_LOCAL", False),
            host=os.environ.get("SNAJPER_HOST", "127.0.0.1"),
            port=int(os.environ.get("SNAJPER_PORT", "8787")),
            auto_import_seconds=int(os.environ.get("SNAJPER_AUTO_IMPORT", "600")),
            openrouter_key=os.environ.get("OPENROUTER_API_KEY", ""),
            vapid_private_path=Path(
                os.environ.get(
                    "SNAJPER_VAPID_KEY", "/opt/data/cache/snajper-web/vapid_private.pem"
                )
            ),
            vapid_public_path=Path(
                os.environ.get(
                    "SNAJPER_VAPID_PUBLIC", "/opt/data/cache/snajper-web/vapid_public.txt"
                )
            ),
            vapid_subject=os.environ.get("SNAJPER_VAPID_SUBJECT", "mailto:jan@jnxautomation.pl"),
            public_host=os.environ.get("SNAJPER_PUBLIC_HOST", "snajper.jnxautomation.pl").strip(),
        )
