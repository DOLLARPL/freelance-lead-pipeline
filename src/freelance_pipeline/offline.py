from __future__ import annotations

import json
from pathlib import Path

from freelance_pipeline.sources.facebook import FacebookGroups
from freelance_pipeline.sources.justjoinit import JustJoinITJobs
from freelance_pipeline.sources.make import MakeHireHelp
from freelance_pipeline.sources.n8n import N8NCommunityJobs
from freelance_pipeline.sources.n8ndevs import N8NDevsJobs
from freelance_pipeline.sources.nofluffjobs import NoFluffJobsJobs
from freelance_pipeline.sources.oferteo import OferteoJobs
from freelance_pipeline.sources.oferteo_mail import OferteoMailJobs
from freelance_pipeline.sources.rocketjobs import RocketJobsJobs
from freelance_pipeline.sources.useme import UsemeJobs

USEME_PAGE0 = "https://useme.com/pl/jobs/category/programowanie-i-it,35/?order_by=-published_on"
USEME_PAGE1 = (
    "https://useme.com/pl/jobs/category/programowanie-i-it,35/?order_by=-published_on&page=2"
)

# source name -> (adapter factory kwargs session key, {url: fixture filename})
_FIXTURE_ROUTES: dict[str, tuple[object, dict[str, str]]] = {
    "n8n_community_jobs": (N8NCommunityJobs, {"https://community.n8n.io/c/jobs.rss": "n8n_jobs.rss"}),
    "make_hire_help": (MakeHireHelp, {"https://community.make.com/c/hire-a-pro.json": "make_hire_pro.json"}),
    "n8ndevs": (N8NDevsJobs, {"https://n8ndevs.com/jobs": "n8ndevs_jobs.html"}),
    "nofluffjobs": (NoFluffJobsJobs, {"https://nofluffjobs.com": "nofluffjobs_listing.html"}),
    # The fixture holds real Oferteo cards (from the website lane) served under
    # the adapter's default lane URL, so offline mode stays network-free while
    # the parsed markup keeps being genuine.
    "oferteo": (OferteoJobs, {"https://www.oferteo.pl/zlecenia-it": "oferteo_listing_page1.html"}),
    "rocketjobs": (RocketJobsJobs, {"https://rocketjobs.pl/oferty-pracy": "rocketjobs_listing.html"}),
    "justjoinit": (JustJoinITJobs, {"https://justjoin.it/job-offers/all-locations": "justjoinit_listing.html"}),
    "useme": (
        UsemeJobs,
        {
            USEME_PAGE0: "useme_listing_page1.html",
            USEME_PAGE1: "useme_listing_page2.html",
        },
    ),
}


# Sources whose adapter reads a local transport payload instead of HTTP: the
# Facebook groups adapter never calls a network endpoint (account-safety rule),
# so its offline form is a frozen pilot payload plus a fixture group list.
_FIXTURE_FILE_ARGS: dict[str, tuple[object, dict[str, str]]] = {
    "facebook_groups": (
        FacebookGroups,
        {
            "posts_path": "facebook_posts_blackfalcondata.json",
            "groups_config_path": "facebook_groups.yaml",
        },
    ),
}


#: Źródła czytające skrzynkę zamiast HTTP: ich offline forma to gotowe maile z fixture'ów.
_FIXTURE_MAILS: dict[str, tuple[tuple[str, str], ...]] = {
    "oferteo_mail": (
        ("oferteo_mail_new.html", "Zapytanie o wykonanie strony www, Warszawa - nr 18344970"),
        ("oferteo_mail_revealed.html", "☎ Zapytanie o wykonanie strony www, Warszawa - 18344970 Zadzwoń"),
    ),
}


class _OfflineMailbox:
    """Podstawia ``Mailbox``: zwraca maile z fixture'ów, bez sieci i bez tokenu."""

    def __init__(self, messages: list[object]) -> None:
        self._messages = messages

    def fetch(self, *, since=None, limit=None, known_ids=()):  # noqa: ANN001, ARG002
        return list(self._messages)[: (limit or len(self._messages))]


class _OfflineResponse:
    """Object protocol subset of ``requests.Response`` used by the adapters."""

    status_code = 200

    def __init__(self, text: str, *, url: str = "") -> None:
        self.text = text
        self.url = url

    def json(self):
        return json.loads(self.text)


class _OfflineSession:
    """Maps exact URLs to fixture text; errors on anything unregistered."""

    def __init__(self, routes: dict[str, _OfflineResponse]) -> None:
        self._routes = routes

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self._routes:
            raise AssertionError(f"offline session has no fixture for {url!r}")
        return self._routes[url]


def _build_mails(root: Path, mails: tuple[tuple[str, str], ...]) -> list[object]:
    """Zamień pliki HTML na ``MailMessage`` z numerem zapytania w temacie."""
    from datetime import UTC, datetime

    from freelance_pipeline.mailbox import MailMessage

    built = []
    for index, (filename, subject) in enumerate(mails):
        built.append(
            MailMessage(
                message_id=f"offline-{index}-{filename}",
                thread_id=f"offline-thread-{index}",
                received_at=datetime(2026, 10, 28, 8, 0, tzinfo=UTC),
                subject=subject,
                from_addr="powiadomienie@oferteo.pl",
                html=(root / filename).read_text(encoding="utf-8"),
            )
        )
    return built


def build_offline_adapters(fixtures_dir: str | Path) -> dict[str, object]:
    """Instantiate every adapter against canned fixture payloads.

    ``fixtures_dir`` is read-only: the returned adapters only ever make
    in-memory (fake-session) requests or read a frozen transport payload, so
    running the pipeline with them can never touch production caches, the
    network or any secret file.
    """
    root = Path(fixtures_dir)
    adapters: dict[str, object] = {}
    for name, (factory, routes) in _FIXTURE_ROUTES.items():
        session = _OfflineSession(
            {url: _OfflineResponse((root / file).read_text(encoding="utf-8"), url=url) for url, file in routes.items()}
        )
        session.calls = []
        adapters[name] = factory(session=session)  # type: ignore[call-arg]
    for name, mails in _FIXTURE_MAILS.items():
        adapters[name] = OferteoMailJobs(mailbox=_OfflineMailbox(_build_mails(root, mails)))
    for name, (factory, file_args) in _FIXTURE_FILE_ARGS.items():
        adapters[name] = factory(  # type: ignore[call-arg,operator]
            **{key: str(root / value) for key, value in file_args.items()},
            check_freshness=False,
        )
    return adapters


__all__ = ["build_offline_adapters"]