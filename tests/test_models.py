import json
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, tzinfo

import pytest

from freelance_pipeline.models import FrozenDict, FrozenList, JobLead


class _NoneOffset(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


class _RaisingOffset(tzinfo):
    def utcoffset(self, dt):
        raise RuntimeError("boom")

    def dst(self, dt):
        return None


def make_lead(**overrides):
    base = {
        "source": "useme",
        "source_id": "35",
        "url": "https://useme.com/pl/jobs/programowanie-i-it,35/",
    }
    base.update(overrides)
    return JobLead(**base)


def test_builds_with_required_fields_only():
    lead = make_lead()
    assert lead.source == "useme"
    assert lead.source_id == "35"
    assert lead.url == "https://useme.com/pl/jobs/programowanie-i-it,35/"
    assert lead.canonical_url == ""
    assert lead.title == ""
    assert lead.description == ""
    assert lead.company_or_author is None
    assert lead.published_at is None
    assert lead.collected_at is not None
    assert lead.budget is None
    assert lead.currency is None
    assert lead.contract_type is None
    assert lead.work_mode is None
    assert lead.location is None
    assert lead.skills == ()
    assert lead.language is None
    assert lead.status is None
    assert lead.raw_data is None


def test_collected_at_defaults_to_utc_aware():
    lead = make_lead()
    assert lead.collected_at.tzinfo is not None
    assert lead.collected_at.tzinfo == UTC


def test_preserves_supplied_values():
    ts = datetime(2025, 1, 1, tzinfo=UTC)
    lead = make_lead(
        title="Zbuduj automatyzację n8n",
        skills={"n8n", "python"},
        budget=500,
        currency="PLN",
        raw_data={"id": 1, "nested": {"tags": ["a", "b"]}},
    )
    assert lead.title == "Zbuduj automatyzację n8n"
    assert set(lead.skills) == {"n8n", "python"}
    assert isinstance(lead.skills, tuple)
    assert lead.budget == 500.0
    assert lead.currency == "PLN"
    assert lead.raw_data == {"id": 1, "nested": {"tags": ["a", "b"]}}
    assert lead.published_at is None
    lead2 = make_lead(published_at=ts)
    assert lead2.published_at == ts


def test_requires_source_and_source_id_and_url():
    with pytest.raises(TypeError):
        JobLead()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        JobLead(source="useme")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        JobLead(source="useme", source_id="35")  # type: ignore[call-arg]


def test_rejects_empty_or_whitespace_required_fields():
    for kwargs in (
        {"source": ""},
        {"source": "   "},
        {"source_id": "  "},
        {"url": ""},
        {"url": "\t\n"},
    ):
        with pytest.raises(ValueError, match="(source|source_id|url)"):
            make_lead(**kwargs)


def test_rejects_non_string_required_fields():
    with pytest.raises(TypeError):
        make_lead(source=123)
    with pytest.raises(TypeError):
        make_lead(source_id=None)
    with pytest.raises(TypeError):
        make_lead(url=b"https://x")


def test_rejects_non_string_optional_fields():
    with pytest.raises(TypeError):
        make_lead(title=123)
    with pytest.raises(TypeError):
        make_lead(currency=["PLN"])
    with pytest.raises(TypeError):
        make_lead(contract_type=42)


def test_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone"):
        make_lead(published_at=datetime(2025, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        make_lead(collected_at=datetime.now())  # noqa: DTZ005


def test_rejects_non_datetime_timestamps():
    with pytest.raises(TypeError):
        make_lead(published_at="2025-01-01")
    with pytest.raises(TypeError):
        make_lead(collected_at=None)


def test_rejects_nonfinite_or_negative_budget():
    for bad in (-1, -0.5, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="budget"):
            make_lead(budget=bad)


def test_rejects_non_numeric_budget():
    with pytest.raises(TypeError):
        make_lead(budget="500")
    with pytest.raises(TypeError):
        make_lead(budget=True)


def test_budget_accepts_int_and_normalizes_to_float():
    lead = make_lead(budget=500)
    assert lead.budget == 500.0
    assert isinstance(lead.budget, float)
    assert math.isfinite(lead.budget)
    assert lead.budget >= 0


def test_skills_defaults_to_tuple():
    lead = make_lead()
    assert lead.skills == ()


def test_skills_normalized_to_immutable_tuple():
    lead = make_lead(skills={"n8n", "python"})
    assert isinstance(lead.skills, tuple)
    assert set(lead.skills) == {"n8n", "python"}
    lead2 = make_lead(skills=("a", "b", "a"))
    assert lead2.skills == ("a", "b", "a")
    lead3 = make_lead(skills=["a", "b"])
    assert isinstance(lead3.skills, tuple)


def test_skills_rejects_bare_string():
    with pytest.raises(TypeError):
        make_lead(skills="python")


def test_skills_rejects_non_string_items():
    with pytest.raises(TypeError):
        make_lead(skills=[1, 2])


def test_raw_data_is_immutable_and_hashable():
    ts = datetime(2025, 1, 1, tzinfo=UTC)
    lead = make_lead(raw_data={"id": 1, "tags": ["a", "b"]}, collected_at=ts)
    assert lead.raw_data == {"id": 1, "tags": ["a", "b"]}
    assert hash(lead) == hash(
        make_lead(raw_data={"id": 1, "tags": ["a", "b"]}, collected_at=ts)
    )
    with pytest.raises((AttributeError, TypeError)):
        lead.raw_data["id"] = 2
    with pytest.raises((AttributeError, TypeError)):
        lead.raw_data["tags"].append("c")
    assert lead.raw_data["tags"] == ["a", "b"]


def test_raw_data_nested_dict_is_immutable_and_hashable():
    lead = make_lead(raw_data={"a": {"b": {"c": [1, 2]}}})
    assert hash(lead)
    with pytest.raises((AttributeError, TypeError)):
        lead.raw_data["a"]["b"]["c"] = []
    with pytest.raises((AttributeError, TypeError)):
        lead.raw_data["a"]["b"]["c"].append(3)
    assert lead.raw_data["a"]["b"]["c"] == [1, 2]


def test_raw_data_serializes_back_to_plain_json():
    lead = make_lead(
        raw_data={"id": 1, "nested": {"tags": ["a", "b"]}, "n": None, "f": 1.5}
    )
    expected = {"id": 1, "nested": {"tags": ["a", "b"]}, "n": None, "f": 1.5}
    assert json.loads(json.dumps(lead.raw_data.to_dict())) == expected


def test_raw_data_rejects_non_json_values():
    with pytest.raises(TypeError):
        make_lead(raw_data={"tags": {"a", "b"}})


def test_raw_data_rejects_non_string_keys():
    with pytest.raises(TypeError):
        make_lead(raw_data={1: "a"})
    with pytest.raises(TypeError):
        make_lead(raw_data={"a": {None: "x"}})


def test_raw_data_rejects_mixed_keys():
    with pytest.raises(TypeError):
        make_lead(raw_data={1: "a", "b": 2})
    with pytest.raises(TypeError):
        make_lead(raw_data={"a": {1: "x", "y": 2}})


def test_raw_data_allows_empty_string_keys():
    lead = make_lead(raw_data={"": "x", "a": {"": [1]}})
    assert lead.raw_data == {"": "x", "a": {"": [1]}}


def test_raw_data_rejects_datetime():
    with pytest.raises(TypeError):
        make_lead(raw_data={"d": datetime(2025, 1, 1, tzinfo=UTC)})


def test_raw_data_rejects_bytes_and_bytearray():
    with pytest.raises(TypeError):
        make_lead(raw_data={"b": b"x"})
    with pytest.raises(TypeError):
        make_lead(raw_data={"ba": bytearray(b"x")})


def test_raw_data_rejects_arbitrary_objects():
    with pytest.raises(TypeError):
        make_lead(raw_data={"o": object()})


def test_raw_data_rejects_nan_and_infinities():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(TypeError):
            make_lead(raw_data={"n": bad})
        with pytest.raises(TypeError):
            make_lead(raw_data={"nested": [bad]})


def test_raw_data_accepts_json_scalars():
    lead = make_lead(raw_data={"n": None, "b": True, "s": "x", "i": 3, "f": 1.5})
    assert lead.raw_data == {"n": None, "b": True, "s": "x", "i": 3, "f": 1.5}


def test_frozendict_rejects_slot_assignment():
    lead = make_lead(raw_data={"id": 1})
    fd = lead.raw_data
    assert isinstance(fd, FrozenDict)
    h = hash(fd)
    with pytest.raises(AttributeError):
        fd._items = ()  # type: ignore[misc]
    assert hash(fd) == h
    assert fd == {"id": 1}


def test_frozenlist_rejects_slot_assignment():
    lead = make_lead(raw_data={"tags": ["a", "b"]})
    fl = lead.raw_data["tags"]
    assert isinstance(fl, FrozenList)
    h = hash(fl)
    with pytest.raises(AttributeError):
        fl._items = ()  # type: ignore[misc]
    assert hash(fl) == h
    assert fl == ["a", "b"]


def test_rejects_datetime_with_none_utcoffset():
    with pytest.raises(ValueError, match="timezone"):
        make_lead(published_at=datetime(2025, 1, 1, tzinfo=_NoneOffset()))


def test_rejects_datetime_with_raising_utcoffset():
    with pytest.raises(ValueError, match="timezone"):
        make_lead(published_at=datetime(2025, 1, 1, tzinfo=_RaisingOffset()))


def test_is_frozen_and_hashable():
    ts = datetime(2025, 1, 1, tzinfo=UTC)
    lead = make_lead(title="x", collected_at=ts, skills={"n8n"}, raw_data={"id": 1})
    assert hash(lead) == hash(
        make_lead(title="x", collected_at=ts, skills={"n8n"}, raw_data={"id": 1})
    )
    with pytest.raises(FrozenInstanceError):
        lead.title = "mutated"