from typing import Any

import pytest

from freelance_pipeline.registry import (
    ACTIVE_SOURCE_KWARGS,
    ACTIVE_SOURCES,
    DEFAULT_ADAPTERS,
    INACTIVE_SOURCES,
    AdapterRegistry,
    SourceConfig,
    build_active_adapters,
    build_adapter,
    default_registry,
)
from freelance_pipeline.sources.facebook import FacebookGroups
from freelance_pipeline.sources.justjoinit import JustJoinITJobs
from freelance_pipeline.sources.make import MakeHireHelp
from freelance_pipeline.sources.n8n import N8NCommunityJobs
from freelance_pipeline.sources.n8ndevs import N8NDevsJobs
from freelance_pipeline.sources.nofluffjobs import NoFluffJobsJobs
from freelance_pipeline.sources.oferteo import (
    OFERTEO_PRODUCTION_CATEGORY_PATHS,
    OferteoJobs,
)
from freelance_pipeline.sources.rocketjobs import RocketJobsJobs
from freelance_pipeline.sources.useme import PRODUCTION_CATEGORY_PATHS, UsemeJobs

# Production selection policy (HANDOFF, etap 1): real client work lives in the
# community/freelance sources; the job boards stay registered but inactive.
# Since 2026-10-07 (Jan's decision D1, PLAN-DOPRACOWANIE-2026-10 §2) the active
# set is Polish only: the three English community sources are off and Oferteo
# left the cron path (pay-per-lead points are Jan's manual decision).
# facebook_groups joined the ACTIVE set on 2026-09-21 after stage 3 (calibration):
# Jan wants the group leads on Telegram, not in a file he never opens; the
# per-source row cap that once limited them was removed on 2026-09-22.
EXPECTED_ACTIVE_SOURCES = (
    "useme",
    "facebook_groups",
)
# Everything else is registered but not collected in production. Oferteo stays
# reachable for a manual run through ``--sources oferteo`` (one flag, no code
# change), and the English/job-board adapters keep the one-line rollback.
EXPECTED_INACTIVE_SOURCES = (
    "justjoinit",
    "n8n_community_jobs",
    "make_hire_help",
    "nofluffjobs",
    "n8ndevs",
    "oferteo",
    "oferteo_mail",
    "rocketjobs",
)


def test_default_registry_has_all_sources():
    names = set(default_registry.names())
    assert {
        "facebook_groups",
        "justjoinit",
        "n8n_community_jobs",
        "make_hire_help",
        "nofluffjobs",
        "n8ndevs",
        "oferteo",
        "useme",
        "rocketjobs",
    } <= names


def test_active_sources_are_exactly_the_four_production_sources():
    assert ACTIVE_SOURCES == EXPECTED_ACTIVE_SOURCES
    assert len(set(ACTIVE_SOURCES)) == len(ACTIVE_SOURCES)


def test_job_boards_stay_registered_but_inactive():
    assert INACTIVE_SOURCES == EXPECTED_INACTIVE_SOURCES
    assert set(INACTIVE_SOURCES).isdisjoint(ACTIVE_SOURCES)
    assert set(ACTIVE_SOURCES) | set(INACTIVE_SOURCES) == set(DEFAULT_ADAPTERS)
    # One-line rollback: an inactive adapter is still registered and buildable.
    assert isinstance(build_adapter("justjoinit"), JustJoinITJobs)
    assert isinstance(build_adapter("rocketjobs"), RocketJobsJobs)
    assert isinstance(build_adapter("nofluffjobs"), NoFluffJobsJobs)
    assert isinstance(build_adapter("facebook_groups"), FacebookGroups)
    assert isinstance(build_adapter("oferteo"), OferteoJobs)


def test_build_active_adapters_builds_only_the_active_set():
    adapters = build_active_adapters()
    assert tuple(adapters) == ACTIVE_SOURCES
    assert isinstance(adapters["useme"], UsemeJobs)
    assert isinstance(adapters["facebook_groups"], FacebookGroups)
    for name in INACTIVE_SOURCES:
        assert name not in adapters


def test_dropped_english_sources_are_still_buildable():
    """Etap 1 (2026-10-07): wyłączenie to jedna linia, nie kasowanie kodu."""
    assert isinstance(build_adapter("n8n_community_jobs"), N8NCommunityJobs)
    assert isinstance(build_adapter("make_hire_help"), MakeHireHelp)
    assert isinstance(build_adapter("n8ndevs"), N8NDevsJobs)
    assert isinstance(build_adapter("oferteo"), OferteoJobs)


def test_build_instantiates_each_adapter():
    assert isinstance(build_adapter("n8n_community_jobs"), N8NCommunityJobs)
    assert isinstance(build_adapter("make_hire_help"), MakeHireHelp)
    assert isinstance(build_adapter("n8ndevs"), N8NDevsJobs)
    assert isinstance(build_adapter("useme"), UsemeJobs)
    assert isinstance(build_adapter("justjoinit"), JustJoinITJobs)
    assert isinstance(build_adapter("rocketjobs"), RocketJobsJobs)
    assert isinstance(build_adapter("nofluffjobs"), NoFluffJobsJobs)
    assert isinstance(build_adapter("facebook_groups"), FacebookGroups)


def test_sources_flag_builds_a_registered_inactive_adapter(tmp_path, monkeypatch):
    """Diagnostyczny przebieg jednego źródła nie musi włączać go do produkcji."""
    from freelance_pipeline import pipeline
    from freelance_pipeline.cli import main as cli_main

    captured: dict[str, Any] = {}

    class _Collection:
        results: tuple = ()
        failures: tuple = ()

    class _Result:
        collection = _Collection()
        candidates: tuple = ()
        scored: tuple = ()
        reported_count = 0
        zamien_count = 0
        committed = 0

    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kwargs: captured.update(kwargs) or _Result())

    rc = cli_main(
        [
            "run",
            "--state",
            str(tmp_path / "state.json"),
            "--report",
            "-",
            "--sources",
            "oferteo",
        ]
    )

    assert rc == 0
    assert tuple(captured["adapters"]) == ("oferteo",)
    adapter = captured["adapters"]["oferteo"]
    assert isinstance(adapter, OferteoJobs)
    assert adapter.category_paths == OFERTEO_PRODUCTION_CATEGORY_PATHS


def test_build_unknown_name_raises_key_error():
    with pytest.raises(KeyError):
        build_adapter("definitely-not-a-source")


def test_build_passes_config_kwargs():
    adapter = build_adapter("n8ndevs", base_url="https://example.com/jobs", timeout=3.0)
    assert adapter.base_url == "https://example.com/jobs"
    assert adapter.timeout == 3.0


def test_registry_build_all_instantiates_every_source():
    adapters = AdapterRegistry().build_all()
    assert set(adapters) == {
        "facebook_groups",
        "justjoinit",
        "n8n_community_jobs",
        "make_hire_help",
        "nofluffjobs",
        "n8ndevs",
        "oferteo",
        "oferteo_mail",
        "useme",
        "rocketjobs",
    }
    # A file-based adapter constructs like any other; it only reads on collect().
    assert isinstance(adapters["facebook_groups"], FacebookGroups)


def test_source_config_round_trips_to_kwargs():
    config = SourceConfig(name="n8ndevs", base_url="https://example.com/jobs", timeout=3.0)
    adapter = build_adapter(config.name, **config.kwargs)
    assert adapter.base_url == "https://example.com/jobs"
    assert adapter.timeout == 3.0


def test_cli_live_default_collects_only_active_sources(tmp_path, monkeypatch):
    """A production run without --sources must never touch the job boards."""
    from freelance_pipeline import pipeline
    from freelance_pipeline.cli import main as cli_main

    captured: dict[str, Any] = {}

    class _Collection:
        results: tuple = ()
        failures: tuple = ()

    class _Result:
        collection = _Collection()
        candidates: tuple = ()
        scored: tuple = ()
        reported_count = 0
        zamien_count = 0
        committed = 0

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    rc = cli_main(["run", "--state", str(tmp_path / "state.json"), "--report", "-"])

    assert rc == 0
    assert tuple(captured["adapters"]) == EXPECTED_ACTIVE_SOURCES


def test_production_build_wires_the_wide_useme_category_list():
    """Stage 5b: a production run collects five Useme lanes."""
    assert ACTIVE_SOURCE_KWARGS["useme"] == {"category_paths": PRODUCTION_CATEGORY_PATHS}

    adapters = build_active_adapters()
    assert adapters["useme"].category_paths == PRODUCTION_CATEGORY_PATHS
    assert len(PRODUCTION_CATEGORY_PATHS) == 5


def test_production_lane_set_is_wired_for_oferteo():
    """Oferteo carries its public lanes as soon as it is switched on."""
    assert ACTIVE_SOURCE_KWARGS["oferteo"] == {
        "category_paths": OFERTEO_PRODUCTION_CATEGORY_PATHS
    }
    adapter = build_adapter(
        "oferteo", category_paths=OFERTEO_PRODUCTION_CATEGORY_PATHS
    )
    assert adapter.category_paths == OFERTEO_PRODUCTION_CATEGORY_PATHS
    # Kolejność nie jest kosmetyką: pierwsza kategoria, która wystawi zapytanie,
    # decyduje o jego metadanych, a lista zaczyna się od najwęższej i najbardziej
    # naszej (strony www), nie od szerokiej listy IT.
    assert OFERTEO_PRODUCTION_CATEGORY_PATHS[0] == "/zlecenia-na-tworzenie-stron-internetowych"
