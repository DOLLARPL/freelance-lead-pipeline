"""The live portfolio, read from the website sources.

One source of truth for two consumers: the scorer (so the model knows what Jan
can already show) and the reply helper (so a proposal can only cite projects
that really exist on jnxautomation.pl).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SITE_URL = "https://jnxautomation.pl"
PROJECTS_DIR = Path("/opt/data/work/jnx-automation-copy/src/content/projects")

_FRONT_RE = re.compile(r"^(title|slug|result):\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class PortfolioItem:
    """One published case study."""

    slug: str
    title: str
    result: str

    @property
    def url(self) -> str:
        return f"{SITE_URL}/projekty/{self.slug}"


def load_portfolio(projects_dir: Path | None = None) -> list[PortfolioItem]:
    """Read the case studies that are on the site right now (empty if unknown)."""
    directory = projects_dir or PROJECTS_DIR
    if not directory.is_dir():
        return []
    items: list[PortfolioItem] = []
    for path in sorted(directory.glob("*.md")):
        # Wersje jezykowe `.en.md` maja slug z sufiksem `.en`, ktory nie istnieje
        # na stronie (404). Model bral je jako osobne projekty i linkowal w draftach.
        if path.name.endswith(".en.md"):
            continue
        fields = {
            key: value.strip().strip('"').strip("'")
            for key, value in _FRONT_RE.findall(path.read_text(encoding="utf-8"))
        }
        slug = fields.get("slug") or path.stem
        items.append(
            PortfolioItem(slug=slug, title=fields.get("title", slug), result=fields.get("result", ""))
        )
    return items


def render_portfolio_block(items: list[PortfolioItem] | None = None) -> str:
    """A short prompt block: what Jan already has, and what he therefore lacks."""
    entries = load_portfolio() if items is None else items
    lines = [f"PORTFOLIO JANA (stan na dziś, {SITE_URL}) — to są jego realne wdrożenia:"]
    if not entries:
        lines.append("- (brak odczytanych case studies — nie zakładaj żadnego)")
        return "\n".join(lines)
    for item in entries:
        lines.append(f"- {item.title} — efekt: {item.result} ({item.url})")
    return "\n".join(lines)


__all__ = ["PROJECTS_DIR", "SITE_URL", "PortfolioItem", "load_portfolio", "render_portfolio_block"]
