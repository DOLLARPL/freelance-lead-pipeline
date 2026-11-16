from datetime import UTC
from pathlib import Path

import pytest

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.make import MakeHireHelp, MakeHireHelpError
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
PAGE1 = "https://community.make.com/c/hire-a-pro.json"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_adapter(session, **kwargs) -> MakeHireHelp:
    return MakeHireHelp(session=session, **kwargs)


def make_session() -> FakeSession:
    return FakeSession(routes={PAGE1: FakeResponse(load("make_hire_pro.json"))})


def test_source_name_is_explicit():
    assert MakeHireHelp.source == "make_hire_help"


def test_collect_returns_validated_jobleads():
    leads = make_adapter(make_session()).collect()
    assert isinstance(leads, list)
    assert leads
    assert all(isinstance(lead, JobLead) for lead in leads)
    assert all(lead.source == "make_hire_help" for lead in leads)


def test_normalizes_fields_including_explicit_skills():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["114603"]
    assert lead.source_id == "114603"
    assert lead.title == "I'm looking for freelancer for one of my project"
    assert lead.company_or_author == "Bhavesh"
    assert "make.com workflow" in lead.description.lower()
    assert lead.published_at is not None
    assert lead.published_at.tzinfo is not None
    assert lead.published_at == lead.published_at.astimezone(UTC)
    assert lead.canonical_url == (
        "https://community.make.com/t/im-looking-for-freelancer-for-one-of-my-project/114603"
    )
    assert lead.skills == ("mapping",)
    assert lead.raw_data is not None


def test_skills_collected_from_explicit_tags():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert leads["114606"].skills == ("ai", "instagram")
    assert leads["114455"].skills == ("ai", "instagram", "instant-trigger")
    assert leads["113875"].skills == ("ai",)


def test_status_derived_from_title_markers():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert leads["114534"].status == "for_hire"
    assert leads["114603"].status is None


def test_company_or_author_is_original_poster_not_last_poster():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert leads["114603"].company_or_author == "Bhavesh"
    assert leads["114455"].company_or_author == "sakthivel_sk"
    assert leads["113875"].company_or_author == "Ultra_Fund"
    assert leads["114603"].raw_data["last_poster_username"] == "Pathfinder_Automate"


def test_company_or_author_none_when_creator_cannot_be_determined():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert leads["900103"].company_or_author is None
    assert leads["900104"].company_or_author is None


def test_excludes_pinned_and_about_metadata_topics():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert "96837" not in leads
    assert "900105" not in leads


def test_excludes_closed_and_archived_topics():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert "900101" not in leads
    assert "900102" not in leads


def test_no_emitted_lead_is_pinned_closed_or_archived():
    leads = make_adapter(make_session()).collect()
    for lead in leads:
        raw = lead.raw_data.to_dict() if lead.raw_data is not None else {}
        assert not raw.get("pinned")
        assert not raw.get("pinned_globally")
        assert not raw.get("closed")
        assert not raw.get("archived")


def test_dedupes_within_one_response_by_source_id():
    leads = make_adapter(make_session()).collect()
    ids = [lead.source_id for lead in leads]
    assert len(ids) == len(set(ids))


def test_skips_malformed_topic_without_id():
    leads = make_adapter(make_session()).collect()
    assert all(lead.source_id != "" for lead in leads)
    assert all(lead.title != "Malformed topic without an id must be skipped" for lead in leads)


def test_single_page_by_default():
    adapter = make_adapter(make_session())
    adapter.collect()
    assert len(adapter._http._session.calls) == 1


def test_http_failure_raises_source_specific_error():
    session = FakeSession(routes={PAGE1: FakeResponse("err", status_code=503)})
    with pytest.raises(MakeHireHelpError):
        make_adapter(session).collect()


def test_http_failure_is_a_source_error():
    session = FakeSession(routes={PAGE1: FakeResponse("err", status_code=503)})
    with pytest.raises(SourceError):
        make_adapter(session).collect()


def test_malformed_payload_raises_source_specific_error():
    session = FakeSession(routes={PAGE1: FakeResponse("not json")})
    with pytest.raises(MakeHireHelpError):
        make_adapter(session).collect()