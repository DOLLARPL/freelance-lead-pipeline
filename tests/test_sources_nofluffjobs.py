from pathlib import Path
from typing import get_type_hints

import pytest
import requests
from bs4 import BeautifulSoup

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.nofluffjobs import NoFluffJobsError, NoFluffJobsJobs
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "https://nofluffjobs.com"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_session(payload: str | None = None) -> FakeSession:
    return FakeSession(
        routes={LISTING_URL: FakeResponse(payload or load("nofluffjobs_listing.html"))}
    )


def test_source_name_and_public_listing_url_are_explicit():
    assert NoFluffJobsJobs.source == "nofluffjobs"
    assert NoFluffJobsJobs.base_url == LISTING_URL


def test_collect_returns_normalized_unique_visible_jobleads():
    leads = NoFluffJobsJobs(session=make_session()).collect()

    assert len(leads) == 4
    lead = next(lead for lead in leads if lead.source_id.startswith("java-developer"))
    assert isinstance(lead, JobLead)
    assert lead.source == "nofluffjobs"
    assert lead.source_id == "java-developer-connectis--remote-43"
    assert lead.url == "https://nofluffjobs.com/job/java-developer-connectis--remote-43"
    assert lead.canonical_url == lead.url
    assert lead.title == "Java Developer"
    assert lead.company_or_author == "Spyrosoft"
    assert lead.location == "Remote"
    assert lead.budget == 18000.0
    assert lead.currency == "PLN"
    assert lead.skills == ("Backend", "Java", "Spring")
    assert lead.status == "active"
    assert lead.raw_data["slug"] == lead.source_id
    assert lead.raw_data["salary_text"] == "18 000 – 21 000 PLN / month"
    assert "NEW" not in lead.title


def test_salary_range_upper_bound_is_kept_in_raw_data():
    leads = NoFluffJobsJobs(session=make_session()).collect()
    lead = next(lead for lead in leads if lead.source_id == "java-developer-connectis--remote-43")

    assert lead.raw_data["salary_to"] == 21000.0


def test_offer_without_disclosed_salary_has_null_budget():
    leads = NoFluffJobsJobs(session=make_session()).collect()
    qa = next(lead for lead in leads if lead.source_id.startswith("qa-engineer"))

    assert qa.budget is None
    assert qa.currency is None
    assert qa.company_or_author == "Code & Pepper"
    assert qa.location == "Remote"
    assert qa.skills == ("Testing", "SoapUI")
    assert qa.raw_data["salary_text"] == "Sprawdź wynagrodzenie"


def test_single_amount_salary_is_parsed():
    leads = NoFluffJobsJobs(session=make_session()).collect()
    gft = next(lead for lead in leads if lead.source_id.startswith("senior-python"))

    assert gft.title == "Senior Python Engineer"
    assert gft.budget == 14580.0
    assert gft.raw_data["salary_to"] is None
    assert gft.currency == "PLN"
    assert gft.company_or_author == "GFT Poland"


def test_salary_parses_a_bare_single_amount():
    assert NoFluffJobsJobs._salary("14 580 PLN / month") == (14580.0, None, "PLN")


def test_salary_annotation_has_lower_upper_currency_arity():
    hints = get_type_hints(NoFluffJobsJobs._salary)
    assert hints["return"] == tuple[float | None, float | None, str | None]


def test_absolute_same_host_href_is_normalized_to_canonical_url():
    leads = NoFluffJobsJobs(session=make_session()).collect()
    absolute = next(lead for lead in leads if lead.source_id == "absolute-offer")

    assert absolute.url == "https://nofluffjobs.com/job/absolute-offer"
    assert absolute.canonical_url == absolute.url


def test_collect_uses_one_robots_allowed_public_page_without_query_pagination():
    adapter = NoFluffJobsJobs(session=make_session())

    adapter.collect()

    assert [call[0] for call in adapter._http._session.calls] == [LISTING_URL]
    assert "/api/" not in adapter.base_url
    assert "?" not in adapter.base_url
    assert ",criteria" not in adapter.base_url


def test_duplicate_slug_cards_are_deduplicated():
    leads = NoFluffJobsJobs(session=make_session()).collect()
    source_ids = [lead.source_id for lead in leads]

    assert len(source_ids) == len(set(source_ids))
    assert source_ids.count("java-developer-connectis--remote-43") == 1
    assert len(leads) == 4


def test_navigation_and_disallowed_links_are_never_collected():
    leads = NoFluffJobsJobs(session=make_session()).collect()

    for lead in leads:
        assert lead.url.startswith("https://nofluffjobs.com/job/")
        assert "/companies" not in lead.url
        assert "evil.example" not in lead.url
    slugs = {lead.source_id for lead in leads}
    for banned in ("companies", "evil-offer", "posting"):
        assert banned.split("/")[-1] not in slugs


def test_closed_or_unavailable_card_is_skipped():
    leads = NoFluffJobsJobs(session=make_session()).collect()

    assert all("senior-net-software-engineer-iteamly-krakow" not in lead.source_id for lead in leads)


@pytest.mark.parametrize(
    "status_text",
    [
        "Niedostępne",
        "Niedostępna",
        "Wygaśnięto",
        "Przeterminowano",
        "Usunięto",
        "Zarchiwizowano",
    ],
)
def test_polish_closed_markers_are_recognized(status_text):
    card = BeautifulSoup(
        f'<a class="posting-list-item" href="/job/closed-pl">'
        f"<nfj-posting-application-status>{status_text}</nfj-posting-application-status>"
        f"</a>",
        "lxml",
    ).select_one("a.posting-list-item")

    assert NoFluffJobsJobs._is_closed(card) is True


def test_malformed_card_without_title_is_skipped():
    leads = NoFluffJobsJobs(session=make_session()).collect()

    assert "malformed-card-xyz" not in {lead.source_id for lead in leads}


def test_cross_host_card_is_skipped():
    leads = NoFluffJobsJobs(session=make_session()).collect()

    assert "evil-offer" not in {lead.source_id for lead in leads}
    assert all("evil.example" not in lead.url for lead in leads)


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/job/evil-offer",
        "//evil.example/job/proto-offer",
        "https://nofluffjobs.com.evil.com/job/squat-offer",
        "https://nofluffjobs.com@evil.com/job/userinfo-offer",
        "https://nofluffjobs.pl/job/prefix-offer",
        "https://nofluffjobs.com:8443/job/port-offer",
        "http://nofluffjobs.com/job/http-offer",
        "https://user@nofluffjobs.com/job/userinfo-offer",
        "https://user:pass@nofluffjobs.com/job/userinfo-offer",
        "mailto:evil@example.com",
    ],
)
def test_cross_host_offer_hrefs_are_rejected(href):
    assert NoFluffJobsJobs._slug_from_url(href) is None


@pytest.mark.parametrize(
    "href, expected_slug",
    [
        ("/job/relative-offer", "relative-offer"),
        ("/pl/job/polish-offer", "polish-offer"),
        ("/hu/job/hungarian-offer", "hungarian-offer"),
        ("/ua-job?", None),
        ("/job/absolute-offer", "absolute-offer"),
        ("https://nofluffjobs.com/job/absolute-offer", "absolute-offer"),
        ("https://WWW.nofluffjobs.com/job/www-offer", "www-offer"),
        ("https://nofluffjobs.com:443/job/port-443-offer", "port-443-offer"),
        ("https://nofluffjobs.com/pl/job/polish-offer", "polish-offer"),
        ("https://nofluffjobs.com/job/double--hyphen-offer", "double--hyphen-offer"),
    ],
)
def test_nofluffjobs_hosted_offer_hrefs_are_accepted(href, expected_slug):
    assert NoFluffJobsJobs._slug_from_url(href) == expected_slug


def test_max_pages_must_be_plain_int_one():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        NoFluffJobsJobs(session=make_session(), max_pages=2)
    with pytest.raises(ValueError, match="max_pages must be 1"):
        NoFluffJobsJobs(session=make_session(), max_pages=0)


def test_max_pages_equal_one_is_accepted():
    leads = NoFluffJobsJobs(session=make_session(), max_pages=1).collect()
    assert len(leads) == 4


def test_max_pages_rejects_bool():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        NoFluffJobsJobs(session=make_session(), max_pages=True)
    with pytest.raises(ValueError, match="max_pages must be 1"):
        NoFluffJobsJobs(session=make_session(), max_pages=False)


def test_max_pages_rejects_non_integer():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        NoFluffJobsJobs(session=make_session(), max_pages=1.0)


def test_valid_empty_listing_returns_empty_list():
    assert NoFluffJobsJobs(session=make_session(load("nofluffjobs_listing_empty.html"))).collect() == []


def test_malformed_page_without_listing_shell_raises_source_specific_parse_error():
    with pytest.raises(NoFluffJobsError, match="listing page structure"):
        NoFluffJobsJobs(session=make_session("<html><body>blocked by bot protection</body></html>")).collect()
    with pytest.raises(SourceError):
        NoFluffJobsJobs(session=make_session("<html></html>")).collect()


def test_http_failure_is_wrapped_in_source_specific_source_error():
    session = FakeSession(routes={LISTING_URL: FakeResponse("err", status_code=503)})
    with pytest.raises(NoFluffJobsError):
        NoFluffJobsJobs(session=session).collect()
    with pytest.raises(SourceError):
        NoFluffJobsJobs(session=session).collect()


def test_timeout_is_wrapped_in_source_specific_error():
    session = FakeSession(errors={LISTING_URL: requests.Timeout("slow")})
    with pytest.raises(NoFluffJobsError):
        NoFluffJobsJobs(session=session).collect()