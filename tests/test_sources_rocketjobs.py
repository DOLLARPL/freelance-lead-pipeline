from pathlib import Path

import pytest
import requests

from freelance_pipeline.http import SourceError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.rocketjobs import RocketJobsError, RocketJobsJobs
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "https://rocketjobs.pl/oferty-pracy"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_session(payload: str | None = None) -> FakeSession:
    return FakeSession(
        routes={LISTING_URL: FakeResponse(payload or load("rocketjobs_listing.html"))}
    )


def test_source_name_and_public_listing_url_are_explicit():
    assert RocketJobsJobs.source == "rocketjobs"
    assert RocketJobsJobs.base_url == LISTING_URL


def test_collect_returns_normalized_unique_visible_jobleads():
    adapter = RocketJobsJobs(session=make_session())

    leads = adapter.collect()

    assert len(leads) == 3
    lead = leads[0]
    assert isinstance(lead, JobLead)
    assert lead.source == "rocketjobs"
    assert lead.source_id == "acrylmed-samodzielny-specjalista-ds-zakupow-branza-chemiczna--srem-logistyka-logistyka"
    assert lead.url == (
        "https://rocketjobs.pl/oferta-pracy/"
        "acrylmed-samodzielny-specjalista-ds-zakupow-branza-chemiczna--srem-logistyka-logistyka"
    )
    assert lead.canonical_url == lead.url
    assert lead.title == "Samodzielny specjalista ds. zakupów (branża chemiczna)"
    assert lead.company_or_author == "Acrylmed"
    assert lead.location == "Śrem"
    assert lead.work_mode == "Stacjonarnie"
    assert lead.contract_type == "Umowa o pracę"
    assert lead.budget == 7000.0
    assert lead.currency == "PLN"
    assert lead.skills == ("język angielski", "analiza", "negocjacje")
    assert lead.status == "active"
    assert lead.raw_data["slug"] == lead.source_id
    assert lead.raw_data["salary_text"] == "7 000 - 9 000 PLN/mies."


def test_offer_without_published_salary_has_null_budget():
    leads = RocketJobsJobs(session=make_session()).collect()
    market_pr = next(lead for lead in leads if "marketingu" in lead.title.lower())

    assert market_pr.budget is None
    assert market_pr.currency is None
    assert market_pr.work_mode == "Hybrydowo"
    assert market_pr.location == "Warszawa"
    assert market_pr.contract_type == "B2B, Umowa zlecenie"
    assert market_pr.skills == ("marketing", "PR")
    assert market_pr.raw_data["salary_text"] == "Wynagrodzenie nieujawnione"


def test_remote_offer_is_normalized():
    leads = RocketJobsJobs(session=make_session()).collect()
    remote = next(lead for lead in leads if lead.source_id.startswith("tigers"))

    assert remote.title == "Social Media Specialist"
    assert remote.work_mode == "Zdalnie"
    assert remote.budget == 8000.0
    assert remote.currency == "PLN"


def test_collect_uses_one_robots_allowed_public_page_without_query_pagination():
    adapter = RocketJobsJobs(session=make_session())

    adapter.collect()

    assert [call[0] for call in adapter._http._session.calls] == [LISTING_URL]
    assert "/api/" not in adapter.base_url
    assert "?" not in adapter.base_url
    assert "," not in adapter.base_url


def test_duplicate_slug_offers_are_deduplicated():
    leads = RocketJobsJobs(session=make_session()).collect()
    source_ids = [lead.source_id for lead in leads]

    assert len(source_ids) == len(set(source_ids))
    assert source_ids.count("acrylmed-samodzielny-specjalista-ds-zakupow-branza-chemiczna--srem-logistyka-logistyka") == 1


def test_category_and_navigation_links_are_never_collected():
    leads = RocketJobsJobs(session=make_session()).collect()

    for lead in leads:
        assert lead.url.startswith("https://rocketjobs.pl/oferta-pracy/")
        assert "/oferty-pracy/" not in lead.url
        assert "/api/" not in lead.url
    assert all("/oferty-pracy/" not in lead.source_id for lead in leads)


def test_malformed_card_with_non_offer_href_is_skipped():
    leads = RocketJobsJobs(session=make_session()).collect()

    assert all("zly-link" not in lead.source_id for lead in leads)
    assert all("/oferty-pracy/wszystkie-lokalizacje/sales" not in lead.url for lead in leads)


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/oferta-pracy/evil-offer",
        "//evil.example/oferta-pracy/proto-offer",
        "https://rocketjobs.pl.evil.com/oferta-pracy/squat-offer",
        "https://rocketjobs.pl@evil.com/oferta-pracy/userinfo-offer",
        "https://evil.example./oferta-pracy/dot-offer",
        "https://notrocketjobs.pl/oferta-pracy/prefix-offer",
        "mailto:evil@example.com",
    ],
)
def test_cross_host_or_protocol_relative_offer_hrefs_are_rejected(href):
    assert RocketJobsJobs._slug_from_url(href) is None


@pytest.mark.parametrize(
    "href, expected_slug",
    [
        ("/oferta-pracy/relative-offer", "relative-offer"),
        ("https://rocketjobs.pl/oferta-pracy/absolute-offer", "absolute-offer"),
        ("https://www.rocketjobs.pl/oferta-pracy/www-offer", "www-offer"),
        ("https://ROCKETJOBS.PL/oferta-pracy/upper-offer", "upper-offer"),
        ("//rocketjobs.pl/oferta-pracy/proto-same-slug", "proto-same-slug"),
        ("https://rocketjobs.pl:8443/oferta-pracy/port-offer", "port-offer"),
    ],
)
def test_rocketjobs_hosted_offer_hrefs_are_accepted(href, expected_slug):
    assert RocketJobsJobs._slug_from_url(href) == expected_slug


def test_host_validation_collects_only_rocketjobs_hosted_offers():
    leads = RocketJobsJobs(session=make_session(load("rocketjobs_listing_hosts.html"))).collect()

    slugs = {lead.source_id for lead in leads}
    assert slugs == {
        "relative-offer",
        "absolute-offer",
        "www-offer",
        "proto-same-slug",
        "port-offer",
    }
    urls = {lead.url for lead in leads}
    assert urls == {
        "https://rocketjobs.pl/oferta-pracy/relative-offer",
        "https://rocketjobs.pl/oferta-pracy/absolute-offer",
        "https://rocketjobs.pl/oferta-pracy/www-offer",
        "https://rocketjobs.pl/oferta-pracy/proto-same-slug",
        "https://rocketjobs.pl/oferta-pracy/port-offer",
    }


def test_malicious_cross_host_cards_are_skipped_and_cannot_become_leads():
    leads = RocketJobsJobs(session=make_session(load("rocketjobs_listing_hosts.html"))).collect()

    slugs = {lead.source_id for lead in leads}
    for malicious in (
        "evil-offer",
        "proto-offer",
        "squat-offer",
        "userinfo-offer",
        "dot-offer",
        "prefix-offer",
    ):
        assert malicious not in slugs
    assert all(lead.url.startswith("https://rocketjobs.pl/oferta-pracy/") for lead in leads)


def test_max_pages_must_be_plain_int_one():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        RocketJobsJobs(session=make_session(), max_pages=2)
    with pytest.raises(ValueError, match="max_pages must be 1"):
        RocketJobsJobs(session=make_session(), max_pages=0)


def test_max_pages_equal_one_is_accepted():
    leads = RocketJobsJobs(session=make_session(), max_pages=1).collect()
    assert len(leads) == 3


def test_max_pages_rejects_bool():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        RocketJobsJobs(session=make_session(), max_pages=True)
    with pytest.raises(ValueError, match="max_pages must be 1"):
        RocketJobsJobs(session=make_session(), max_pages=False)


def test_max_pages_rejects_non_integer():
    with pytest.raises(ValueError, match="max_pages must be 1"):
        RocketJobsJobs(session=make_session(), max_pages=1.0)


def test_missing_listing_structure_raises_source_specific_parse_error():
    with pytest.raises(RocketJobsError, match="listing page structure"):
        RocketJobsJobs(session=make_session("<html></html>")).collect()


def test_valid_empty_listing_returns_empty_list():
    assert RocketJobsJobs(session=make_session(load("rocketjobs_listing_empty.html"))).collect() == []


def test_malformed_page_without_listing_shell_raises():
    with pytest.raises(RocketJobsError):
        RocketJobsJobs(session=make_session("<html><body>blocked by bot protection</body></html>")).collect()


def test_http_failure_is_wrapped_in_source_specific_source_error():
    session = FakeSession(routes={LISTING_URL: FakeResponse("err", status_code=503)})
    with pytest.raises(RocketJobsError):
        RocketJobsJobs(session=session).collect()
    with pytest.raises(SourceError):
        RocketJobsJobs(session=session).collect()


def test_timeout_is_wrapped_in_source_specific_error():
    session = FakeSession(errors={LISTING_URL: requests.Timeout("slow")})
    with pytest.raises(RocketJobsError):
        RocketJobsJobs(session=session).collect()