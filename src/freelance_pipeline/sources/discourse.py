from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any, ClassVar

from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    HTTPClient,
    SourceError,
    SourceHTTPError,
    SourceParseError,
)
from freelance_pipeline.models import JobLead
from freelance_pipeline.urls import canonicalize_url

from ._common import (
    _tag_names,
    extract_status,
    html_to_text,
    parse_iso,
    parse_rfc822,
    sanitize_topic,
)


def _local_tag(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree adds to namespaced tags."""
    return tag.rsplit("}", 1)[-1]


def _inner_xml(el: ET.Element) -> str:
    """Serialise an element's inner content (its HTML body) back to a string."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(ET.tostring(child, encoding="unicode"))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _text_of(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    return (el.text or "").strip() or None


class DiscourseCategoryAdapter:
    """Shared base for Discourse community category feeds (JSON or RSS).

    Pagination follows Discourse's zero-based semantics: the category URL
    without a query string is page 0, ``?page=1`` is the second page, and so
    on. ``max_pages`` is the total number of pages to fetch (base + up to
    ``max_pages - 1`` query pages); it must be a plain ``int`` of at least 1
    and is safely capped at 5. Pagination stops as soon as a page returns no
    items, so the cap is an upper bound, not a guaranteed fetch count.

    Exclusion policy (JSON category feeds): pinned or globally-pinned topics
    and topics whose title marks them as the "About this category" metadata
    topic are never emitted, because they are category metadata rather than
    jobs. Explicitly closed or archived topics are treated as unavailable and
    are likewise excluded, so they can never masquerade as active leads.
    """

    source: ClassVar[str] = "discourse"
    error_cls: ClassVar[type[SourceError]]
    base_url: ClassVar[str] = "https://community.example.com"
    category_slug: ClassVar[str] = "jobs"
    feed_format: ClassVar[str] = "json"  # or "rss"
    max_pages: ClassVar[int] = 1
    _MAX_PAGES_CAP: ClassVar[int] = 5
    _ABOUT_TOPIC_RE = re.compile(r"(?i)^about (the|this) .{0,40} categor")

    def __init__(
        self,
        *,
        base_url: str | None = None,
        session: Any | None = None,
        timeout: float = 15.0,
        max_pages: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.timeout = timeout
        self.max_pages = self._coerce_max_pages(
            max_pages if max_pages is not None else self.max_pages
        )
        self._http = HTTPClient(
            session=session,
            timeout=timeout,
            user_agent=user_agent or DEFAULT_USER_AGENT,
            source=self.source,
        )

    @classmethod
    def _coerce_max_pages(cls, value: Any) -> int:
        """Validate ``max_pages`` as a plain int in ``[1, _MAX_PAGES_CAP]``.

        Bools are rejected (they are ints in Python) and values above the
        safety cap are clamped rather than trusted.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"max_pages must be a plain int, got {type(value).__name__}"
            )
        if value < 1:
            raise ValueError(f"max_pages must be >= 1, got {value}")
        return min(value, cls._MAX_PAGES_CAP)

    def _extension(self) -> str:
        return "rss" if self.feed_format == "rss" else "json"

    def _category_url(self, page: int) -> str:
        """Return the category URL for a zero-based page number."""
        url = f"{self.base_url}/c/{self.category_slug}.{self._extension()}"
        if page > 0:
            url = f"{url}?page={page}"
        return url

    def collect(self) -> list[JobLead]:
        leads: list[JobLead] = []
        seen: set[str] = set()
        for page in range(self.max_pages):
            url = self._category_url(page)
            try:
                payload = self._http.get(url).text
                items, ctx = self._iter_items(payload)
            except (SourceHTTPError, SourceParseError) as exc:
                raise self.error_cls(str(exc)) from exc
            for item in items:
                lead = self._item_to_lead(item, ctx)
                if lead is None or lead.source_id in seen:
                    continue
                seen.add(lead.source_id)
                leads.append(lead)
            if not items:
                break
        return leads

    def _iter_items(self, payload: str) -> tuple[list[Any], dict[str, Any] | None]:
        """Return ``(items, ctx)``; ``ctx`` carries per-response context.

        For JSON feeds the context is the ``users`` array indexed by id so
        topic creators can be resolved without extra requests. RSS has no
        such context and returns ``None``.
        """
        if self.feed_format == "rss":
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise SourceParseError(self.source, f"invalid RSS/XML: {exc}") from exc
            return (
                [el for el in root.iter() if _local_tag(el.tag) == "item"],
                None,
            )
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SourceParseError(self.source, f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SourceParseError(self.source, "JSON payload must be an object")
        topic_list = data.get("topic_list")
        if not isinstance(topic_list, dict):
            raise SourceParseError(self.source, "topic_list must be an object")
        topics = topic_list.get("topics")
        if not isinstance(topics, list):
            raise SourceParseError(self.source, "topic_list.topics must be a list")
        return topics, {"users_by_id": self._users_by_id(data)}

    def _item_to_lead(self, item: Any, ctx: dict[str, Any] | None) -> JobLead | None:
        if self.feed_format == "rss":
            return self._rss_item_to_lead(item)
        return self._json_topic_to_lead(item, ctx or {})

    @staticmethod
    def _users_by_id(data: dict[str, Any]) -> dict[int, str]:
        """Map the category JSON ``users`` array onto ``id -> username``."""
        users = data.get("users")
        if not isinstance(users, list):
            return {}
        result: dict[int, str] = {}
        for user in users:
            if not isinstance(user, dict):
                continue
            uid = user.get("id")
            username = user.get("username")
            if isinstance(uid, int) and uid >= 0 and isinstance(username, str) and username.strip():
                result[uid] = username
        return result

    @staticmethod
    def _original_poster_username(
        topic: dict[str, Any], users_by_id: dict[int, str]
    ) -> str | None:
        """Resolve the original topic creator from ``posters`` + ``users``.

        The category listing already carries the full ``users`` array and each
        topic's ``posters`` metadata, so no per-topic detail request is needed.
        If the creator cannot be determined, ``None`` is returned rather than
        falling back to (or fabricating) the most recent responder.
        """
        posters = topic.get("posters")
        if not isinstance(posters, list) or not posters:
            return None
        op_id: int | None = None
        for poster in posters:
            if not isinstance(poster, dict):
                continue
            uid = poster.get("user_id")
            description = poster.get("description")
            if not isinstance(uid, int) or uid < 0:
                continue
            if "Original Poster" in (description or ""):
                op_id = uid
                break
            if op_id is None:
                op_id = uid
        if op_id is None:
            return None
        username = users_by_id.get(op_id)
        return username if isinstance(username, str) and username.strip() else None

    @classmethod
    def _is_excluded_topic(cls, topic: dict[str, Any]) -> bool:
        """Apply the metadata/closed-archived exclusion policy to a topic."""
        if topic.get("pinned") or topic.get("pinned_globally"):
            return True
        if cls._ABOUT_TOPIC_RE.match(str(topic.get("title") or "")):
            return True
        return bool(topic.get("closed") or topic.get("archived"))

    # ---- RSS (n8n) ----
    def _rss_item_to_lead(self, item: ET.Element) -> JobLead | None:
        children = {_local_tag(child.tag): child for child in item}
        link = _text_of(children.get("link")) or ""
        title = _text_of(children.get("title")) or ""
        if not link or not title:
            return None
        match = re.search(r"/(\d+)/?$", link)
        if not match:
            return None
        source_id = match.group(1)

        creator = _text_of(children.get("creator"))
        published = parse_rfc822(_text_of(children.get("pubDate")))
        description = html_to_text(_inner_xml(children["description"])) if "description" in children else ""
        canonical = canonicalize_url(link)
        raw = {
            "title": title,
            "link": link,
            "creator": creator,
            "pub_date": _text_of(children.get("pubDate")),
        }
        return JobLead(
            source=self.source,
            source_id=source_id,
            url=canonical,
            canonical_url=canonical,
            title=title,
            description=description,
            company_or_author=creator,
            published_at=published,
            status=extract_status(title),
            raw_data=raw,
        )

    # ---- JSON (Make) ----
    def _json_topic_to_lead(self, topic: Any, ctx: dict[str, Any]) -> JobLead | None:
        if not isinstance(topic, dict):
            return None
        if self._is_excluded_topic(topic):
            return None
        topic_id = topic.get("id")
        title = str(topic.get("title") or "").strip()
        if topic_id is None or not title:
            return None
        slug = str(topic.get("slug") or "").strip()
        url = f"{self.base_url}/t/{slug}/{topic_id}" if slug else f"{self.base_url}/t/{topic_id}"
        canonical = canonicalize_url(url)
        published = parse_iso(topic.get("created_at"))
        author = self._original_poster_username(topic, ctx.get("users_by_id") or {})
        excerpt = str(topic.get("excerpt") or "").strip()
        return JobLead(
            source=self.source,
            source_id=str(topic_id),
            url=canonical,
            canonical_url=canonical,
            title=title,
            description=excerpt,
            company_or_author=author,
            published_at=published,
            skills=_tag_names(topic.get("tags")),
            status=extract_status(title),
            raw_data=sanitize_topic(topic),
        )