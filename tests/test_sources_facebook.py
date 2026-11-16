"""Stage 2 adapter tests: Facebook groups → JobLead, offline on stage-1 fixtures.

The payloads in ``tests/fixtures/facebook_posts_*.json`` are trimmed, verbatim
subsets of the real pilot runs of 2026-09-21 (``data/fb-pilot-*.json``), so the
field shapes are the actors' own, not invented. No test touches the network, the
production cache or facebook.com.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_pipeline.registry import (
    ACTIVE_SOURCES,
    DEFAULT_ADAPTERS,
    INACTIVE_SOURCES,
    build_adapter,
)
from freelance_pipeline.sources.facebook import (
    DEFAULT_GROUPS_CONFIG,
    FACEBOOK_GROUPS_SOURCE,
    FacebookGroups,
    FacebookGroupsConfigError,
    FacebookGroupsError,
    group_key,
    load_groups,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = FIXTURES / "facebook_groups.yaml"
BLACKFALCON = "facebook_posts_blackfalcondata.json"
APIFY = "facebook_posts_apify.json"
PROGRAMISCI = "facebook_posts_programiscipoland.json"
TRANSPORT_BLOCKED = "facebook_posts_transport_blocked.json"

# Pilot payload timestamps (collected_at of the real runs).
BLACKFALCON_AT = datetime(2026, 9, 21, 12, 48, 51, tzinfo=UTC)
APIFY_AT = datetime(2026, 9, 21, 12, 47, 30, tzinfo=UTC)

# Enabled in tests/fixtures/facebook_groups.yaml: programiscipoland,
# dlaprzedsiebiorcow, 417702252034285. Disabled there: socialmediapracazlecenia
# (enabled in production) and przygodyprzedsiebiorcow (private).
BLACKFALCON_LEADS = 8  # 4 text posts in dlaprzedsiebiorcow + 4 in 4177
APIFY_LEADS = 8  # same two groups; przygodyprzedsiebiorcow errored
PROGRAMISCI_LEADS = 4


def build(payload: str, **kwargs) -> FacebookGroups:
    """Adapter over a frozen pilot payload; freshness off unless a test wants it."""
    kwargs.setdefault("check_freshness", False)
    return FacebookGroups(
        posts_path=FIXTURES / payload, groups_config_path=CONFIG, **kwargs
    )


def payload(posts_groups: list[dict], *, collected_at: datetime | None = APIFY_AT) -> dict:
    return {
        "collected_at": collected_at.isoformat() if collected_at else None,
        "actor": "blackfalcondata/facebook-group-post-scraper",
        "groups": posts_groups,
    }


def write_payload(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def post(
    *,
    permalink: str = "https://www.facebook.com/groups/dlaprzedsiebiorcow/permalink/999/",
    text: str | None = "Szukam osoby do wdrożenia automatyzacji faktur.",
    time: str | None = "2026-09-21T12:33:09.000Z",
    author: str | None = "Jan Klient",
    post_id: str = "999",
) -> dict:
    return {
        "url": permalink,
        "portalUrl": permalink,
        "legacyId": post_id,
        "id": post_id,
        "text": text,
        "time": time,
        "user": {"name": author, "id": "1"},
        "commentsCount": 3,
        "sharesCount": 1,
        "likesCount": 5,
        "attachments": [],
    }


# --------------------------------------------------------------------------- #
# Production config
# --------------------------------------------------------------------------- #


def test_production_config_loads_and_is_usable():
    groups = load_groups()
    assert DEFAULT_GROUPS_CONFIG.name == "facebook_groups.yaml"
    # Liczba grup nie jest celem samym w sobie: lista jest wynikiem pomiaru
    # 2026-09-21 (28 kandydatek, GRUPY-FB-RESEARCH.md), nie liczbą z planu (8-12).
    # Sześć grup zmierzonych jako trafne = sześć pozycji w pliku; reszta odrzuconych
    # jest spisana w komentarzu nagłówkowym, żeby powrót był decyzją, nie researchem.
    assert 4 <= len(groups) <= 12
    assert len({g.key for g in groups}) == len(groups)
    assert all(g.tag in {"automatyzacja", "no-code", "ecommerce", "freelancing", "lokalne-warszawa", "founderzy"} for g in groups)
    assert all(g.lang in {"pl", "en"} for g in groups)
    assert all(g.max_posts > 0 for g in groups)
    # Every enabled group is confirmed public: a private group can only ever
    # return a diagnostic note, so enabling one would burn paid runs.
    enabled = [g for g in groups if g.enabled]
    assert enabled
    assert all(g.public for g in enabled)
    # Enabled set = grupy, które w pomiarze oddały realny popyt klienta. Grupa może
    # wypaść z tego zbioru: decyzję o wywaleniu podejmujemy po realnych leadach z niej,
    # nie po przeglądzie postów. 2026-09-23 wypadły dwie grupy social-media
    # (`socialmediapracazlecenia`, `417702252034285`): 39 z 57 postów przebiegu i zero
    # TOP-ów w historii — patrz komentarz w configu. 2026-09-24 (runda 4: discovery
    # + 22 zmierzone kandydatki) weszły dwie: `Excel - Zapytania i Zlecenia`
    # (5–6 z 8 postów to zlecenia na tabele/kalkulatory — grupa nazwana zadaniem)
    # i `Usługi księgowe / doradztwo podatkowe` („szukam kogoś, kto wypełni DRA i RCD").
    # 2026-09-25 wypadła `Usługi księgowe / doradztwo podatkowe`: 25 z 32 postów tej grupy
    # przechodziło politykę jako TOP („Dziś pierwszy dzień jesieni!", „Poznań.", promocje
    # kursów), czyli 14 z 24 pozycji TOP całego przebiegu, a realne zlecenie z profilu
    # zdarzyło się jedno na tydzień. Bramka treści FB została przy tym zaostrzona osobno
    # (testy w `tests/test_scoring.py`), więc grupa wraca tylko z lepszymi kryteriami.
    assert {g.key for g in enabled} == {
        "facebook.com/groups/1264219497906334",
        "facebook.com/groups/szukam.programisty.dev",
        "facebook.com/groups/prestashoppl",
        "facebook.com/groups/wordporesszlecenia",
        "facebook.com/groups/418363537774333",
        "facebook.com/groups/474347724671287",
    }


def test_group_key_normalises_urls():
    assert group_key("https://www.facebook.com/groups/dlaprzedsiebiorcow/") == (
        "facebook.com/groups/dlaprzedsiebiorcow"
    )
    assert group_key("HTTPS://Facebook.com/groups/AbC/?ref=share") == (
        "facebook.com/groups/abc"
    )


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


def valid_entry(**overrides) -> dict:
    entry = {
        "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
        "name": "Grupa dla przedsiębiorców",
        "tag": "founderzy",
        "lang": "pl",
        "public": True,
        "enabled": True,
        "max_posts": 30,
        "view": "CHRONOLOGICAL",
    }
    entry.update(overrides)
    return entry


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "groups.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_rejects_missing_file(tmp_path):
    with pytest.raises(FacebookGroupsConfigError, match="cannot read"):
        load_groups(tmp_path / "nope.yaml")


def test_config_rejects_invalid_yaml(tmp_path):
    with pytest.raises(FacebookGroupsConfigError, match="not valid YAML"):
        load_groups(write_config(tmp_path, "url: [unclosed\n"))


def test_config_rejects_non_list_document(tmp_path):
    with pytest.raises(FacebookGroupsConfigError, match="must contain a list"):
        load_groups(write_config(tmp_path, "url: x\n"))


def test_config_rejects_empty_list(tmp_path):
    with pytest.raises(FacebookGroupsConfigError, match="lists no groups"):
        load_groups(write_config(tmp_path, "[]\n"))


@pytest.mark.parametrize(
    "entry, message",
    [
        ({**valid_entry(), "extra": 1}, "unknown key"),
        ({"url": "https://www.facebook.com/groups/x"}, "missing required key"),
        (valid_entry(tag="cokolwiek"), "tag must be one of"),
        (valid_entry(view="LATEST"), "view must be one of"),
        (valid_entry(lang="de"), "lang must be one of"),
        (valid_entry(public=False), "cannot be enabled"),
        (valid_entry(max_posts=0), "max_posts must be a positive integer"),
        (valid_entry(max_posts=True), "max_posts must be a positive integer"),
        (valid_entry(enabled="yes"), "enabled must be true or false"),
        (valid_entry(name="  "), "name must be a non-empty string"),
        (valid_entry(url="https://example.com/groups/abc"), "must be a Facebook group URL"),
        (valid_entry(url="https://www.facebook.com/groups"), "must be a Facebook group URL"),
        (valid_entry(url="https://www.facebook.com/groups/abc/about"), "must point at one group"),
        ("not-a-mapping", "must be a mapping"),
    ],
)
def test_config_rejects_bad_entries(tmp_path, entry, message):
    import yaml

    path = write_config(tmp_path, yaml.safe_dump([entry], allow_unicode=True))
    with pytest.raises(FacebookGroupsConfigError, match=message):
        load_groups(path)


def test_config_rejects_duplicate_groups(tmp_path):
    import yaml

    path = write_config(
        tmp_path,
        yaml.safe_dump(
            [
                valid_entry(),
                valid_entry(url="https://facebook.com/groups/dlaprzedsiebiorcow/"),
            ],
            allow_unicode=True,
        ),
    )
    with pytest.raises(FacebookGroupsConfigError, match="duplicate group"):
        load_groups(path)


# --------------------------------------------------------------------------- #
# Mapping: pilot payload -> JobLead
# --------------------------------------------------------------------------- #


def test_real_pilot_posts_map_to_job_leads():
    leads = build(BLACKFALCON).collect()
    assert len(leads) == BLACKFALCON_LEADS
    assert {lead.source for lead in leads} == {FACEBOOK_GROUPS_SOURCE}
    assert all(lead.url == lead.canonical_url for lead in leads)
    assert all(lead.description.strip() for lead in leads)
    assert all(lead.source_id for lead in leads)
    assert all(lead.company_or_author for lead in leads)
    assert all(lead.language == "pl" for lead in leads)
    # Permalinks are unique, which is what makes cross-source dedup work.
    assert len({lead.canonical_url for lead in leads}) == len(leads)
    # Both groups of this payload are enabled in the fixture config.
    groups = {lead.raw_data["group_name"] for lead in leads}
    assert groups == {"Grupa dla przedsiębiorców", "Social Media - ZLECĘ / WYKONAM"}


def test_mapping_field_by_field_on_a_real_post():
    leads = build(APIFY).collect()
    lead = next(lead for lead in leads if lead.source_id == "2626755054462316")

    assert lead.url == "https://www.facebook.com/groups/417702252034285/permalink/2626755054462316/"
    assert lead.canonical_url == lead.url
    assert lead.description.startswith("Hej, poszukuję specjalisty który pomoże mi")
    assert lead.title == (
        "Social Media - ZLECĘ / WYKONAM: Hej, poszukuję specjalisty który pomoże mi "
        "z ustawieniami kont w Meta Business Suite."
    )
    assert lead.company_or_author == "Radek Chrobok"
    assert lead.published_at == datetime(2026, 9, 21, 12, 33, 9, tzinfo=UTC)
    assert lead.language == "pl"
    assert lead.budget is None and lead.currency is None  # groups carry no budget
    assert lead.raw_data["group_url"] == "https://www.facebook.com/groups/417702252034285"
    assert lead.raw_data["group_tag"] == "freelancing"
    assert lead.raw_data["group_view"] == "CHRONOLOGICAL"
    assert lead.raw_data["permalink"] == lead.url
    assert lead.raw_data["author_name"] == "Radek Chrobok"
    assert lead.raw_data["posted_at"] == "2026-09-21T12:33:09.000Z"
    assert lead.raw_data["reactions"] == 0
    assert lead.raw_data["comments"] == 1
    assert lead.raw_data["shares"] == 0
    assert lead.raw_data["media"] == 0


def test_published_at_is_real_when_the_actor_provides_it():
    """A real timestamp is what switches the age rule on for this source."""
    leads = build(BLACKFALCON).collect()
    with_time = [lead for lead in leads if lead.published_at is not None]
    without_time = [lead for lead in leads if lead.published_at is None]
    # blackfalcondata returns time for dlaprzedsiebiorcow but not for 4177.
    assert with_time and without_time
    assert all(lead.published_at.tzinfo is not None for lead in with_time)
    assert all(
        lead.published_at.year == 2026 and lead.published_at.month == 9 for lead in with_time
    )


def test_posts_without_text_are_dropped_and_counted():
    adapter = build(BLACKFALCON)
    leads = adapter.collect()
    dlaprzedsiebiorcow = next(
        item for item in adapter.last_group_report if item.name == "Grupa dla przedsiębiorców"
    )
    assert dlaprzedsiebiorcow.status == "ok"
    assert dlaprzedsiebiorcow.posts == 6  # payload subset, incl. 2 photo-only posts
    assert dlaprzedsiebiorcow.posts_with_text == 4
    assert dlaprzedsiebiorcow.leads == 4
    assert len(leads) == BLACKFALCON_LEADS
    # No lead can carry a photo-only post: text is the only judgement input.
    assert all(lead.title.split(": ", 1)[1] for lead in leads)


def test_group_language_and_tag_come_from_config():
    leads = build(PROGRAMISCI).collect()
    assert len(leads) == PROGRAMISCI_LEADS
    assert {lead.raw_data["group_tag"] for lead in leads} == {"freelancing"}
    assert {lead.language for lead in leads} == {"pl"}


def test_disabled_group_is_skipped_and_reported_not_mapped():
    adapter = build(BLACKFALCON)
    leads = adapter.collect()
    skipped = [item for item in adapter.last_group_report if item.status == "skipped"]
    assert [item.name for item in skipped] == [
        "Social Media, marketing - ZLECĘ/WYKONAM, praca w marketingu"
    ]
    assert skipped[0].note == "wyłączona w configu"
    assert all(
        lead.raw_data["group_url"] != "https://www.facebook.com/groups/socialmediapracazlecenia"
        for lead in leads
    )


def test_unknown_group_in_payload_is_skipped(tmp_path):
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/nieznana-grupa",
                "status": "SUCCEEDED",
                "posts": [post()],
            },
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [post()],
            },
        ]
    )
    adapter = FacebookGroups(
        posts_path=write_payload(tmp_path / "posts-2026-09-21.json", data),
        groups_config_path=CONFIG,
        check_freshness=False,
    )
    leads = adapter.collect()
    assert len(leads) == 1
    skipped = [item for item in adapter.last_group_report if item.status == "skipped"]
    assert skipped[0].status == "skipped"
    assert skipped[0].note == "grupa spoza configu"


def test_duplicate_permalinks_in_one_payload_collapse_to_one_lead(tmp_path):
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [post(), post()],
            }
        ]
    )
    leads = FacebookGroups(
        posts_path=write_payload(tmp_path / "posts-2026-09-21.json", data),
        groups_config_path=CONFIG,
        check_freshness=False,
    ).collect()
    assert len(leads) == 1


def test_title_strips_markup_and_truncates_long_first_sentences(tmp_path):
    long_sentence = "## ** Poszukuję wykonawcy do integracji sklepu z systemem faktur " + (
        "oraz kolejnego etapu " * 12
    )
    text = long_sentence + "szczegóły poniżej."
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [
                    post(
                        text=text,
                        permalink="https://www.facebook.com/groups/dlaprzedsiebiorcow/permalink/1234/",
                        post_id="1234",
                    )
                ],
            }
        ]
    )
    lead = FacebookGroups(
        posts_path=write_payload(tmp_path / "posts-2026-09-21.json", data),
        groups_config_path=CONFIG,
        check_freshness=False,
    ).collect()[0]
    assert lead.title.startswith("Grupa dla przedsiębiorców: Poszukuję wykonawcy")
    assert "**" not in lead.title and "##" not in lead.title
    assert lead.title.endswith("…")
    assert len(lead.title) <= len("Grupa dla przedsiębiorców: ") + 140


def test_title_keeps_only_the_first_sentence(tmp_path):
    post_text = "Szukam osoby do automatyzacji faktur. Drugie zdanie nie wchodzi do tytułu."
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [post(text=post_text, post_id="2000")],
            }
        ]
    )
    lead = FacebookGroups(
        posts_path=write_payload(tmp_path / "posts-2026-09-21.json", data),
        groups_config_path=CONFIG,
        check_freshness=False,
    ).collect()[0]
    assert lead.title == "Grupa dla przedsiębiorców: Szukam osoby do automatyzacji faktur."
    assert lead.description == post_text  # the full text still reaches the scorer


def test_photo_only_post_cannot_become_a_lead(tmp_path):
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [post(text="   "), post(text=None)],
            }
        ]
    )
    with pytest.raises(FacebookGroupsError, match="0 leads"):
        FacebookGroups(
            posts_path=write_payload(tmp_path / "posts-2026-09-21.json", data),
            groups_config_path=CONFIG,
            check_freshness=False,
        ).collect()


def test_post_without_permalink_is_dropped(tmp_path):
    broken = post()
    broken.pop("url")
    broken.pop("portalUrl")
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [broken, post(post_id="1000")],
            }
        ]
    )
    leads = FacebookGroups(
        posts_path=write_payload(tmp_path / "posts-2026-09-21.json", data),
        groups_config_path=CONFIG,
        check_freshness=False,
    ).collect()
    assert [lead.source_id for lead in leads] == ["1000"]


# --------------------------------------------------------------------------- #
# Loud failure instead of a silent zero
# --------------------------------------------------------------------------- #


def test_no_payload_file_raises_with_a_visible_reason(tmp_path):
    adapter = FacebookGroups(
        cache_dir=tmp_path, groups_config_path=CONFIG, check_freshness=False
    )
    with pytest.raises(FacebookGroupsError, match="transport did not run"):
        adapter.collect()


def test_explicit_missing_posts_path_raises(tmp_path):
    adapter = FacebookGroups(
        posts_path=tmp_path / "posts-2026-09-21.json",
        groups_config_path=CONFIG,
        check_freshness=False,
    )
    with pytest.raises(FacebookGroupsError, match="does not exist"):
        adapter.collect()


def test_all_groups_blocked_raises_with_per_group_status():
    adapter = build(TRANSPORT_BLOCKED)
    with pytest.raises(FacebookGroupsError) as excinfo:
        adapter.collect()
    message = str(excinfo.value)
    assert "no data from source" in message
    assert "BLOCKED" in message
    assert "Grupa dla przedsiębiorców" in message
    assert "4 leadów" not in message  # nothing was mapped


def test_payload_without_groups_list_raises(tmp_path):
    path = write_payload(tmp_path / "posts-x.json", {"collected_at": APIFY_AT.isoformat()})
    adapter = FacebookGroups(
        posts_path=path, groups_config_path=CONFIG, check_freshness=False
    )
    with pytest.raises(FacebookGroupsError, match="no 'groups' list"):
        adapter.collect()


def test_payload_that_is_not_json_raises(tmp_path):
    path = tmp_path / "posts-x.json"
    path.write_text("<html>login wall</html>", encoding="utf-8")
    adapter = FacebookGroups(
        posts_path=path, groups_config_path=CONFIG, check_freshness=False
    )
    with pytest.raises(FacebookGroupsError, match="not valid JSON"):
        adapter.collect()


def test_no_enabled_group_raises(tmp_path):
    import yaml

    config = write_config(
        tmp_path, yaml.safe_dump([valid_entry(enabled=False, public=True)], allow_unicode=True)
    )
    adapter = FacebookGroups(
        posts_path=FIXTURES / BLACKFALCON, groups_config_path=config, check_freshness=False
    )
    with pytest.raises(FacebookGroupsError, match="no group is enabled"):
        adapter.collect()


def test_stale_payload_is_rejected(tmp_path):
    fresh = build(APIFY, check_freshness=True, now=lambda: APIFY_AT + timedelta(hours=1))
    assert len(fresh.collect()) == APIFY_LEADS

    stale = build(APIFY, check_freshness=True, now=lambda: APIFY_AT + timedelta(hours=30))
    with pytest.raises(FacebookGroupsError, match="did not run"):
        stale.collect()


def test_freshness_uses_file_mtime_when_payload_has_no_timestamp(tmp_path):
    data = payload(
        [
            {
                "url": "https://www.facebook.com/groups/dlaprzedsiebiorcow",
                "status": "SUCCEEDED",
                "posts": [post()],
            }
        ],
        collected_at=None,
    )
    path = write_payload(tmp_path / "posts-2026-09-21.json", data)
    os.utime(path, (APIFY_AT.timestamp(), APIFY_AT.timestamp()))
    adapter = FacebookGroups(
        posts_path=path,
        groups_config_path=CONFIG,
        now=lambda: APIFY_AT + timedelta(hours=20),
    )
    assert len(adapter.collect()) == 1


def test_newest_posts_file_wins(tmp_path):
    write_payload(tmp_path / "posts-2026-09-20.json", {"groups": "not-a-list"})
    newest = write_payload(
        tmp_path / "posts-2026-09-21.json",
        json.loads((FIXTURES / BLACKFALCON).read_text(encoding="utf-8")),
    )
    old = tmp_path / "posts-2026-09-20.json"
    os.utime(old, (APIFY_AT.timestamp() - 86400, APIFY_AT.timestamp() - 86400))
    os.utime(newest, (APIFY_AT.timestamp(), APIFY_AT.timestamp()))

    adapter = FacebookGroups(
        cache_dir=tmp_path,
        posts_path=None,
        groups_config_path=CONFIG,
        check_freshness=False,
    )
    assert len(adapter.collect()) == BLACKFALCON_LEADS


# --------------------------------------------------------------------------- #
# Account safety: the adapter is a file reader, never a Facebook client
# --------------------------------------------------------------------------- #


def test_collect_makes_no_network_call(monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - only fires on a regression
        raise AssertionError("facebook_groups adapter must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    assert len(build(BLACKFALCON).collect()) == BLACKFALCON_LEADS


def test_adapter_has_no_http_client():
    adapter = build(BLACKFALCON)
    assert not hasattr(adapter, "_http")
    assert not hasattr(adapter, "session")


def test_status_lines_report_every_group_of_the_payload():
    adapter = build(APIFY)
    adapter.collect()
    lines = adapter.status_lines()
    assert len(lines) == 3  # przygody (skipped), dlaprzedsiebiorcow, 4177
    assert any("Grupa dla przedsiębiorców" in line and "ok" in line for line in lines)
    assert any("Przygody Przedsiębiorców" in line and "skipped" in line for line in lines)


# --------------------------------------------------------------------------- #
# Registry: registered for diagnostics, outside the active production set
# --------------------------------------------------------------------------- #


def test_facebook_adapter_is_registered_and_active():
    """FB wszedł do ACTIVE_SOURCES 2026-09-21: Jan chce leady na Telegramie."""
    assert DEFAULT_ADAPTERS["facebook_groups"] is FacebookGroups
    assert FACEBOOK_GROUPS_SOURCE in ACTIVE_SOURCES
    assert FACEBOOK_GROUPS_SOURCE not in INACTIVE_SOURCES


def test_build_adapter_facebook_groups_builds_through_the_registry():
    # check_freshness=False: ten test sprawdza okablowanie rejestru, nie świeżość pliku.
    # Bez tego był bombą zegarową — fixture blackfalcondata ma wpisane `collected_at`
    # z 21.09, więc po ~26 h test sam z siebie robił się czerwony (złapane 2026-09-23).
    # Świeżość mają pokryte dedykowane testy niżej, na zamrożonym `now`.
    adapter = build_adapter(
        "facebook_groups",
        posts_path=FIXTURES / BLACKFALCON,
        groups_config_path=CONFIG,
        check_freshness=False,
    )
    assert isinstance(adapter, FacebookGroups)
    assert len(adapter.collect()) == BLACKFALCON_LEADS


def test_offline_mode_covers_the_facebook_adapter():
    from freelance_pipeline.offline import build_offline_adapters

    adapters = build_offline_adapters(FIXTURES)
    assert set(adapters) == set(DEFAULT_ADAPTERS)
    facebook = adapters["facebook_groups"]
    assert isinstance(facebook, FacebookGroups)
    assert len(facebook.collect()) == BLACKFALCON_LEADS
