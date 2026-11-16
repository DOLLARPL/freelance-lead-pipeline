from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

STATUS_MARKERS: tuple[tuple[str, str], ...] = (
    ("[for hire]", "for_hire"),
    ("[hiring]", "hiring"),
    ("[filled]", "filled"),
    ("[closed]", "closed"),
)


def html_to_text(html: str) -> str:
    """Strip HTML to collapsed plain text (safe for empty/None input)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def parse_iso(value: Any) -> datetime | None:
    """Parse a Discourse-style ISO timestamp into a UTC-aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def parse_rfc822(value: str | None) -> datetime | None:
    """Parse an RFC 822/RSS ``pubDate`` into a UTC-aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def extract_status(title: str) -> str | None:
    """Map leading ``[For Hire]``-style markers to a normalized status value."""
    lowered = title.lower()
    for marker, status in STATUS_MARKERS:
        if lowered.startswith(marker):
            return status
    return None


def _tag_names(tags: Any) -> tuple[str, ...]:
    """Extract skill names from Discourse tag dicts or plain strings."""
    names: list[str] = []
    if not tags:
        return ()
    for tag in tags:
        name = None
        if isinstance(tag, str):
            name = tag
        elif isinstance(tag, dict) and isinstance(tag.get("name"), str):
            name = tag["name"]
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def sanitize_topic(topic: dict[str, Any]) -> dict[str, Any]:
    """Keep a small, JSON-safe subset of a Discourse topic dict."""
    keys = (
        "id",
        "title",
        "slug",
        "created_at",
        "excerpt",
        "tags",
        "last_poster_username",
        "posts_count",
        "views",
        "closed",
        "archived",
        "pinned",
        "pinned_globally",
    )
    return {key: topic.get(key) for key in keys if key in topic}


__all__ = [
    "extract_status",
    "html_to_text",
    "parse_iso",
    "parse_rfc822",
    "sanitize_topic",
]