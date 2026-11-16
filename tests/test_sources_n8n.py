from datetime import UTC
from pathlib import Path

import pytest

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.n8n import N8NCommunityJobs, N8NCommunityJobsError
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
PAGE0 = "https://community.n8n.io/c/jobs.rss"
PAGE1 = "https://community.n8n.io/c/jobs.rss?page=1"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_adapter(session, **kwargs) -> N8NCommunityJobs:
    return N8NCommunityJobs(session=session, **kwargs)


def make_session() -> FakeSession:
    return FakeSession(
        routes={
            PAGE0: FakeResponse(load("n8n_jobs.rss")),
            PAGE1: FakeResponse(load("n8n_jobs_page1.rss")),
        }
    )


def test_source_name_is_explicit():
    assert N8NCommunityJobs.source == "n8n_community_jobs"


def test_base_url_is_configurable():
    adapter = N8NCommunityJobs(base_url="https://community.n8n.io", session=make_session())
    assert adapter.base_url == "https://community.n8n.io"


def test_collect_returns_validated_jobleads():
    leads = make_adapter(make_session()).collect()
    assert isinstance(leads, list)
    assert leads
    assert all(isinstance(lead, JobLead) for lead in leads)
    assert all(lead.source == "n8n_community_jobs" for lead in leads)


def test_normalizes_title_description_author_url_and_time():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["311719"]
    assert lead.source_id == "311719"
    assert lead.title.startswith("[For Hire] n8n builder")
    assert "n8n" in lead.description.lower()
    assert lead.company_or_author == "duc_luu"
    assert lead.published_at is not None
    assert lead.published_at.tzinfo is not None
    assert lead.published_at == lead.published_at.astimezone(UTC)
    assert lead.canonical_url == (
        "https://community.n8n.io/t/for-hire-n8n-builder-in-vietnam-gmt-7-25-to-35-hr-or-fixed-price-per-workflow/311719"
    )
    assert lead.url == lead.canonical_url
    assert lead.raw_data is not None


def test_status_derived_from_title_markers():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    assert leads["311719"].status == "for_hire"
    assert leads["311286"].status == "hiring"
    assert leads["311316"].status == "filled"
    assert leads["276670"].status is None


def test_dedupes_within_one_response_by_source_id():
    leads = make_adapter(make_session()).collect()
    ids = [lead.source_id for lead in leads]
    assert len(ids) == len(set(ids))


def test_skips_malformed_items_without_crashing_whole_source():
    leads = make_adapter(make_session()).collect()
    assert all(lead.source_id != "999999" for lead in leads)


def test_pagination_is_zero_based_and_bounded():
    adapter = make_adapter(make_session(), max_pages=2)
    leads = adapter.collect()
    page_ids = [lead.source_id for lead in leads]
    assert "311610" in page_ids
    assert "311608" in page_ids
    assert len({lead.source_id for lead in leads}) == len(page_ids)
    urls = [url for url, _ in adapter._http._session.calls]
    assert urls == [PAGE0, PAGE1]


def test_pagination_never_skips_first_queried_page():
    adapter = make_adapter(make_session(), max_pages=2)
    adapter.collect()
    urls = [url for url, _ in adapter._http._session.calls]
    assert any("?page=1" in url for url in urls)
    assert not any("?page=2" in url for url in urls)


def test_stops_pagination_early_on_empty_page():
    session = FakeSession(
        routes={
            PAGE0: FakeResponse(load("n8n_jobs.rss")),
            PAGE1: FakeResponse(load("n8n_jobs_empty.rss")),
        }
    )
    adapter = make_adapter(session, max_pages=3)
    leads = adapter.collect()
    urls = [url for url, _ in adapter._http._session.calls]
    assert urls == [PAGE0, PAGE1]
    assert all(lead.source_id not in {"311610", "311608"} for lead in leads)


def test_single_page_by_default():
    adapter = make_adapter(make_session())
    adapter.collect()
    assert len(adapter._http._session.calls) == 1


def test_http_failure_raises_source_specific_error():
    session = FakeSession(routes={PAGE0: FakeResponse("err", status_code=502)})
    with pytest.raises(N8NCommunityJobsError):
        make_adapter(session).collect()


def test_http_failure_is_a_source_error():
    session = FakeSession(routes={PAGE0: FakeResponse("err", status_code=502)})
    with pytest.raises(SourceError):
        make_adapter(session).collect()


def test_malformed_payload_raises_source_specific_error():
    session = FakeSession(routes={PAGE0: FakeResponse("not xml at all")})
    with pytest.raises(N8NCommunityJobsError):
        make_adapter(session).collect()
