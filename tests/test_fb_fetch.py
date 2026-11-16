"""Stage 2b tests: the FB transport that writes posts-<data>.json for the adapter.

Everything here runs offline against a fake Apify transport — no test starts a real
actor run, because a real run costs money (stage 1 spent $1.19). What is pinned:

* selection: only ``enabled: true`` groups reach the actor input, the limit is the
  sum of their ``max_posts`` (capped), and a mixed ``view`` config is reported, not
  silently overridden;
* actor order: the official ``apify/facebook-groups-scraper`` is primary and runs
  alone by default (one payment); the blackfalcondata fallback is opt-in via
  ``--fallback``, because its dataset mixes posts with comments (measured 2026-09-21);
* the file contract: what the script writes is what
  ``freelance_pipeline.sources.facebook.FacebookGroups`` reads (integration test on
  the produced file, not on a fixture);
* visibility: per-group statuses (ok/empty/diagnostic), a run with zero text posts
  exits 3 **and** still writes the statuses, a dead transport exits 2 and writes
  nothing, and the token never appears on stdout/stderr.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_pipeline.sources.facebook import (
    FacebookGroups,
    FacebookGroupsError,
    load_groups,
)

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "fb_fetch.py"
WRAPPER = REPO / "scripts" / "fb_fetch.sh"
CRON_SHIM = Path("/opt/data/scripts/snajper-fb-fetch.sh")
FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = FIXTURES / "facebook_groups.yaml"

# Enabled in the fixture config; the payloads below must use these URLs.
PROGRAMISCI = "https://www.facebook.com/groups/programiscipoland"
DLAPRZED = "https://www.facebook.com/groups/dlaprzedsiebiorcow"
SOCIAL_4177 = "https://www.facebook.com/groups/417702252034285"

NOW = datetime(2026, 9, 22, 4, 50, tzinfo=UTC)
TOKEN = "apify_api_test_token_do_not_print"


def _load_script():
    spec = importlib.util.spec_from_file_location("fb_fetch_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fb_fetch = _load_script()


def post(group_url: str, *, pid: str, text: str | None = "Szukam osoby do wdrożenia n8n.", **extra):
    """One blackfalcondata-shaped item (field names come from the stage-1 pilot)."""
    item = {
        "facebookUrl": group_url,
        "inputUrl": group_url,
        "groupTitle": "Grupa",
        "url": f"{group_url}/permalink/{pid}/",
        "portalUrl": f"{group_url}/permalink/{pid}/",
        "legacyId": pid,
        "id": pid,
        "text": text,
        "time": "2026-09-21T12:00:00.000Z",
        "user": {"id": None, "name": "Autor Posta"},
        "likesCount": None,
        "topReactionsCount": None,
        "commentsCount": None,
        "sharesCount": None,
        "attachments": [],
    }
    item.update(extra)
    return item


def apify_diagnostic(group_url: str, *, error: str = "no_items"):
    """The apify actor's diagnostic item for a private/empty group."""
    return {
        "url": group_url,
        "error": error,
        "errorDescription": "Empty or private data for provided input",
    }


class FakeTransport:
    """Stand-in for :class:`fb_fetch.ApifyClient`; records what was asked of it."""

    def __init__(self, outcomes: dict[str, object], *, credit_left: float | None = 5.0) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict]] = []
        self.credit_left = credit_left

    def call(self, method: str, path: str, payload=None):
        """Only the endpoint the credit guard reads; ``None`` means "unreadable"."""
        if path != "/users/me/limits":
            raise AssertionError(f"nieoczekiwane wywołanie API: {method} {path}")
        if self.credit_left is None:
            raise fb_fetch.FetchError("limits endpoint unavailable")
        return {
            "data": {
                "limits": {"maxMonthlyUsageUsd": 5.0},
                "current": {"monthlyUsageUsd": 5.0 - self.credit_left},
            }
        }

    def run_actor(self, spec, actor_input, *, poll_every=0, poll_max=0):
        self.calls.append((spec.name, dict(actor_input)))
        outcome = self.outcomes.get(spec.name)
        if outcome is None:
            raise fb_fetch.FetchError(f"brak zdefiniowanego runu dla {spec.name}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def run_of(actor: str, items, *, status: str = "SUCCEEDED", cost: float = 0.16):
    spec = fb_fetch.ActorSpec(name=actor)
    return fb_fetch.ActorRun(
        actor=actor,
        slug=spec.slug,
        run_id="run123",
        status=status,
        cost_usd=cost,
        items=list(items),
    )


def write_env(tmp_path: Path, *, token: str | None = TOKEN) -> Path:
    env_file = tmp_path / ".env"
    body = "OPENROUTER_API_KEY=unused\n"
    if token is not None:
        body += f'{fb_fetch.TOKEN_ENV_NAME}="{token}"\n'
    env_file.write_text(body, encoding="utf-8")
    return env_file


def run_main(tmp_path: Path, transport, argv=(), *, now=NOW):
    argv = [
        "--groups-config",
        str(CONFIG),
        "--posts-dir",
        str(tmp_path / "cache"),
        "--env-file",
        str(write_env(tmp_path)),
        *argv,
    ]
    return fb_fetch.main(list(argv), transport=transport, now=lambda: now)


def posts_file(tmp_path: Path) -> Path:
    return tmp_path / "cache" / f"posts-{NOW.strftime('%Y%m%d')}.json"


# ------------------------------------------------------------------ actor input


def test_only_enabled_groups_are_requested():
    groups = load_groups(CONFIG)
    enabled = tuple(group for group in groups if group.enabled)
    run_input = fb_fetch.build_actor_input(enabled)

    urls = [entry["url"] for entry in run_input.payload["startUrls"]]
    assert urls == [PROGRAMISCI, DLAPRZED, SOCIAL_4177]
    # socialmediapracazlecenia is public but disabled in the fixture config; the
    # private group is disabled too. Neither may reach a paid run.
    assert all("socialmediapracazlecenia" not in url for url in urls)
    assert all("przygodyprzedsiebiorcow" not in url for url in urls)


def test_results_limit_is_the_sum_of_max_posts_and_window_comes_from_cli():
    groups = tuple(g for g in load_groups(CONFIG) if g.enabled)
    run_input = fb_fetch.build_actor_input(groups, newer_than="3 days")
    assert run_input.payload["resultsLimit"] == sum(g.max_posts for g in groups)
    assert run_input.payload["onlyPostsNewerThan"] == "3 days"
    assert run_input.payload["viewOption"] == "CHRONOLOGICAL"
    assert run_input.note is None


def test_results_limit_is_capped_so_a_config_typo_cannot_run_up_a_bill():
    groups = tuple(g for g in load_groups(CONFIG) if g.enabled)
    fat = tuple(
        fb_fetch.FacebookGroup(
            url=g.url,
            name=g.name,
            tag=g.tag,
            lang=g.lang,
            public=True,
            enabled=True,
            max_posts=5000,
            view=g.view,
        )
        for g in groups
    )
    assert fb_fetch.build_actor_input(fat).payload["resultsLimit"] == fb_fetch.MAX_RESULTS_LIMIT


def test_mixed_views_are_reported_not_silently_overridden():
    groups = load_groups(CONFIG)
    enabled = [g for g in groups if g.enabled]
    mixed = [replace(enabled[0], view="TOP_POSTS"), *enabled[1:]]
    run_input = fb_fetch.build_actor_input(mixed)
    assert run_input.payload["viewOption"] == fb_fetch.DEFAULT_VIEW
    assert run_input.note and "TOP_POSTS" in run_input.note


# -------------------------------------------------------------- actor chasing


def test_primary_actor_wins_and_fallback_is_never_paid_for(tmp_path: Path):
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="1")]),
        }
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    assert [name for name, _ in transport.calls] == [fb_fetch.PRIMARY_ACTOR]


def test_fallback_actor_is_opt_in_with_a_flag(tmp_path: Path):
    """Domyślnie przebieg to jeden aktor: drugi dataset miesza komentarze i kosztuje."""
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: fb_fetch.FetchError("HTTP 500: actor unavailable"),
            fb_fetch.FALLBACK_ACTOR: run_of(fb_fetch.FALLBACK_ACTOR, [post(PROGRAMISCI, pid="2")]),
        }
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_NO_DATA
    assert [name for name, _ in transport.calls] == [fb_fetch.PRIMARY_ACTOR]


def test_fallback_actor_runs_when_primary_fails_and_flag_is_given(tmp_path: Path):
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: fb_fetch.FetchError("HTTP 500: actor unavailable"),
            fb_fetch.FALLBACK_ACTOR: run_of(fb_fetch.FALLBACK_ACTOR, [post(PROGRAMISCI, pid="2")]),
        }
    )
    assert run_main(tmp_path, transport, ["--fallback"]) == fb_fetch.EXIT_OK
    assert [name for name, _ in transport.calls] == [fb_fetch.PRIMARY_ACTOR, fb_fetch.FALLBACK_ACTOR]
    payload = json.loads(posts_file(tmp_path).read_text(encoding="utf-8"))
    assert payload["actor"] == fb_fetch.FALLBACK_ACTOR
    assert [attempt["status"] for attempt in payload["attempts"]] == ["ERROR", "SUCCEEDED"]


def test_fallback_actor_runs_when_primary_returns_no_text_posts(tmp_path: Path):
    """Stage-1 risk „actor pada po zmianie markupu": items come back, text does not."""
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(
                fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="3", text=None)]
            ),
            fb_fetch.FALLBACK_ACTOR: run_of(fb_fetch.FALLBACK_ACTOR, [post(PROGRAMISCI, pid="4")]),
        }
    )
    assert run_main(tmp_path, transport, ["--fallback"]) == fb_fetch.EXIT_OK
    assert [name for name, _ in transport.calls] == [fb_fetch.PRIMARY_ACTOR, fb_fetch.FALLBACK_ACTOR]


def test_default_run_is_one_actor_and_one_payment(tmp_path: Path):
    """Kontrakt kosztowy: bez flagi nie płacimy za drugi dataset."""
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="6")]),
            fb_fetch.FALLBACK_ACTOR: run_of(fb_fetch.FALLBACK_ACTOR, [post(PROGRAMISCI, pid="7")]),
        }
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    assert [name for name, _ in transport.calls] == [fb_fetch.PRIMARY_ACTOR]


def test_primary_actor_is_the_official_one_that_returns_posts_only():
    """Regresja na decyzję z 2026-09-21: blackfalcondata miesza komentarze do datasetu."""
    assert fb_fetch.PRIMARY_ACTOR == "apify/facebook-groups-scraper"
    assert fb_fetch.FALLBACK_ACTOR != fb_fetch.PRIMARY_ACTOR


def test_actor_override_is_honoured(tmp_path: Path):
    transport = FakeTransport({"someone/custom-scraper": run_of("someone/custom-scraper", [post(PROGRAMISCI, pid="5")])})
    assert run_main(tmp_path, transport, ["--actor", "someone/custom-scraper"]) == fb_fetch.EXIT_OK
    assert [name for name, _ in transport.calls] == ["someone/custom-scraper"]


# ------------------------------------------------------------- file contract


def test_written_file_is_read_by_the_facebook_adapter(tmp_path: Path):
    """The produced payload — not a fixture — has to feed the stage-2 adapter."""
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(
                fb_fetch.PRIMARY_ACTOR,
                [
                    post(PROGRAMISCI, pid="10", text="Szukam freelancera do integracji API."),
                    post(DLAPRZED, pid="11", text="Potrzebuję pomocy z automatyzacją faktur."),
                    post(DLAPRZED, pid="12", text=None),  # photo-only: dropped by the adapter
                ],
            )
        }
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK

    adapter = FacebookGroups(
        posts_path=posts_file(tmp_path), groups_config_path=CONFIG, check_freshness=False
    )
    leads = adapter.collect()
    assert len(leads) == 2
    assert {lead.canonical_url for lead in leads} == {
        f"{PROGRAMISCI}/permalink/10/",
        f"{DLAPRZED}/permalink/11/",
    }
    first = next(lead for lead in leads if lead.source_id == "10")
    assert first.source == "facebook_groups"
    assert first.published_at == datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert first.company_or_author == "Autor Posta"
    assert first.title.startswith("Programiści Polska")
    lines = adapter.status_lines()
    assert len(lines) == 3
    assert {line.split(" (")[0] for line in lines} == {
        "Programiści Polska / Developers Poland",
        "Grupa dla przedsiębiorców",
        "Social Media - ZLECĘ / WYKONAM",
    }


def test_every_group_gets_its_own_status(tmp_path: Path):
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(
                fb_fetch.PRIMARY_ACTOR,
                [post(PROGRAMISCI, pid="20"), apify_diagnostic(SOCIAL_4177)],
            )
        }
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    payload = json.loads(posts_file(tmp_path).read_text(encoding="utf-8"))
    statuses = {entry["url"]: entry["status"] for entry in payload["groups"]}
    assert statuses == {PROGRAMISCI: "ok", DLAPRZED: "empty", SOCIAL_4177: "diagnostic"}
    diagnostic = next(e for e in payload["groups"] if e["url"] == SOCIAL_4177)
    assert diagnostic["diagnostic"] == "no_items"


def test_items_from_a_group_outside_the_config_are_kept_and_flagged(tmp_path: Path):
    stranger = "https://www.facebook.com/groups/nie-ma-mnie-w-configu"
    transport = FakeTransport(
        {fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(stranger, pid="30")])}
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    payload = json.loads(posts_file(tmp_path).read_text(encoding="utf-8"))
    extra = [entry for entry in payload["groups"] if entry["url"] == stranger]
    assert len(extra) == 1
    assert extra[0]["status"] == "unassigned"
    # The adapter reports such a group as „grupa spoza configu" instead of dropping it.
    adapter = FacebookGroups(
        posts_path=posts_file(tmp_path), groups_config_path=CONFIG, check_freshness=False
    )
    with pytest.raises(FacebookGroupsError) as excinfo:  # nothing mappable in the enabled groups
        adapter.collect()
    assert "grupa spoza configu" in str(excinfo.value)


def test_file_name_and_payload_metadata(tmp_path: Path):
    transport = FakeTransport(
        {fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="40")])}
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    assert posts_file(tmp_path).is_file()
    payload = json.loads(posts_file(tmp_path).read_text(encoding="utf-8"))
    assert payload["collected_at"] == NOW.isoformat()
    assert payload["actor"] == fb_fetch.PRIMARY_ACTOR
    assert payload["groups_requested"] == [PROGRAMISCI, DLAPRZED, SOCIAL_4177]
    assert payload["input"]["onlyPostsNewerThan"] == fb_fetch.DEFAULT_NEWER_THAN
    assert payload["cost_usd"] == pytest.approx(0.16)
    # Raw items are stored verbatim: the actor's own field names, nothing rewritten.
    assert payload["groups"][0]["posts"][0]["legacyId"] == "40"


def test_payload_is_written_atomically(tmp_path: Path):
    transport = FakeTransport(
        {fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="41")])}
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    leftovers = list((tmp_path / "cache").glob("*.tmp")) + list((tmp_path / "cache").glob(".*.tmp"))
    assert leftovers == []


# ----------------------------------------------------------- visible failures


def test_dead_transport_is_a_loud_failure_without_a_file(tmp_path: Path, capsys):
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: fb_fetch.FetchError("HTTP 500: boom"),
            fb_fetch.FALLBACK_ACTOR: fb_fetch.FetchError("HTTP 503: boom"),
        }
    )
    rc = run_main(tmp_path, transport)
    assert rc == fb_fetch.EXIT_NO_DATA
    assert not posts_file(tmp_path).exists()
    out, err = capsys.readouterr()
    # stdout is what the no_agent cron delivers, so the failure has to be there.
    assert "brak danych z transportu Apify" in out
    assert "brak danych z transportu Apify" in err


def test_zero_text_posts_exits_three_and_still_writes_the_statuses(tmp_path: Path, capsys):
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(
                fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="50", text=None)]
            ),
            fb_fetch.FALLBACK_ACTOR: run_of(
                fb_fetch.FALLBACK_ACTOR, [post(PROGRAMISCI, pid="51", text=None)]
            ),
        }
    )
    rc = run_main(tmp_path, transport)
    assert rc == fb_fetch.EXIT_NO_POSTS
    assert posts_file(tmp_path).is_file()
    out, _ = capsys.readouterr()
    assert "bez ani jednego postu z tekstem" in out
    assert "Programiści Polska" in out


def test_no_enabled_group_is_a_config_error(tmp_path: Path, capsys):
    config = tmp_path / "groups.yaml"
    config.write_text(
        "- url: https://www.facebook.com/groups/x\n"
        '  name: "X"\n'
        "  tag: freelancing\n"
        "  lang: pl\n"
        "  public: true\n"
        "  enabled: false\n"
        "  max_posts: 30\n"
        "  view: CHRONOLOGICAL\n",
        encoding="utf-8",
    )
    transport = FakeTransport({})
    rc = fb_fetch.main(
        ["--groups-config", str(config), "--posts-dir", str(tmp_path / "cache")],
        transport=transport,
        now=lambda: NOW,
    )
    assert rc == fb_fetch.EXIT_NO_DATA
    assert transport.calls == []  # a paid run is never started on an empty plan
    assert "enabled: true" in capsys.readouterr().out


def test_broken_group_config_is_a_loud_failure(tmp_path: Path, capsys):
    config = tmp_path / "groups.yaml"
    config.write_text("- url: https://example.com/groups/x\n", encoding="utf-8")
    rc = fb_fetch.main(
        ["--groups-config", str(config), "--posts-dir", str(tmp_path / "cache")],
        transport=FakeTransport({}),
    )
    assert rc == fb_fetch.EXIT_NO_DATA
    assert "konfiguracja grup nie przeszła" in capsys.readouterr().out


def test_missing_token_is_visible_and_never_starts_a_run(tmp_path: Path, capsys):
    env_file = write_env(tmp_path, token=None)
    rc = fb_fetch.main(
        [
            "--groups-config",
            str(CONFIG),
            "--posts-dir",
            str(tmp_path / "cache"),
            "--env-file",
            str(env_file),
        ],
        transport=FakeTransport({}),
        now=lambda: NOW,
    )
    assert rc == fb_fetch.EXIT_NO_DATA
    assert fb_fetch.TOKEN_ENV_NAME in capsys.readouterr().out


def test_token_never_reaches_stdout_or_stderr(tmp_path: Path, capsys):
    transport = FakeTransport(
        {
            fb_fetch.PRIMARY_ACTOR: run_of(
                fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="60")], cost=0.42
            )
        }
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    out, err = capsys.readouterr()
    assert TOKEN not in out and TOKEN not in err
    assert TOKEN not in posts_file(tmp_path).read_text(encoding="utf-8")


def test_dry_run_needs_no_token_and_writes_nothing(tmp_path: Path, capsys):
    posts_dir = tmp_path / "cache"
    rc = fb_fetch.main(
        [
            "--groups-config",
            str(CONFIG),
            "--posts-dir",
            str(posts_dir),
            "--env-file",
            str(write_env(tmp_path, token=None)),
            "--dry-run",
        ],
        transport=FakeTransport({}),
        now=lambda: NOW,
    )
    assert rc == fb_fetch.EXIT_OK
    assert not posts_dir.exists()
    out = capsys.readouterr().out
    assert f"aktorzy: {fb_fetch.PRIMARY_ACTOR}" in out
    assert CONFIG.as_posix() not in out  # no token, no secrets, just the plan


# ------------------------------------------------------------------ env + HTTP


def test_env_value_parsing_handles_quotes_and_comments(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# komentarz\n"
        "\n"
        "OTHER=1\n"
        f'{fb_fetch.TOKEN_ENV_NAME}="quoted value"\n',
        encoding="utf-8",
    )
    assert fb_fetch.read_env_value(env_file, fb_fetch.TOKEN_ENV_NAME) == "quoted value"
    assert fb_fetch.read_env_value(env_file, "NIE_MA_MNIE") is None


def test_env_file_that_cannot_be_read_is_an_error(tmp_path: Path):
    with pytest.raises(fb_fetch.FetchError):
        fb_fetch.load_token(tmp_path / "nie-ma-takiego-pliku")


class StubResponse:
    def __init__(self, status_code: int, body) -> None:
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)


class StubSession:
    """Duck-typed ``requests.Session``; records every call."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers or {}, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_client_sends_the_token_in_a_header_not_in_the_url():
    session = StubSession(
        [
            StubResponse(
                200,
                {
                    "data": {
                        "id": "run1",
                        "defaultDatasetId": "ds1",
                        "status": "SUCCEEDED",
                        "usageTotalUsd": 0.16,
                    }
                },
            ),
            StubResponse(200, [post(PROGRAMISCI, pid="70")]),
        ]
    )
    client = fb_fetch.ApifyClient(TOKEN, session=session)
    run = client.run_actor(fb_fetch.ActorSpec(name=fb_fetch.PRIMARY_ACTOR), {"startUrls": []})

    assert run.run_id == "run1"
    assert run.posts_with_text == 1
    assert run.cost_usd == pytest.approx(0.16)
    assert session.calls[0]["url"] == (
        f"{fb_fetch.APIFY_API_BASE}/acts/"
        f"{fb_fetch.ActorSpec(name=fb_fetch.PRIMARY_ACTOR).slug}/runs"
    )
    assert session.calls[1]["url"].startswith(f"{fb_fetch.APIFY_API_BASE}/datasets/ds1/items")
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert all(TOKEN not in call["url"] for call in session.calls)


def test_client_polls_until_the_run_is_terminal():
    session = StubSession(
        [
            StubResponse(200, {"data": {"id": "run2", "status": "RUNNING", "defaultDatasetId": "ds2"}}),
            StubResponse(200, {"data": {"id": "run2", "status": "SUCCEEDED", "defaultDatasetId": "ds2"}}),
            StubResponse(200, []),
        ]
    )
    slept: list[float] = []
    client = fb_fetch.ApifyClient(TOKEN, session=session, sleep=slept.append)
    run = client.run_actor(
        fb_fetch.ActorSpec(name=fb_fetch.PRIMARY_ACTOR), {}, poll_every=7, poll_max=60
    )
    assert run.status == "SUCCEEDED"
    assert slept == [7.0]


def test_client_retries_a_server_error_then_succeeds():
    session = StubSession([StubResponse(500, "boom"), StubResponse(200, {"data": {}})])
    slept: list[float] = []
    client = fb_fetch.ApifyClient(TOKEN, session=session, sleep=slept.append)
    assert client.call("GET", "/actor-runs/run3") == {"data": {}}
    assert len(session.calls) == 2
    assert len(slept) == 1


def test_client_gives_up_on_a_client_error_without_retrying():
    session = StubSession([StubResponse(400, "bad input")])
    client = fb_fetch.ApifyClient(TOKEN, session=session)
    with pytest.raises(fb_fetch.FetchError) as excinfo:
        client.call("POST", "/acts/x/runs", {})
    assert "HTTP 400" in str(excinfo.value)
    assert len(session.calls) == 1


def test_client_strips_the_token_from_error_messages():
    session = StubSession([StubResponse(500, f"server echoed token {TOKEN}")])
    client = fb_fetch.ApifyClient(TOKEN, session=session, retries=1)
    with pytest.raises(fb_fetch.FetchError) as excinfo:
        client.call("GET", "/actor-runs/run4")
    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# ------------------------------------------------------------------- wrappers


def _fake_uv_env(tmp_path: Path) -> tuple[dict, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    capture = tmp_path / "capture.txt"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$CAPTURE"\n'
        'exit "${FAKE_UV_EXIT:-0}"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "SNAJPER_ENV_FILE": str(write_env(tmp_path)),
        "SNAJPER_FB_POSTS_DIR": str(tmp_path / "posts"),
        "SNAJPER_REPO": str(REPO),
    }
    return env, capture


def test_wrapper_passes_paths_to_the_fetch_script(tmp_path: Path):
    env, capture = _fake_uv_env(tmp_path)
    result = subprocess.run([str(WRAPPER)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[0] == "run"
    assert args[1] == "python"
    assert args[2] == "scripts/fb_fetch.py"
    assert args[3:4] == ["--env-file"]
    assert str(tmp_path / "posts") in args
    assert str(REPO / "config" / "facebook_groups.yaml") in args


def test_wrapper_propagates_the_exit_code_of_the_fetch_script(tmp_path: Path):
    env, _ = _fake_uv_env(tmp_path)
    env["FAKE_UV_EXIT"] = "3"
    result = subprocess.run([str(WRAPPER)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 3


def test_wrapper_refuses_to_run_without_the_env_file(tmp_path: Path):
    env, _ = _fake_uv_env(tmp_path)
    env["SNAJPER_ENV_FILE"] = str(tmp_path / "nie-ma.env")
    result = subprocess.run([str(WRAPPER)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 2
    assert "nie mogę przeczytać pliku środowiska" in result.stderr


@pytest.mark.skipif(not CRON_SHIM.is_file(), reason="shim lives outside the repo")
def test_cron_shim_delegates_to_the_repo_wrapper(tmp_path: Path):
    env, capture = _fake_uv_env(tmp_path)
    result = subprocess.run([str(CRON_SHIM)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert capture.read_text(encoding="utf-8").splitlines()[2] == "scripts/fb_fetch.py"


@pytest.mark.skipif(not CRON_SHIM.is_file(), reason="shim lives outside the repo")
def test_cron_shim_passes_its_arguments_through(tmp_path: Path):
    """Regression: without "$@" the shim dropped --dry-run into a *paid* real run."""
    env, capture = _fake_uv_env(tmp_path)
    result = subprocess.run(
        [str(CRON_SHIM), "--dry-run"], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert capture.read_text(encoding="utf-8").splitlines()[-1] == "--dry-run"

def test_run_is_skipped_when_apify_credit_is_gone(tmp_path: Path, capsys):
    """Zero kredytu = zero runów: start płaci się nawet wtedy, gdy aktor odmówi."""
    transport = FakeTransport(
        {fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="90")])},
        credit_left=0.02,
    )
    rc = run_main(tmp_path, transport)
    assert rc == fb_fetch.EXIT_NO_CREDIT
    assert transport.calls == []  # ani jednego płatnego startu
    assert not posts_file(tmp_path).exists()
    captured = capsys.readouterr()
    assert "kredytu Apify" in captured.err  # powód jest w logu
    assert captured.out == ""  # …ale cron no_agent nie dostaje nic do wysłania


def test_run_proceeds_when_credit_covers_the_estimate(tmp_path: Path):
    transport = FakeTransport(
        {fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="91")])},
        credit_left=5.0,
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK
    assert [name for name, _ in transport.calls] == [fb_fetch.PRIMARY_ACTOR]


def test_unreadable_limits_do_not_block_the_transport(tmp_path: Path):
    """Zepsuta diagnostyka nie może zatrzymać transportu — jedziemy i zgłaszamy brak danych."""
    transport = FakeTransport(
        {fb_fetch.PRIMARY_ACTOR: run_of(fb_fetch.PRIMARY_ACTOR, [post(PROGRAMISCI, pid="92")])},
        credit_left=None,
    )
    assert run_main(tmp_path, transport) == fb_fetch.EXIT_OK


def test_credit_estimate_scales_with_the_config():
    """Szacunek rośnie z liczbą postów w configu (limit = suma `max_posts`)."""
    first = next(g for g in load_groups(CONFIG) if g.enabled)
    groups = (replace(first, max_posts=10), replace(first, max_posts=20))
    run_input = fb_fetch.build_actor_input(groups)
    estimate = fb_fetch.run_cost_estimate(run_input)
    assert estimate == pytest.approx(
        30 * fb_fetch.ESTIMATED_COST_PER_POST_USD + fb_fetch.CREDIT_MARGIN_USD
    )
