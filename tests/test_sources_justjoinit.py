from datetime import UTC
from pathlib import Path

import pytest
import requests

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.justjoinit import JustJoinITError, JustJoinITJobs
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "https://justjoin.it/job-offers/all-locations"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_session(payload: str | None = None) -> FakeSession:
    return FakeSession(
        routes={LISTING_URL: FakeResponse(payload or load("justjoinit_listing.html"))}
    )


def test_source_name_and_public_listing_url_are_explicit():
    assert JustJoinITJobs.source == "justjoinit"
    assert JustJoinITJobs.base_url == LISTING_URL


def test_collect_returns_normalized_unique_visible_jobleads():
    adapter = JustJoinITJobs(session=make_session())

    leads = adapter.collect()

    assert len(leads) == 1
    lead = leads[0]
    assert isinstance(lead, JobLead)
    assert lead.source == "justjoinit"
    assert lead.source_id == "04d217ac-6753-4ba3-a6b8-9f1e44250ea3"
    assert lead.url == "https://justjoin.it/job-offer/acme-python-automation-engineer-warszawa-python"
    assert lead.canonical_url == lead.url
    assert lead.title == "Python Automation Engineer"
    assert lead.description == "Build internal workflow automations"
    assert lead.company_or_author == "Acme Sp. z o.o."
    assert lead.location == "Warszawa, Prosta 1"
    assert lead.work_mode == "hybrid"
    assert lead.contract_type == "b2b"
    assert lead.budget == 12000.0
    assert lead.currency == "PLN"
    assert lead.skills == ("Python", "n8n", "Make")
    assert lead.published_at is not None
    assert lead.published_at.tzinfo is not None
    assert lead.published_at == lead.published_at.astimezone(UTC)
    assert lead.status == "active"
    assert lead.raw_data["displayOffer"] is True


def test_collect_uses_one_robots_allowed_public_page_without_query_pagination():
    adapter = JustJoinITJobs(session=make_session())

    adapter.collect()

    assert [call[0] for call in adapter._http._session.calls] == [LISTING_URL]
    assert "/api/" not in adapter.base_url
    assert "?" not in adapter.base_url


def test_expired_offer_is_excluded():
    payload = load("justjoinit_listing.html").replace(
        '\\"expiredAt\\":\\"2030-12-31T19:00:06Z\\"',
        '\\"expiredAt\\":\\"2020-01-01T00:00:00Z\\"',
    ).replace(
        '\\"guid\\":\\"04d217ac-6753-4ba3-a6b8-9f1e44250ea3\\",'
        '\\"slug\\":\\"duplicate-must-be-skipped\\"',
        '\\"guid\\":\\"duplicate-guid\\",'
        '\\"slug\\":\\"duplicate-must-be-skipped\\"',
    )
    leads = JustJoinITJobs(session=make_session(payload)).collect()
    assert all(lead.source_id != "04d217ac-6753-4ba3-a6b8-9f1e44250ea3" for lead in leads)


def test_max_pages_must_remain_one_because_query_pagination_is_not_used():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        JustJoinITJobs(session=make_session(), max_pages=2)


def test_missing_flight_payload_raises_source_specific_parse_error():
    with pytest.raises(JustJoinITError, match="offers payload"):
        JustJoinITJobs(session=make_session("<html></html>")).collect()


@pytest.mark.parametrize(
    "payload",
    [
        '<script>self.__next_f.push([1,"no offers here"])</script>',
        '<script>self.__next_f.push([1,"1:{\\"state\\":{\\"data\\":{\\"pages\\":{}}}}"])</script>',
    ],
)
def test_malformed_flight_shape_raises_source_specific_error(payload):
    with pytest.raises(JustJoinITError):
        JustJoinITJobs(session=make_session(payload)).collect()


def test_valid_empty_data_is_distinct_from_malformed_payload():
    payload = '<script>self.__next_f.push([1,"1:{\\"state\\":{\\"data\\":{\\"pages\\":[{\\"meta\\":{},\\"data\\":[]}]}}}"])</script>'
    assert JustJoinITJobs(session=make_session(payload)).collect() == []


def test_http_failure_is_wrapped_in_source_specific_source_error():
    session = FakeSession(routes={LISTING_URL: FakeResponse("err", status_code=503)})
    with pytest.raises(JustJoinITError):
        JustJoinITJobs(session=session).collect()
    with pytest.raises(SourceError):
        JustJoinITJobs(session=session).collect()


def test_timeout_is_wrapped_in_source_specific_error():
    session = FakeSession(errors={LISTING_URL: requests.Timeout("slow")})
    with pytest.raises(JustJoinITError):
        JustJoinITJobs(session=session).collect()
