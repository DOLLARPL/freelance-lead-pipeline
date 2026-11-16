from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from freelance_pipeline.models import JobLead
from freelance_pipeline.scoring import ScoreBucket, ScoredLead

# Jan's decision (2026-09-22): the Telegram report carries **every** new offer
# from the TOP and MOŻE buckets, uncapped and without a per-source ceiling. The
# caps (15 rows, at most 5 rows from Facebook groups) were hiding rows he never
# saw, and his call is that a visible low-priority offer is cheaper than a missed
# client. ODRZUĆ still never reaches the chat — it stays in the per-run audit
# file, and ZAMIEN keeps its own separate message. A run with no TOP and no MOŻE
# yields an empty chat report, which the cron wrapper turns into silence (no
# message at all). The Telegram adapter chunks anything above 4096 characters
# into several messages, so a long report still arrives whole.

# Jan's decision (2026-09-21): rows from Polish boards go to the top of the chat
# report, then the rest, all ranked by score inside their group. Nothing is
# reserved: a Polish row only takes a slot when it actually exists, and the
# fifteen-row cap stays the same. Useme is the only Polish source in the active
# set today, so the rest of the pile (n8ndevs, n8n community, Make) can no
# longer push Polish offers off the chat.
POLISH_SOURCES: tuple[str, ...] = ("useme", "oferteo")

# Per-source ceiling for the chat report: **removed** on 2026-09-22 together with
# the fifteen-row cap (Jan: „chcę mieć wszystkie"). Facebook groups used to be
# held to five rows per report so they could not push Useme out of the chat;
# every row now ships, and quality inside the feed is handled by the seller-voice
# rules in ``scoring.py``, not by a smaller report.

_HEADINGS = {
    "top": "SNAJPER v2 — TOP: nowe zlecenia (na Telegram, max {limit}; polskie źródła na górze), {stamp} UTC",
    "moze": "SNAJPER v2 — MOŻE: do przejrzenia (plik, nie na Telegram), {stamp} UTC",
    "top+moze": (
        "SNAJPER v2 — nowe zlecenia: TOP + MOŻE (na Telegram, bez limitu; "
        "polskie źródła na górze), {stamp} UTC"
    ),
    "zamien": (
        "SNAJPER v2 — ZAMIEN: zaproponuj automatyzację zamiast zatrudnienia "
        "(osobna wiadomość na Telegram), {stamp} UTC"
    ),
}


def _is_polish(entry: ScoredLead) -> bool:
    return entry.lead.source in POLISH_SOURCES


def _oferteo_details(lead: JobLead) -> str | None:
    """One line of zapytanie data: city, buyer type, publication date, deadline.

    Oferteo's public list shows a title and nothing else, so twenty-four rows
    titled „Zapytanie o wykonanie strony www” are indistinguishable in the chat
    report even though the adapter already collects the city, the buyer type and
    the publication date. Jan's verdict 2026-09-25: put them in the row, so a
    paid-contact row can be judged without opening the portal. Only Oferteo
    leads carry ``contact_requires_points``; every other source keeps the
    original five-line row.
    """
    data = lead.raw_data or {}
    if not data.get("contact_requires_points"):
        return None
    parts: list[str] = []
    if lead.location:
        parts.append(lead.location)
    buyer = data.get("buyer_type")
    if isinstance(buyer, str) and buyer:
        parts.append(buyer)
    if lead.published_at is not None:
        parts.append(f"opublikowane {lead.published_at.strftime('%Y-%m-%d')}")
    deadline = data.get("deadline")
    if isinstance(deadline, str) and deadline:
        parts.append(f"termin: {deadline}")
    if data.get("buyer_verified"):
        parts.append("zlecający zweryfikowany")
    if not parts:
        return None
    return "  zapytanie: " + " | ".join(parts)


def _oferteo_listing_line(lead: JobLead) -> str | None:
    """The category list a zapytanie was found on, as a second, always-open link.

    ``lead.url`` is the exact request (``/konto/zapytanie/<id>_X``) and needs the
    free Wykonawca login; the lane link works logged out. Jan asked on 2026-09-25
    for links narrow enough not to force manual searching, so the row carries
    both: the zapytanie itself and the list it came from.
    """
    data = lead.raw_data or {}
    listing = data.get("listing_url")
    if isinstance(listing, str) and listing and listing != lead.url:
        return f"  lista: {listing}"
    return None


def render_report(
    scored: Sequence[ScoredLead],
    *,
    now: datetime | None = None,
    buckets: Sequence[ScoreBucket] = (ScoreBucket.TOP, ScoreBucket.MOZE),
    limit: int | None = None,
    heading: str | None = None,
    polish_first: bool = False,
    per_source_limit: Mapping[str, int] | None = None,
) -> str:
    """Render a plain-text report limited to the requested buckets.

    Every row carries bucket, score, title, source, company/author, URL and the
    concise rationale in a stable layout. ODRZUĆ rows never appear here (they go
    to the rejected audit instead). An empty selection produces an empty string,
    which is what makes the "zero TOP ⇒ no chat message" rule work.

    ``limit`` caps the number of rows (highest score first, ties keep the input
    order) and states how many further rows were left out; the unlimited default
    keeps the input order exactly as scored. ``polish_first`` ranks rows from
    :data:`POLISH_SOURCES` above the rest (score order inside each group) before
    the cap is applied.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive integer or None")
    selected = [entry for entry in scored if entry.bucket in tuple(buckets)]
    if not selected:
        return ""
    stamp = (now if now is not None else datetime.now(UTC)).strftime("%Y-%m-%d %H:%M")
    if heading is None:
        selected_buckets = tuple(buckets)
        if selected_buckets == (ScoreBucket.TOP,):
            key = "top"
        elif selected_buckets == (ScoreBucket.MOZE,):
            key = "moze"
        elif selected_buckets == (ScoreBucket.ZAMIEN,):
            key = "zamien"
        else:
            key = "top+moze"
        heading = _HEADINGS[key]
    ordered = list(selected)
    if polish_first:
        ordered.sort(key=lambda entry: (0 if _is_polish(entry) else 1, -entry.score))
    elif limit is not None and len(ordered) > limit:
        ordered.sort(key=lambda entry: -entry.score)
    if per_source_limit:
        kept: list[ScoredLead] = []
        used: dict[str, int] = {}
        for entry in ordered:
            cap = per_source_limit.get(entry.lead.source)
            if cap is not None and used.get(entry.lead.source, 0) >= cap:
                continue
            used[entry.lead.source] = used.get(entry.lead.source, 0) + 1
            kept.append(entry)
        ordered = kept
    shown = ordered if limit is None else ordered[:limit]
    omitted = len(ordered) - len(shown)
    lines: list[str] = [
        heading.format(stamp=stamp, limit=limit if limit is not None else len(shown)),
        f"Liczba nowych ofert w kubełku: {len(selected)}",
        "",
    ]
    for entry in shown:
        lead = entry.lead
        company = lead.company_or_author or "(brak)"
        lines.append(f"[{entry.bucket.value}] {entry.score}/100 — {lead.title}")
        lines.append(f"  source: {lead.source}")
        lines.append(f"  company/author: {company}")
        lines.append(f"  url: {lead.url}")
        if lead.raw_data and lead.raw_data.get("contact_requires_points"):
            lines.append(
                "  kontakt: płatny, punkty Oferteo, nie odblokowujemy automatycznie"
            )
            listing = _oferteo_listing_line(lead)
            if listing is not None:
                lines.append(listing)
            details = _oferteo_details(lead)
            if details is not None:
                lines.append(details)
        lines.append(f"  rationale: {entry.rationale}")
        lines.append("")
    if omitted:
        lines.append(f"(+{omitted} kolejnych TOP w raporcie pliku tego przebiegu)")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_chat_report(scored: Sequence[ScoredLead], *, now: datetime | None = None) -> str:
    """The Telegram-bound report: every TOP **and** MOŻE row, uncapped.

    Polish sources come first (:data:`POLISH_SOURCES`), then the rest, each group
    ranked by score — Jan's decisions: ordering of 2026-09-21, uncapped TOP+MOŻE
    delivery of 2026-09-22. ODRZUĆ and ZAMIEN rows are not part of it.
    """
    return render_report(
        scored,
        now=now,
        buckets=(ScoreBucket.TOP, ScoreBucket.MOZE),
        polish_first=True,
    )


def render_zamien_report(scored: Sequence[ScoredLead], *, now: datetime | None = None) -> str:
    """The ZAMIEN list: offers to answer with automation, not with a hire.

    Jan's decision (2026-09-21, after his first review of the bucket): it is
    delivered to Telegram as its **own second message**, never mixed into the
    TOP report, and it always lands in ``reports/zamien-*.txt`` so the signal
    stays countable over several runs.
    """
    return render_report(scored, now=now, buckets=(ScoreBucket.ZAMIEN,))


def render_moze_report(scored: Sequence[ScoredLead], *, now: datetime | None = None) -> str:
    """The file-only report: every MOŻE row of this run."""
    return render_report(scored, now=now, buckets=(ScoreBucket.MOZE,))


def render_top_report(scored: Sequence[ScoredLead], *, now: datetime | None = None) -> str:
    """The per-run TOP archive: **every** TOP row of this run.

    It is rendered from the same selection the chat now sends in full (TOP+MOŻE,
    uncapped since 2026-09-22), so it doubles as the TOP-only slice of the run.
    """
    return render_report(scored, now=now, buckets=(ScoreBucket.TOP,))


__all__ = [
    "POLISH_SOURCES",
    "render_chat_report",
    "render_moze_report",
    "render_report",
    "render_top_report",
    "render_zamien_report",
]
