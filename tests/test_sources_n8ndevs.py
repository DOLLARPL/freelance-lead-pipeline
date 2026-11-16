from pathlib import Path

import pytest

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.n8ndevs import N8NDevsError, N8NDevsJobs, parse_rate
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
PAGE1 = "https://n8ndevs.com/jobs"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_adapter(session, **kwargs) -> N8NDevsJobs:
    return N8NDevsJobs(session=session, **kwargs)


def make_session() -> FakeSession:
    return FakeSession(routes={PAGE1: FakeResponse(load("n8ndevs_jobs.html"))})


def test_source_name_is_explicit():
    assert N8NDevsJobs.source == "n8ndevs"


def test_collect_returns_validated_jobleads():
    leads = make_adapter(make_session()).collect()
    assert isinstance(leads, list)
    assert leads
    assert all(isinstance(lead, JobLead) for lead in leads)
    assert all(lead.source == "n8ndevs" for lead in leads)


def test_normalizes_fields():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["BEUyULSMTeaf9niihyhYew"]
    assert lead.source_id == "BEUyULSMTeaf9niihyhYew"
    assert lead.title == "B2B Lead Generation & Automation Specialist"
    assert "lead-generation" in lead.description.lower()
    assert lead.skills == ("B2B", "Lead Generation", "n8n", "Automation", "Sales", "Marketing")
    assert lead.canonical_url == "https://n8ndevs.com/jobs/BEUyULSMTeaf9niihyhYew"
    assert lead.url == lead.canonical_url
    assert lead.contract_type == "Freelance"
    assert lead.raw_data is not None


def test_no_absolute_published_at_is_honest():
    lead = make_adapter(make_session()).collect()[0]
    assert lead.published_at is None


def test_second_card_parses():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["zXUVML9oRky6SDBu3SznBg"]
    assert lead.title == "OpenAI Assistant API integration for customer support bot"
    assert lead.skills == ("OpenAI", "API")
    assert lead.contract_type == "Freelance"


# ---------- budget pills (the rate decides if a lead is worth the time) ----------


def test_hourly_pill_becomes_budget_and_rate_metadata():
    lead = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}["BEUyULSMTeaf9niihyhYew"]
    assert lead.budget == 7.0
    assert lead.currency == "USD"
    assert lead.raw_data["rate_min"] == 7.0
    assert lead.raw_data["rate_max"] == 15.0
    assert lead.raw_data["rate_unit"] == "hour"


def test_fixed_pill_becomes_a_project_amount():
    lead = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}["zXUVML9oRky6SDBu3SznBg"]
    assert lead.budget == 250.0
    assert lead.currency == "USD"
    assert lead.raw_data["rate_unit"] == "project"
    assert lead.raw_data["rate_max"] == 250.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$19–40 / hour", (19.0, 40.0, "hour", "USD")),
        ("$15 / hour", (15.0, 15.0, "hour", "USD")),
        ("$80-$100/hr", (80.0, 100.0, "hour", "USD")),
        ("€75–150 / project", (75.0, 150.0, "project", "EUR")),
        ("£20 per hour", (20.0, 20.0, "hour", "GBP")),
        ("€49 / project", (49.0, 49.0, "project", "EUR")),
    ],
)
def test_parse_rate_formats(text, expected):
    rate = parse_rate(text)
    assert rate is not None
    assert (rate.minimum, rate.maximum, rate.unit, rate.currency) == expected


@pytest.mark.parametrize("text", ["", "Freelance", "Negotiable", "as soon as possible"])
def test_parse_rate_never_invents_an_amount(text):
    assert parse_rate(text) is None


def test_skips_malformed_card_without_link():
    leads = make_adapter(make_session()).collect()
    assert all("Malformed card without a job link" not in lead.title for lead in leads)


def test_fetches_exact_allowed_url_without_query_strings():
    adapter = make_adapter(make_session())
    adapter.collect()
    urls = [url for url, _ in adapter._http._session.calls]
    assert urls == ["https://n8ndevs.com/jobs"]
    assert all("?" not in url for url in urls)


def test_rejects_query_string_pagination_due_to_robots():
    with pytest.raises(ValueError, match="robots"):
        N8NDevsJobs(session=make_session(), max_pages=2)


def test_http_failure_raises_source_specific_error():
    session = FakeSession(routes={PAGE1: FakeResponse("err", status_code=500)})
    with pytest.raises(N8NDevsError):
        make_adapter(session).collect()


def test_http_failure_is_a_source_error():
    session = FakeSession(routes={PAGE1: FakeResponse("err", status_code=500)})
    with pytest.raises(SourceError):
        make_adapter(session).collect()