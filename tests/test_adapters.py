from freelance_pipeline.adapters import SourceAdapter, is_source_adapter
from freelance_pipeline.models import JobLead


def make_lead():
    return JobLead(source="useme", source_id="1", url="https://u.com/j/1")


def test_source_adapter_is_runtime_checkable_protocol():
    assert callable(SourceAdapter.collect)


def test_duck_typed_adapter_satisfies_protocol():
    class FakeAdapter:
        def collect(self) -> list[JobLead]:
            return []

    assert isinstance(FakeAdapter(), SourceAdapter)
    assert is_source_adapter(FakeAdapter())


def test_adapter_missing_collect_does_not_satisfy_protocol():
    class BrokenAdapter:
        def other(self) -> None:
            return None

    assert not isinstance(BrokenAdapter(), SourceAdapter)
    assert not is_source_adapter(BrokenAdapter())


def test_non_callable_collect_does_not_satisfy_protocol():
    class StaticAdapter:
        collect = "not-a-method"

    assert is_source_adapter(StaticAdapter()) is False


def test_collect_returns_list_of_jobleads():
    class StubAdapter:
        def collect(self) -> list[JobLead]:
            return [make_lead()]

    leads = StubAdapter().collect()
    assert isinstance(leads, list)
    assert all(isinstance(lead, JobLead) for lead in leads)
    assert is_source_adapter(StubAdapter())