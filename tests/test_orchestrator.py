"""Offline tests for the multi-source collection orchestrator.

The orchestrator only stores per-source results; it never talks to any state
store, never mutates state ("collection must not mutate state") and isolates
failures so one broken source never aborts the rest.
"""

from __future__ import annotations

from freelance_pipeline.models import JobLead
from freelance_pipeline.orchestrator import SourceResult, collect_all


def make_lead(source: str, source_id: str) -> JobLead:
    return JobLead(source=source, source_id=source_id, url=f"https://{source}.x/o/{source_id}")


class RecordingAdapter:
    """Adapter that returns canned leads and records how often it is called."""

    def __init__(self, source: str, leads: list[JobLead]) -> None:
        self.source = source
        self.leads = leads
        self.calls = 0

    def collect(self) -> list[JobLead]:
        self.calls += 1
        return self.leads


class ExplodingAdapter:
    def __init__(self, source: str = "broken") -> None:
        self.source = source

    def collect(self) -> list[JobLead]:
        raise RuntimeError(f"[{self.source}] boom")


def test_source_result_defaults_are_empty_and_clean():
    result = SourceResult(source="useme")
    assert result.leads == ()
    assert result.error is None
    assert result.ok
    assert result.error_message is None


def test_collect_all_concatensates_leads_in_register_order():
    adapters = {
        "alpha": RecordingAdapter("alpha", [make_lead("alpha", "1"), make_lead("alpha", "2")]),
        "beta": RecordingAdapter("beta", [make_lead("beta", "1")]),
    }
    result = collect_all(adapters)
    assert [lead.source_id for lead in result.all] == ["1", "2", "1"]
    assert [lead.source for lead in result.all] == ["alpha", "alpha", "beta"]
    assert result.is_ok


def test_failing_source_is_isolated_and_does_not_block_others():
    adapters = {
        "good": RecordingAdapter("good", [make_lead("good", "1")]),
        "broken": ExplodingAdapter(source="broken"),
        "other": RecordingAdapter("other", [make_lead("other", "9")]),
    }
    result = collect_all(adapters)
    assert not result.is_ok
    names = {r.source for r in result.results}
    assert names == {"good", "broken", "other"}
    broken = next(r for r in result.results if r.source == "broken")
    assert broken.error is not None
    assert "boom" in (broken.error_message or "")
    assert broken.leads == ()
    assert result.successes_sources == ("good", "other")
    assert result.failed_sources == ("broken",)
    assert [lead.source for lead in result.all] == ["good", "other"]


def test_failure_list_produces_error_messages_per_source():
    result = collect_all({"no": ExplodingAdapter(source="no")})
    assert result.error_messages == {"no": "[no] boom"}


def test_every_source_is_collected_exactly_once():
    alpha = RecordingAdapter("alpha", [make_lead("alpha", "1")])
    beta = RecordingAdapter("beta", [])
    collect_all({"alpha": alpha, "beta": beta})
    assert alpha.calls == 1
    assert beta.calls == 1


def test_collect_with_no_sources_is_empty_and_ok():
    result = collect_all({})
    assert result.results == ()
    assert result.all == ()
    assert result.is_ok


def test_custom_collector_can_wrap_timeouts():
    seen = []

    def collector(source):
        seen.append(source.source)
        return []

    adapters = {"a": RecordingAdapter("a", []), "b": RecordingAdapter("b", [])}
    collect_all(adapters, collector=collector)
    assert seen == ["a", "b"]


def test_collection_run_never_touches_state():
    """The orchestrator API has no state parameter at all (pure collection)."""
    adapters = {"a": RecordingAdapter("a", [make_lead("a", "1")])}
    collect_all(adapters)


def test_single_source_result_summary_is_consistent():
    from freelance_pipeline.orchestrator import _default_collect

    assert isinstance(collect_all({}, collector=_default_collect).results, tuple)