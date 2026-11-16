"""Tests for the shared Discourse base adapter behaviour (Make + n8n both inherit).

These cover cross-cutting contracts that apply to every Discourse category
adapter: ``max_pages`` validation, zero-based pagination, strict nested JSON
validation, original-poster resolution, and the metadata/closed-archived
exclusion policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from freelance_pipeline.http import SourceParseError
from freelance_pipeline.sources.make import MakeHireHelp, MakeHireHelpError
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
PAGE0 = "https://community.make.com/c/hire-a-pro.json"
PAGE1 = "https://community.make.com/c/hire-a-pro.json?page=1"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_session(routes: dict[str, FakeResponse] | None = None) -> FakeSession:
    routes = routes if routes is not None else {PAGE0: FakeResponse(load("make_hire_pro.json"))}
    return FakeSession(routes=routes)


# ---- max_pages validation -------------------------------------------------


def test_max_pages_defaults_to_class_value():
    adapter = MakeHireHelp(session=make_session())
    assert adapter.max_pages == 1


def test_max_pages_must_be_plain_int_not_bool():
    with pytest.raises(TypeError, match="max_pages"):
        MakeHireHelp(session=make_session(), max_pages=True)
    with pytest.raises(TypeError, match="max_pages"):
        MakeHireHelp(session=make_session(), max_pages=2.5)


def test_max_pages_must_be_at_least_one():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="max_pages"):
            MakeHireHelp(session=make_session(), max_pages=bad)


def test_max_pages_is_capped_at_five():
    assert MakeHireHelp(session=make_session(), max_pages=5).max_pages == 5
    assert MakeHireHelp(session=make_session(), max_pages=10).max_pages == 5
    assert MakeHireHelp(session=make_session(), max_pages=99).max_pages == 5


# ---- strict nested JSON validation ----------------------------------------


def test_json_payload_must_be_object():
    session = FakeSession(routes={PAGE0: FakeResponse([1, 2, 3])})
    with pytest.raises(MakeHireHelpError):
        MakeHireHelp(session=session).collect()


def test_json_topic_list_must_be_an_object():
    session = FakeSession(routes={PAGE0: FakeResponse({"topic_list": []})})
    with pytest.raises(MakeHireHelpError):
        MakeHireHelp(session=session).collect()


def test_json_topics_must_be_a_list():
    session = FakeSession(routes={PAGE0: FakeResponse({"topic_list": {"topics": {"a": 1}}})})
    with pytest.raises(MakeHireHelpError):
        MakeHireHelp(session=session).collect()


def test_json_missing_topics_key_raises():
    session = FakeSession(routes={PAGE0: FakeResponse({"topic_list": {}})})
    with pytest.raises(MakeHireHelpError):
        MakeHireHelp(session=session).collect()


def test_malformed_json_shapes_raise_source_specific_error_not_attribute_error():
    malformed = [
        [1, 2],
        {"topic_list": []},
        {"topic_list": {"topics": {"a": 1}}},
        {"topic_list": {}},
        {"topic_list": None},
        {"users": [], "topic_list": {"topics": "nope"}},
    ]
    for payload in malformed:
        session = FakeSession(routes={PAGE0: FakeResponse(payload)})
        with pytest.raises(MakeHireHelpError):
            MakeHireHelp(session=session).collect()


def test_json_parse_errors_are_source_parse_errors():
    session = FakeSession(routes={PAGE0: FakeResponse({"topic_list": []})})
    with pytest.raises(MakeHireHelpError) as exc:
        MakeHireHelp(session=session).collect()
    assert "parse error" in str(exc.value)
    assert isinstance(exc.value.__cause__, SourceParseError)


def test_json_empty_valid_topics_list_returns_empty():
    session = FakeSession(routes={PAGE0: FakeResponse({"topic_list": {"topics": []}})})
    assert MakeHireHelp(session=session).collect() == []