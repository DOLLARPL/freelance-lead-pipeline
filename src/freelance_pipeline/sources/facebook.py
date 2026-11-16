from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.urls import canonicalize_url

FACEBOOK_GROUPS_SOURCE = "facebook_groups"

# The transport (a scheduled Apify run, HANDOFF "Etap 1") writes one payload per
# run here; this adapter only ever reads the file. It never issues a request to
# facebook.com or api.apify.com — that is the account-safety rule of the plan
# (FACEBOOK-GROUPS.md, section 2) and a test guards it.
DEFAULT_CACHE_DIR = Path("/opt/data/cache/snajper-v2/facebook")
DEFAULT_GROUPS_CONFIG = (
    Path(__file__).resolve().parents[3] / "config" / "facebook_groups.yaml"
)
POSTS_FILE_GLOB = "posts-*.json"

# A payload older than this means the transport did not run (or wrote nothing),
# so the source must say "no data" instead of quietly reporting zero posts.
DEFAULT_MAX_AGE_HOURS = 26.0

ALLOWED_TAGS = (
    "automatyzacja",
    "no-code",
    "ecommerce",
    "freelancing",
    "lokalne-warszawa",
    "founderzy",
)
ALLOWED_LANGS = ("pl", "en")
ALLOWED_VIEWS = ("CHRONOLOGICAL", "RECENT_ACTIVITY", "TOP_POSTS")

_GROUP_KEYS = (
    "url",
    "name",
    "tag",
    "lang",
    "public",
    "enabled",
    "max_posts",
    "view",
)

# Post titles carry the group plus the first sentence of the post, so the report
# says where the offer came from without opening Facebook.
_TITLE_LIMIT = 140
_TITLE_NOISE_RE = re.compile(r"^[\s>*#\-–—•·]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class FacebookGroupsError(SourceError):
    """Raised when the Facebook groups payload cannot be turned into leads."""


class FacebookGroupsConfigError(FacebookGroupsError):
    """Raised when config/facebook_groups.yaml is missing or malformed."""


@dataclass(frozen=True, slots=True)
class FacebookGroup:
    """One configured group, validated at load time."""

    url: str
    name: str
    tag: str
    lang: str
    public: bool
    enabled: bool
    max_posts: int
    view: str

    @property
    def key(self) -> str:
        """Normalised identity used to match payload entries to config entries."""
        return group_key(self.url)


@dataclass(frozen=True, slots=True)
class GroupReport:
    """Per-group outcome of one collection, for the run's visible status."""

    url: str
    name: str
    status: str
    posts: int = 0
    posts_with_text: int = 0
    leads: int = 0
    note: str | None = None

    def render(self) -> str:
        parts = [f"{self.name} ({self.url.rsplit('/', 1)[-1]}): {self.status}"]
        if self.posts:
            parts.append(f"{self.posts} postów, {self.posts_with_text} z tekstem")
        parts.append(f"{self.leads} leadów")
        if self.note:
            parts.append(self.note)
        return ", ".join(parts)


def group_key(url: str) -> str:
    """Normalise a group URL to ``facebook.com/groups/<slug>``."""
    parts = urlsplit(url.strip())
    host = (parts.netloc or parts.path).lower().removeprefix("www.")
    path = (parts.path if parts.netloc else "").rstrip("/")
    return f"{host}{path}".lower()


def load_groups(path: str | Path | None = None) -> tuple[FacebookGroup, ...]:
    """Read and strictly validate the group list.

    Unknown keys, wrong types, unknown tags/views/languages, duplicate groups and
    a non-Facebook URL all raise: a typo in the config would otherwise silently
    drop a group from collection.
    """
    config_path = Path(path) if path is not None else DEFAULT_GROUPS_CONFIG
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FacebookGroupsConfigError(
            f"[{FACEBOOK_GROUPS_SOURCE}] config error: cannot read {config_path}: {exc}"
        ) from exc
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FacebookGroupsConfigError(
            f"[{FACEBOOK_GROUPS_SOURCE}] config error: {config_path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise FacebookGroupsConfigError(
            f"[{FACEBOOK_GROUPS_SOURCE}] config error: {config_path} must contain a list of groups"
        )

    groups: list[FacebookGroup] = []
    seen: set[str] = set()
    for index, entry in enumerate(payload, start=1):
        group = _parse_group(entry, index=index, path=config_path)
        if group.key in seen:
            raise FacebookGroupsConfigError(
                f"[{FACEBOOK_GROUPS_SOURCE}] config error: duplicate group {group.url}"
            )
        seen.add(group.key)
        groups.append(group)
    if not groups:
        raise FacebookGroupsConfigError(
            f"[{FACEBOOK_GROUPS_SOURCE}] config error: {config_path} lists no groups"
        )
    return tuple(groups)


def _parse_group(entry: Any, *, index: int, path: Path) -> FacebookGroup:
    where = f"{path} entry #{index}"

    def fail(message: str) -> FacebookGroupsConfigError:
        return FacebookGroupsConfigError(
            f"[{FACEBOOK_GROUPS_SOURCE}] config error: {where}: {message}"
        )

    if not isinstance(entry, Mapping):
        raise fail("must be a mapping with url/name/tag/lang/public/enabled/max_posts/view")
    missing = [key for key in _GROUP_KEYS if key not in entry]
    if missing:
        raise fail(f"missing required key(s): {', '.join(missing)}")
    unknown = [key for key in entry if key not in _GROUP_KEYS]
    if unknown:
        raise fail(f"unknown key(s): {', '.join(sorted(str(key) for key in unknown))}")

    url = entry["url"]
    if not isinstance(url, str) or not url.strip():
        raise fail("url must be a non-empty string")
    key = group_key(url)
    prefix = "facebook.com/groups/"
    if not key.startswith(prefix):
        raise fail(f"url must be a Facebook group URL, got {url!r}")
    slug = key[len(prefix):]
    if not slug or "/" in slug:
        raise fail(f"url must point at one group, got {url!r}")

    name = entry["name"]
    if not isinstance(name, str) or not name.strip():
        raise fail("name must be a non-empty string")

    tag = entry["tag"]
    if tag not in ALLOWED_TAGS:
        raise fail(f"tag must be one of {', '.join(ALLOWED_TAGS)}, got {tag!r}")

    lang = entry["lang"]
    if lang not in ALLOWED_LANGS:
        raise fail(f"lang must be one of {', '.join(ALLOWED_LANGS)}, got {lang!r}")

    view = entry["view"]
    if view not in ALLOWED_VIEWS:
        raise fail(f"view must be one of {', '.join(ALLOWED_VIEWS)}, got {view!r}")

    for flag in ("public", "enabled"):
        if not isinstance(entry[flag], bool):
            raise fail(f"{flag} must be true or false, got {entry[flag]!r}")
    # A private group only ever returns a diagnostic note, so enabling one can
    # never produce leads and would just burn paid transport runs.
    if entry["enabled"] and not entry["public"]:
        raise fail("a group that is not confirmed public cannot be enabled")

    max_posts = entry["max_posts"]
    if isinstance(max_posts, bool) or not isinstance(max_posts, int) or max_posts <= 0:
        raise fail(f"max_posts must be a positive integer, got {max_posts!r}")

    return FacebookGroup(
        url=url.strip(),
        name=name.strip(),
        tag=str(tag),
        lang=str(lang),
        public=entry["public"],
        enabled=entry["enabled"],
        max_posts=max_posts,
        view=str(view),
    )


class FacebookGroups:
    """Adapter for public Facebook groups collected by an Apify actor.

    Input is a transport payload (see :meth:`_read_payload`) written by the
    scheduled fetch step; this adapter performs **no** network I/O. Leads map
    onto :class:`JobLead` as in FACEBOOK-GROUPS.md, section 5: permalink becomes
    ``url``/``canonical_url`` (the dedup key), post text becomes ``description``,
    group name + first sentence becomes ``title``, group/post language becomes
    ``language`` and the author stays context only (never a contact list).
    """

    source = FACEBOOK_GROUPS_SOURCE
    default_max_age_hours = DEFAULT_MAX_AGE_HOURS

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        posts_path: str | Path | None = None,
        groups_config_path: str | Path | None = None,
        max_age_hours: float | None = None,
        now: Callable[[], datetime] | None = None,
        check_freshness: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self.posts_path = Path(posts_path) if posts_path is not None else None
        self.groups_config_path = (
            Path(groups_config_path) if groups_config_path is not None else DEFAULT_GROUPS_CONFIG
        )
        hours = DEFAULT_MAX_AGE_HOURS if max_age_hours is None else float(max_age_hours)
        if hours <= 0:
            raise ValueError("max_age_hours must be positive")
        self.max_age_hours = hours
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        # Offline fixtures are frozen pilot payloads by definition, so the
        # staleness guard is switched off explicitly there and nowhere else.
        self.check_freshness = check_freshness
        # Visible per-group status of the last collection (also embedded in the
        # error when nothing came back, so a silent zero is impossible).
        self.last_group_report: tuple[GroupReport, ...] = ()

    def collect(self) -> list[JobLead]:
        groups = load_groups(self.groups_config_path)
        enabled = {group.key: group for group in groups if group.enabled}
        if not enabled:
            raise FacebookGroupsError(
                f"[{self.source}] no data from source: no group is enabled in "
                f"{self.groups_config_path}"
            )

        path = self._resolve_posts_path()
        payload = self._read_payload(path)
        self._check_freshness(payload, path)

        entries = self._payload_groups(payload, path)
        report: list[GroupReport] = []
        leads: list[JobLead] = []
        seen_ids: set[str] = set()

        for entry in entries:
            key = group_key(str(entry.get("url") or entry.get("group") or ""))
            group = enabled.get(key)
            if group is None:
                known = next((g for g in groups if g.key == key), None)
                status = "wyłączona w configu" if known else "grupa spoza configu"
                report.append(
                    GroupReport(
                        url=str(entry.get("url") or ""),
                        name=known.name if known else key,
                        status="skipped",
                        note=status,
                    )
                )
                continue
            group_leads, group_report = self._group_leads(entry, group)
            report.append(group_report)
            for lead in group_leads:
                if lead.source_id in seen_ids:
                    continue
                seen_ids.add(lead.source_id)
                leads.append(lead)

        self.last_group_report = tuple(report)
        if not leads:
            raise FacebookGroupsError(
                f"[{self.source}] no data from source: {path.name} turned into 0 leads; "
                + "; ".join(item.render() for item in report)
            )
        return leads

    def status_lines(self) -> tuple[str, ...]:
        """Per-group status of the last collection, one line per group."""
        return tuple(item.render() for item in self.last_group_report)

    def _group_leads(
        self, entry: Mapping[str, Any], group: FacebookGroup
    ) -> tuple[list[JobLead], GroupReport]:
        posts = entry.get("posts")
        posts = posts if isinstance(posts, list) else []
        leads: list[JobLead] = []
        with_text = 0
        for post in posts:
            if not isinstance(post, Mapping):
                continue
            text = post.get("text")
            if not isinstance(text, str) or not text.strip():
                # Plan, section 5: a post without text (photo only) is dropped.
                continue
            with_text += 1
            lead = self._post_to_lead(post, group, text=text)
            if lead is not None:
                leads.append(lead)

        diagnostic = entry.get("diagnostic") or entry.get("error")
        if diagnostic:
            status, note = "diagnostic", str(diagnostic)
        elif not posts:
            status, note = "empty", "grupa nie oddała postów w tym przebiegu"
        else:
            status, note = "ok", None
        return leads, GroupReport(
            url=group.url,
            name=group.name,
            status=status,
            posts=len(posts),
            posts_with_text=with_text,
            leads=len(leads),
            note=note,
        )

    def _post_to_lead(
        self, post: Mapping[str, Any], group: FacebookGroup, *, text: str
    ) -> JobLead | None:
        permalink = post.get("url") or post.get("portalUrl")
        if not isinstance(permalink, str) or not permalink.startswith("http"):
            return None
        canonical = canonicalize_url(permalink)
        source_id = self._source_id(post, canonical)
        if not source_id:
            return None

        author = post.get("user")
        author = author if isinstance(author, Mapping) else {}
        author_name = author.get("name") if isinstance(author.get("name"), str) else None
        author_url = author.get("profileUrl") or author.get("url") or author.get("profileLink")
        published_at = _parse_time(post.get("time"))

        return JobLead(
            source=self.source,
            source_id=source_id,
            url=canonical,
            canonical_url=canonical,
            title=_title(group.name, text),
            description=text,
            company_or_author=author_name,
            published_at=published_at,
            language=group.lang,
            raw_data={
                "group_url": group.url,
                "group_name": group.name,
                "group_tag": group.tag,
                "group_view": group.view,
                "permalink": permalink,
                "author_name": author_name,
                "author_url": author_url if isinstance(author_url, str) else None,
                "posted_at": post.get("time") if isinstance(post.get("time"), str) else None,
                "reactions": _first_count(post.get("likesCount"), post.get("topReactionsCount")),
                "comments": _count(post.get("commentsCount")),
                "shares": _count(post.get("sharesCount")),
                "media": len(post["attachments"]) if isinstance(post.get("attachments"), list) else 0,
            },
        )

    @staticmethod
    def _source_id(post: Mapping[str, Any], canonical: str) -> str | None:
        for key in ("legacyId", "id"):
            value = post.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        tail = urlsplit(canonical).path.rstrip("/").rsplit("/", 1)[-1]
        return tail or None

    def _resolve_posts_path(self) -> Path:
        if self.posts_path is not None:
            if not self.posts_path.is_file():
                raise FacebookGroupsError(
                    f"[{self.source}] no data from source: {self.posts_path} does not exist"
                )
            return self.posts_path
        candidates = sorted(
            self.cache_dir.glob(POSTS_FILE_GLOB),
            key=lambda item: (item.stat().st_mtime, item.name),
        )
        if not candidates:
            raise FacebookGroupsError(
                f"[{self.source}] no data from source: no {POSTS_FILE_GLOB} file in "
                f"{self.cache_dir} (transport did not run)"
            )
        return candidates[-1]

    def _read_payload(self, path: Path) -> Mapping[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FacebookGroupsError(
                f"[{self.source}] parse error: cannot read {path}: {exc}"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FacebookGroupsError(
                f"[{self.source}] parse error: {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise FacebookGroupsError(
                f"[{self.source}] parse error: {path} must contain a JSON object"
            )
        return payload

    def _payload_groups(self, payload: Mapping[str, Any], path: Path) -> list[Mapping[str, Any]]:
        entries = payload.get("groups")
        if not isinstance(entries, list):
            raise FacebookGroupsError(
                f"[{self.source}] parse error: {path} has no 'groups' list"
            )
        return [entry for entry in entries if isinstance(entry, Mapping)]

    def _check_freshness(self, payload: Mapping[str, Any], path: Path) -> None:
        if not self.check_freshness:
            return
        collected_at = _parse_time(payload.get("collected_at"))
        if collected_at is None:
            collected_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        age = self._now() - collected_at
        if age > timedelta(hours=self.max_age_hours):
            hours = age.total_seconds() / 3600
            raise FacebookGroupsError(
                f"[{self.source}] no data from source: {path.name} is {hours:.1f} h old "
                f"(limit {self.max_age_hours:g} h) — the fetch step did not run"
            )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # Python 3.11+ accepts the actors' trailing "Z" directly.
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _first_count(*values: Any) -> int | None:
    """First available counter, so a real ``0`` is never replaced by a missing field."""
    for value in values:
        counted = _count(value)
        if counted is not None:
            return counted
    return None


def _title(group_name: str, text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = _TITLE_NOISE_RE.sub("", first_line)
    sentence = _SENTENCE_SPLIT_RE.split(first_line, maxsplit=1)[0].strip()
    if len(sentence) > _TITLE_LIMIT:
        sentence = sentence[: _TITLE_LIMIT - 1].rstrip() + "…"
    if not sentence:
        sentence = text.strip()[:_TITLE_LIMIT]
    return f"{group_name}: {sentence}" if sentence else group_name


def facebook_group_summary(groups: Sequence[FacebookGroup]) -> dict[str, int]:
    """Counts per tag, handy for operations output and tests."""
    summary: dict[str, int] = {}
    for group in groups:
        summary[group.tag] = summary.get(group.tag, 0) + 1
    return summary


__all__ = [
    "ALLOWED_LANGS",
    "ALLOWED_TAGS",
    "ALLOWED_VIEWS",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_GROUPS_CONFIG",
    "DEFAULT_MAX_AGE_HOURS",
    "FACEBOOK_GROUPS_SOURCE",
    "FacebookGroup",
    "FacebookGroups",
    "FacebookGroupsConfigError",
    "FacebookGroupsError",
    "GroupReport",
    "facebook_group_summary",
    "group_key",
    "load_groups",
]
