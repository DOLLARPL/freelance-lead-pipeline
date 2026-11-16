"""Snajper FB — transport: jeden run Apify → cache/snajper-v2/facebook/posts-<data>.json.

Ten skrypt jest **jedynym** miejscem w Snajperze, które rozmawia z Apify (i nigdy
z facebook.com). Uruchamia run aktora na grupach z ``config/facebook_groups.yaml``
z ``enabled: true``, czeka na zakończenie, grupuje surowe itemy per grupa i zapisuje
plik w formacie, który czyta adapter ``freelance_pipeline.sources.facebook``
(HANDOFF „Etap 2 (adapter FB)", kontrakt wejścia transport → adapter).

Zasady, które ten skrypt trzyma:

* **Token tylko z pliku.** ``APIFY_API_TOKEN`` jest czytany z ``/opt/data/.env``
  (``--env-file``) i wysyłany w nagłówku ``Authorization``, nigdy w URL-u, nigdy na
  stdout/stderr i nigdy w argv (argv widać w ``ps``).
* **Zamówienie aktorów — zmienione 2026-09-21 (pomiar, patrz stała ``PRIMARY_ACTOR``):**
  ``apify/facebook-groups-scraper`` jako primary (oddaje same posty, $0.005/post na
  FREE), ``blackfalcondata`` wyłącznie jako aktor diagnostyczny (``--actor``), bo jego
  dataset miesza posty z komentarzami. Domyślnie jeden aktor = jedna płatność;
  ``--fallback`` dołącza drugiego aktora świadomie.
* **Status każdej grupy osobno.** Grupa bez itemów to ``empty``, item diagnostyczny
  (np. ``no_items`` przy grupie prywatnej) to ``diagnostic`` — nic nie znika po cichu.
* **Brak danych = widoczny błąd.** Transport nie ruszył (wszystkie próby padły) ⇒
  kod wyjścia 2 i **żadnego pliku** (adapter zgłosi wtedy „transport did not run",
  a nie ciche zero). Run doszedł, ale zero postów z tekstem ⇒ plik powstaje (żeby
  statusy były do przeczytania) i kod wyjścia 3. Konto Apify bez kredytu na ten
  przebieg ⇒ kod wyjścia 4 i **żadnego runu** (start płaci się nawet wtedy, gdy
  aktor odmówi z 402, więc najpierw sprawdzamy `users/me/limits`).
* **Cisza na stdout, gdy wszystko gra.** Shim crona dostarcza stdout w trybie
  ``no_agent``, więc stdout jest pusty przy sukcesie i mówi jednym zdaniem, co się
  stało, tylko przy awarii. Podsumowanie techniczne (per grupa, koszt) idzie na stderr.

Użycie::

    uv run python scripts/fb_fetch.py --dry-run          # plan bez API, bez tokenu
    uv run python scripts/fb_fetch.py                    # pełny przebieg transportu
    uv run python scripts/fb_fetch.py --fallback         # dołóż drugiego aktora (płatny)
    uv run python scripts/fb_fetch.py --actor blackfalcondata/facebook-group-post-scraper
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from freelance_pipeline.sources.facebook import (
    FacebookGroup,
    FacebookGroupsConfigError,
    group_key,
    load_groups,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

TOKEN_ENV_NAME = "APIFY_API_TOKEN"
APIFY_API_BASE = "https://api.apify.com/v2"
DEFAULT_ENV_FILE = Path("/opt/data/.env")
DEFAULT_POSTS_DIR = Path("/opt/data/cache/snajper-v2/facebook")
DEFAULT_GROUPS_CONFIG = REPO_ROOT / "config" / "facebook_groups.yaml"
POSTS_FILE_PREFIX = "posts-"

# Kolejność aktorów — zmieniona 2026-09-21 po pomiarze (etap 3, kalibracja).
#
# Stage 1 rekomendował blackfalcondata (~$0.0018/item, bogatsze pola), ale jego dataset
# miesza posty z komentarzami i śmieciem UI. W pliku produkcyjnym z 2026-09-21 tylko 9 z
# 77 itemów miało w ``groupTitle`` nazwę grupy; pozostałe niosą tam nazwę autora posta, pod
# którym wiszą, a w tekstach są jednoznaczne komentarze i artefakty Facebooka
# („Napisałam na priv", „Portfolio na priv, klasyk", „New posts", „jakie miasto?"), które
# adapter zamieniał w leady. Flaga ``includeComments`` ze schematu aktora (default false)
# **nie zmienia zachowania**: dwa runy 2026-09-21, z flagą i bez, oddały identyczny zbiór
# 58 itemów. Efektywny koszt czystego posta jest więc wielokrotnie wyższy niż $0.0018.
#
# Oficjalny aktor ``apify/facebook-groups-scraper`` oddaje wyłącznie posty (pilot etapu 1:
# 115 itemów = 100 + 14 postów, ``groupTitle`` = nazwa grupy) i kosztuje $0.005/post na
# planie FREE (plus $0.002/post, gdy używamy ``onlyPostsNewerThan``). Dlatego jest primary,
# a blackfalcondata zostaje w kodzie jako aktor diagnostyczny (``--actor``).
PRIMARY_ACTOR = "apify/facebook-groups-scraper"
# Nieużywany domyślnie: dataset miesza komentarze (patrz wyżej). Włączany ręcznie
# przez ``--actor blackfalcondata/...`` albo ``--fallback``, gdy trzeba sprawdzić
# sam transport, a nie treść.
FALLBACK_ACTOR = "blackfalcondata/facebook-group-post-scraper"

# The actor takes one global window/limit; the plan (section 6) budgets ~25-30
# posts per group per day, so the limit is the sum of the groups' own max_posts,
# capped so a config typo cannot turn into a four-figure Apify bill.
MAX_RESULTS_LIMIT = 400
DEFAULT_NEWER_THAN = "2 days"
DEFAULT_VIEW = "CHRONOLOGICAL"

POLL_EVERY_S = 10
POLL_MAX_S = 900
HTTP_TIMEOUT_S = 60
HTTP_RETRIES = 3
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")

EXIT_OK = 0
EXIT_NO_DATA = 2
EXIT_NO_POSTS = 3
# Brak kredytu Apify na koncie (FREE: $5/mies., nie przechodzi na następny miesiąc).
EXIT_NO_CREDIT = 4

# Szacunek kosztu przebiegu przed jego startem. Zmierzone na planie FREE
# (2026-09-21): $0.005 za post + $0.002 za `onlyPostsNewerThan` + $0.001–0.006 start.
# Bez filtra daty zostaje ~$0.005, ale bierzemy górną wartość — lepiej pominąć
# przebieg, niż zapłacić za start i dostać 402 w połowie.
ESTIMATED_COST_PER_POST_USD = 0.007
CREDIT_MARGIN_USD = 0.02

# Item fields carrying the group a post/diagnostic came from. The two actors name
# them differently (blackfalcondata: inputUrl/facebookUrl, apify: facebookUrl, and
# diagnostics only carry url), so all known spellings are accepted.
_ITEM_GROUP_KEYS = ("inputUrl", "facebookUrl", "groupUrl", "sourceUrl", "group", "url")
_TEXT_KEYS = ("text", "postText", "message", "content", "caption")
_DIAGNOSTIC_KEYS = ("error", "errorDescription", "diagnostic", "code")


class FetchError(Exception):
    """Everything this script refuses to continue with, with a readable reason."""


def _sanitize(message: str, *secrets: str) -> str:
    """Strip secrets from anything that may reach stderr or the cron message."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


# --------------------------------------------------------------------------- env


def read_env_value(path: str | Path, name: str) -> str | None:
    """Read one value from a dotenv file; quotes and surrounding space are stripped."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise FetchError(f"nie mogę przeczytać pliku środowiska {path}: {exc}") from exc
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def load_token(env_file: str | Path) -> str:
    """Load ``APIFY_API_TOKEN`` from the environment file; never log its value."""
    token = read_env_value(env_file, TOKEN_ENV_NAME)
    if not token:
        raise FetchError(
            f"{TOKEN_ENV_NAME} jest pusty albo nie ma go w {env_file} "
            "(etap 0 planu: konto Apify i token do pliku, nigdy do czatu)"
        )
    return token


# ----------------------------------------------------------------------- actors


@dataclass(frozen=True, slots=True)
class ActorSpec:
    """One Apify actor, addressed by its ``owner/name`` identifier."""

    name: str

    @property
    def slug(self) -> str:
        """Actor id used in API paths (``owner~name``)."""
        return self.name.replace("/", "~")


@dataclass(slots=True)
class ActorRun:
    """Outcome of one actor run: raw dataset items plus run metadata."""

    actor: str
    slug: str
    run_id: str | None = None
    status: str | None = None
    cost_usd: float | None = None
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def posts_with_text(self) -> int:
        return sum(1 for item in self.items if item_text(item))


def item_text(item: Mapping[str, Any]) -> str:
    """Post text, whatever the actor called the field; empty for photo-only posts."""
    for key in _TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("body")
        if isinstance(value, str) and value.strip():
            return value
    return ""


def item_group_url(item: Mapping[str, Any]) -> str | None:
    """Group a post/diagnostic belongs to, or ``None`` when the item names none."""
    for key in _ITEM_GROUP_KEYS:
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("url") or value.get("name")
        if isinstance(value, str) and "/groups/" in value:
            return value
    return None


def item_diagnostic(item: Mapping[str, Any]) -> str | None:
    """Diagnostic note of an item that carries no post, e.g. ``no_items``."""
    if item_text(item):
        return None
    for key in _DIAGNOSTIC_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --------------------------------------------------------------------- payloads


@dataclass(frozen=True, slots=True)
class RunInput:
    """The actor input plus notes about what could not be expressed in it."""

    payload: dict[str, Any]
    note: str | None = None


def build_actor_input(
    groups: Sequence[FacebookGroup], *, newer_than: str = DEFAULT_NEWER_THAN
) -> RunInput:
    """Build the actor input for the enabled groups.

    ``viewOption`` is global in this actor family, so a config whose groups ask for
    different views cannot be honoured literally — the run then uses
    ``CHRONOLOGICAL`` and says so in the payload (``view_note``), instead of
    pretending the config was respected.
    """
    urls = [group.url for group in groups]
    limit = min(sum(group.max_posts for group in groups), MAX_RESULTS_LIMIT)
    views = sorted({group.view for group in groups})
    view = views[0] if len(views) == 1 else DEFAULT_VIEW
    note = None
    if len(views) > 1:
        note = (
            f"config podaje różne view ({', '.join(views)}), "
            f"aktor przyjmuje jedno — użyto {DEFAULT_VIEW}"
        )
    return RunInput(
        payload={
            "startUrls": [{"url": url} for url in urls],
            "resultsLimit": limit,
            "viewOption": view,
            "onlyPostsNewerThan": newer_than,
        },
        note=note,
    )


def group_payloads(
    groups: Sequence[FacebookGroup],
    items: Sequence[Mapping[str, Any]],
    *,
    actor_status: str | None = None,
) -> list[dict[str, Any]]:
    """Group raw actor items per configured group, with a status for every group.

    Posty trafiają do ``posts`` dosłownie tak, jak wróciły z aktora (adapter sam
    odrzuca te bez tekstu i liczy je w statusie). Grupa bez itemów to ``empty``,
    grupa z notką diagnostyczną i bez postów to ``diagnostic`` — oba przypadki są
    w pliku, więc raport pokaże je per grupa.
    """
    enabled = {group.key: group for group in groups}
    posts: dict[str, list[Mapping[str, Any]]] = {key: [] for key in enabled}
    notes: dict[str, list[str]] = {key: [] for key in enabled}
    unassigned: list[Mapping[str, Any]] = []

    for item in items:
        url = item_group_url(item)
        key = group_key(url) if url else None
        if key not in enabled:
            unassigned.append(item)
            continue
        diagnostic = item_diagnostic(item)
        if diagnostic and not item_text(item):
            notes[key].append(diagnostic)
            continue
        posts[key].append(item)

    entries: list[dict[str, Any]] = []
    for key, group in enabled.items():
        group_posts = posts[key]
        group_notes = notes[key]
        if group_posts:
            status = "ok"
        elif group_notes:
            status = "diagnostic"
        else:
            status = "empty"
        entries.append(
            {
                "url": group.url,
                "name": group.name,
                "status": status,
                "actor_status": actor_status,
                "diagnostic": "; ".join(group_notes) if (group_notes and not group_posts) else None,
                "error": None,
                "notes": group_notes or None,
                "posts": list(group_posts),
            }
        )
    if unassigned:
        entries.append(
            {
                "url": str(item_group_url(unassigned[0]) or ""),
                "name": "(itemy spoza configu)",
                "status": "unassigned",
                "actor_status": actor_status,
                "diagnostic": None,
                "error": None,
                "notes": ["grupa spoza config/facebook_groups.yaml"],
                "posts": [dict(item) for item in unassigned],
            }
        )
    return entries


def render_status_lines(entries: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """One line per group, mirroring the adapter's own ``status_lines()``."""
    lines = []
    for entry in entries:
        posts = entry.get("posts")
        posts = posts if isinstance(posts, list) else []
        with_text = sum(1 for post in posts if isinstance(post, Mapping) and item_text(post))
        note = entry.get("diagnostic") or entry.get("error") or ""
        line = (
            f"{entry.get('name')} ({str(entry.get('url', '')).rsplit('/', 1)[-1]}): "
            f"{entry.get('status')} — {len(posts)} itemów, {with_text} z tekstem"
        )
        if note:
            line += f" [{note}]"
        lines.append(line)
    return tuple(lines)


def build_payload(
    run: ActorRun,
    entries: Sequence[Mapping[str, Any]],
    *,
    run_input: RunInput,
    groups: Sequence[FacebookGroup],
    attempts: Sequence[Mapping[str, Any]],
    collected_at: datetime,
) -> dict[str, Any]:
    """The persisted transport payload — the whole contract the adapter reads."""
    return {
        "collected_at": collected_at.isoformat(),
        "actor": run.actor,
        "actor_slug": run.slug,
        "run_id": run.run_id,
        "status": run.status,
        "cost_usd": run.cost_usd,
        "groups_requested": [group.url for group in groups],
        "input": run_input.payload,
        "view_note": run_input.note,
        "attempts": [dict(attempt) for attempt in attempts],
        "groups": [dict(entry) for entry in entries],
    }


def _post_identity(post: Mapping[str, Any]) -> str | None:
    for name in ("legacyId", "id"):
        value = post.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = post.get("url")
    return value.strip() if isinstance(value, str) and value.strip() else None


def merge_payload(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Scal dzisiejszy przebieg z poprzednim plikiem zamiast go nadpisywać.

    Transport pisał jeden plik na dobę, a adapter czyta najnowszy po mtime.
    Przy przebiegu co godzinę druga godzina kasowała posty pierwszej i część
    ofert nigdy nie docierała do kolejki. Posty łączymy po ``legacyId``/``id``/
    ``url``; grupa, która teraz wróciła pusta, zachowuje wcześniejsze posty.
    ``cost_usd`` sumuje się w obrębie doby, żeby rachunek za Apify był widoczny
    w pliku, a nie tylko w panelu.
    """
    merged = dict(current)
    old_groups = {
        str(entry.get("url")): entry
        for entry in previous.get("groups") or []
        if isinstance(entry, Mapping)
    }
    groups: list[dict[str, Any]] = []
    for entry in current.get("groups") or []:
        if not isinstance(entry, Mapping):
            continue
        entry = dict(entry)
        old = old_groups.get(str(entry.get("url")))
        if old:
            old_posts = [post for post in old.get("posts") or [] if isinstance(post, Mapping)]
            seen = {_post_identity(post) for post in old_posts}
            seen.discard(None)
            fresh = [
                post
                for post in entry.get("posts") or []
                if _post_identity(post) not in seen
            ]
            entry["posts"] = old_posts + fresh
            if entry["posts"] and entry.get("status") == "empty":
                entry["status"] = "ok"
            if not entry.get("notes") and old.get("notes"):
                entry["notes"] = old["notes"]
        groups.append(entry)
    known = {str(entry.get("url")) for entry in groups}
    for url, old in old_groups.items():
        if url not in known:  # grupa wypadła z configu, ale jej posty zostają w pliku
            groups.append(dict(old))
    merged["groups"] = groups
    try:
        merged["cost_usd"] = round(
            float(previous.get("cost_usd") or 0) + float(current.get("cost_usd") or 0), 4
        )
    except (TypeError, ValueError):
        pass
    return merged


def write_payload(payload: Mapping[str, Any], posts_dir: str | Path, *, stamp: str) -> Path:
    """Write ``posts-<data>.json`` atomically; returns the final path."""
    directory = Path(posts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{POSTS_FILE_PREFIX}{stamp}.json"
    temporary = directory / f".{final.name}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(temporary, final)
    return final


# ---------------------------------------------------------------------- client


class ApifyClient:
    """Thin Apify REST client — the only network boundary of this script.

    The token travels in the ``Authorization`` header (not in the query string, so
    it never lands in logs of proxies or in an exception message) and is stripped
    from every error string before it can reach a terminal.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = APIFY_API_BASE,
        session: Any | None = None,
        timeout: int = HTTP_TIMEOUT_S,
        retries: int = HTTP_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self.base_url = base_url.rstrip("/")
        self._session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep

    def call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        """One API call with bounded retries; raises :class:`FetchError` on failure."""
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        last_error = "brak odpowiedzi"
        for attempt in range(1, self.retries + 1):
            try:
                response = self._session.request(
                    method, url, json=payload, headers=headers, timeout=self.timeout
                )
                status = getattr(response, "status_code", 200)
                if status >= 400:
                    last_error = f"HTTP {status}: {self._body(response)[:300]}"
                    if status < 500 and status != 429:
                        break
                else:
                    body = self._body(response)
                    return json.loads(body) if body else {}
            except (requests.RequestException, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                self._sleep(float(attempt))
        raise FetchError(_sanitize(f"{method} {path} nie powiódł się: {last_error}", self._token))

    def run_actor(
        self,
        spec: ActorSpec,
        actor_input: Mapping[str, Any],
        *,
        poll_every: int = POLL_EVERY_S,
        poll_max: int = POLL_MAX_S,
    ) -> ActorRun:
        """Start one run, wait for a terminal status and fetch its dataset items."""
        started = self.call("POST", f"/acts/{spec.slug}/runs", actor_input)
        run = started.get("data") if isinstance(started, Mapping) else None
        run = run if isinstance(run, Mapping) else {}
        run_id = run.get("id")
        dataset_id = run.get("defaultDatasetId")
        status = run.get("status")
        cost = run.get("usageTotalUsd")

        waited = 0
        while run_id and status not in TERMINAL_STATUSES and waited < poll_max:
            self._sleep(float(poll_every))
            waited += poll_every
            fetched = self.call("GET", f"/actor-runs/{run_id}")
            data = fetched.get("data") if isinstance(fetched, Mapping) else None
            data = data if isinstance(data, Mapping) else {}
            status = data.get("status") or status
            dataset_id = data.get("defaultDatasetId") or dataset_id
            cost = data.get("usageTotalUsd", cost)

        items = self.dataset_items(dataset_id) if dataset_id else []
        return ActorRun(
            actor=spec.name,
            slug=spec.slug,
            run_id=str(run_id) if run_id else None,
            status=str(status) if status else None,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            items=items,
        )

    def dataset_items(self, dataset_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Read the run's dataset; a non-list body is treated as an honest zero."""
        fetched = self.call(
            "GET", f"/datasets/{dataset_id}/items?clean=true&limit={int(limit)}"
        )
        if isinstance(fetched, list):
            return [item for item in fetched if isinstance(item, dict)]
        if isinstance(fetched, Mapping):
            items = fetched.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    @staticmethod
    def _body(response: Any) -> str:
        return str(getattr(response, "text", "") or "")


# ------------------------------------------------------------------- transport


@dataclass(slots=True)
class FetchOutcome:
    """Result of the actor chase: the run we kept, plus every attempt for the file."""

    run: ActorRun | None
    attempts: list[dict[str, Any]]
    errors: list[str]
    run_input: RunInput
    groups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def posts_with_text(self) -> int:
        return self.run.posts_with_text if self.run is not None else 0


def fetch_posts(
    transport: Any,
    groups: Sequence[FacebookGroup],
    actors: Sequence[ActorSpec],
    *,
    newer_than: str = DEFAULT_NEWER_THAN,
    poll_every: int = POLL_EVERY_S,
    poll_max: int = POLL_MAX_S,
) -> FetchOutcome:
    """Try the actors in order and keep the first run that brought text posts.

    ``apify`` fallback is paid, so it only runs when the primary actor failed or
    returned nothing readable — the plan's „actor pada po zmianie markupu" risk.
    A run that merely returned zero posts is kept as a last resort so the caller
    can still write the per-group statuses and fail loudly.
    """
    run_input = build_actor_input(groups, newer_than=newer_than)
    attempts: list[dict[str, Any]] = []
    errors: list[str] = []
    chosen: ActorRun | None = None
    honest_zero: ActorRun | None = None

    for spec in actors:
        try:
            run = transport.run_actor(
                spec, run_input.payload, poll_every=poll_every, poll_max=poll_max
            )
        except FetchError as exc:
            attempts.append({"actor": spec.name, "status": "ERROR", "error": str(exc)})
            errors.append(f"{spec.name}: {exc}")
            continue
        attempts.append(
            {
                "actor": spec.name,
                "run_id": run.run_id,
                "status": run.status,
                "cost_usd": run.cost_usd,
                "items": len(run.items),
                "posts_with_text": run.posts_with_text,
            }
        )
        if run.posts_with_text:
            chosen = run
            break
        honest_zero = run
        errors.append(
            f"{spec.name}: run {run.run_id} zakończony ({run.status}), "
            f"{len(run.items)} itemów, zero postów z tekstem"
        )

    outcome = FetchOutcome(
        run=chosen or honest_zero, attempts=attempts, errors=errors, run_input=run_input
    )
    if outcome.run is not None:
        outcome.groups = group_payloads(
            groups, outcome.run.items, actor_status=outcome.run.status
        )
    return outcome


# ------------------------------------------------------------------------ main


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fb_fetch.py",
        description="Jeden run Apify na grupach FB z configu → posts-<data>.json dla adaptera.",
    )
    parser.add_argument("--groups-config", type=Path, default=DEFAULT_GROUPS_CONFIG)
    parser.add_argument("--posts-dir", type=Path, default=DEFAULT_POSTS_DIR)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--actor",
        action="append",
        default=None,
        help=f"nadpisz aktora (domyślnie tylko {PRIMARY_ACTOR})",
    )
    parser.add_argument(
        "--fallback",
        action="store_true",
        help=(
            "dołóż drugiego aktora po primary (płatny, a jego dataset miesza komentarze) "
            "— domyślnie przebieg to jeden aktor"
        ),
    )
    parser.add_argument("--newer-than", default=DEFAULT_NEWER_THAN)
    parser.add_argument(
        "--quiet-empty",
        action="store_true",
        help="godzinowy przebieg bez nowych postów kończy się cicho (kod 0, zero stdout)",
    )
    parser.add_argument("--poll-every", type=int, default=POLL_EVERY_S)
    parser.add_argument("--poll-max", type=int, default=POLL_MAX_S)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="pokaż plan (grupy, aktorzy, input, plik) bez wołania API i bez tokenu",
    )
    return parser.parse_args(argv)


def run_cost_estimate(run_input: RunInput) -> float:
    """Conservative USD estimate of one transport run, before it is started."""
    limit = run_input.payload.get("resultsLimit")
    posts = limit if isinstance(limit, int) and limit > 0 else 0
    return posts * ESTIMATED_COST_PER_POST_USD + CREDIT_MARGIN_USD


def credit_shortfall(client: Any, run_input: RunInput) -> str | None:
    """Why this run must not start (Apify credit), or ``None`` when it may.

    FREE-plan credit does not roll over between months, so a run started on a
    near-empty account dies with HTTP 402 (live 2026-09-21: three parallel starts
    were refused exactly this way) while still paying the actor-start event. The
    check is advisory: if the limits endpoint itself cannot be read, the run goes
    ahead — a broken diagnostics endpoint must not stop the transport.
    """
    try:
        payload = client.call("GET", "/users/me/limits")
    except FetchError:
        return None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    limits = data.get("limits")
    current = data.get("current")
    if not isinstance(limits, Mapping) or not isinstance(current, Mapping):
        return None
    total = limits.get("maxMonthlyUsageUsd")
    used = current.get("monthlyUsageUsd")
    if not isinstance(total, (int, float)) or not isinstance(used, (int, float)):
        return None
    left = float(total) - float(used)
    need = run_cost_estimate(run_input)
    if left >= need:
        return None
    limit = run_input.payload.get("resultsLimit")
    return (
        f"za mało kredytu Apify: zostało ${left:.3f}, a przebieg na {limit} postów "
        f"potrzebuje ~${need:.2f}. Transport pominięty, żaden run nie został startowany "
        "(adapter FB zgłosi brak danych ze źródła). Kredyt odnawia się 7. dnia miesiąca."
    )


def actor_specs(args: argparse.Namespace) -> tuple[ActorSpec, ...]:
    if args.actor:
        return tuple(ActorSpec(name=name) for name in args.actor)
    if args.fallback:
        return (ActorSpec(name=PRIMARY_ACTOR), ActorSpec(name=FALLBACK_ACTOR))
    return (ActorSpec(name=PRIMARY_ACTOR),)


def enabled_groups(config_path: Path) -> tuple[FacebookGroup, ...]:
    groups = load_groups(config_path)
    enabled = tuple(group for group in groups if group.enabled)
    if not enabled:
        raise FetchError(
            f"żadna grupa w {config_path} nie ma enabled: true — transport nie ma na czym pracować"
        )
    return enabled


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Any | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    args = parse_args(argv)
    clock = now if now is not None else (lambda: datetime.now(UTC))
    specs = actor_specs(args)

    try:
        groups = enabled_groups(args.groups_config)
        run_input = build_actor_input(groups, newer_than=args.newer_than)
    except (FacebookGroupsConfigError, FetchError) as exc:
        # A config typo must be loud: a silently empty plan would either skip a
        # group or start a paid run on nothing.
        return _fail(f"snajper-fb: konfiguracja grup nie przeszła: {exc}", EXIT_NO_DATA)

    collected_at = clock()
    stamp = collected_at.strftime("%Y%m%d")
    target = Path(args.posts_dir) / f"{POSTS_FILE_PREFIX}{stamp}.json"

    if args.dry_run:
        print(f"grupy (enabled): {len(groups)}")
        for group in groups:
            print(f"  {group.url} — {group.name} [{group.tag}, max {group.max_posts}, {group.view}]")
        print(f"aktorzy: {', '.join(spec.name for spec in specs)}")
        print(f"input: {json.dumps(run_input.payload, ensure_ascii=False)}")
        if run_input.note:
            print(f"uwaga: {run_input.note}")
        print(f"plik docelowy: {target}")
        return EXIT_OK

    try:
        token = load_token(args.env_file)
    except FetchError as exc:
        return _fail(f"snajper-fb: {exc}", EXIT_NO_DATA)

    client = transport if transport is not None else ApifyClient(token=token)
    shortfall = credit_shortfall(client, run_input)
    if shortfall is not None:
        # Cichy pominięty przebieg: log na stderr, **pusty stdout**, więc cron
        # no_agent nic nie wyśle. Kredyt odnawia się 7. dnia miesiąca, więc
        # codzienna wiadomość „brak kredytu” byłaby czystym szumem przez dwa
        # tygodnie. Zero postów, zero pliku ⇒ adapter zgłosi brak danych źródła.
        return _skip(f"snajper-fb: {shortfall}", EXIT_NO_CREDIT)
    outcome = fetch_posts(
        client,
        groups,
        specs,
        newer_than=args.newer_than,
        poll_every=args.poll_every,
        poll_max=args.poll_max,
    )

    if outcome.run is None:
        detail = "; ".join(outcome.errors) or "brak szczegółów"
        return _fail(
            f"snajper-fb: brak danych z transportu Apify — {detail}",
            EXIT_NO_DATA,
        )

    payload = build_payload(
        outcome.run,
        outcome.groups,
        run_input=outcome.run_input,
        groups=groups,
        attempts=outcome.attempts,
        collected_at=collected_at,
    )
    # Ten sam plik dnia jest nadpisywany co godzinę, więc bez scalenia druga
    # godzina kasowałaby posty pierwszej (adapter czyta najnowszy plik po mtime).
    target_path = Path(args.posts_dir) / f"{POSTS_FILE_PREFIX}{stamp}.json"
    if target_path.is_file():
        try:
            previous = json.loads(target_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, Mapping):
            payload = merge_payload(previous, payload)
    path = write_payload(payload, args.posts_dir, stamp=stamp)
    status_lines = render_status_lines(outcome.groups)

    print(f"snajper-fb: {path.name}: {outcome.posts_with_text} postów z tekstem, "
          f"koszt ${outcome.run.cost_usd}", file=sys.stderr)
    for line in status_lines:
        print(f"  {line}", file=sys.stderr)

    if not outcome.posts_with_text:
        detail = "; ".join(outcome.errors) or "brak szczegółów"
        if args.quiet_empty:
            return _skip(
                f"snajper-fb: zero nowych postów z tekstem w tej godzinie — {detail}", EXIT_OK
            )
        print(
            f"SNAJPER FB: transport wrócił bez ani jednego postu z tekstem — {detail}. "
            "Statusy per grupa: " + " | ".join(status_lines)
        )
        return EXIT_NO_POSTS
    return EXIT_OK


def _fail(message: str, code: int) -> int:
    """Visible failure: stderr for the log, stdout for the cron message."""
    print(message, file=sys.stderr)
    print(message)
    return code


def _skip(message: str, code: int) -> int:
    """Silent skip: stderr only, **no stdout**, so ``no_agent`` stays quiet.

    Used for conditions that are known, temporary and self-resolving — today that
    is only the Apify credit brake (credit renews on the 7th of the month). The
    cron must not send the same "brak kredytu" line to Telegram every morning for
    two weeks; the reason is in the log, and the missing rows are visible in the
    run's own source status either way.
    """
    print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
