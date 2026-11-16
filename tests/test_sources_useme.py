from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_pipeline.http import SourceError, SourceHTTPError
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.useme import (
    PRODUCTION_CATEGORY_PATHS,
    UsemeError,
    UsemeJobs,
    classify_job_href,
    parse_budget,
    parse_published_pl,
)
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"

PAGE0 = "https://useme.com/pl/jobs/category/programowanie-i-it,35/?order_by=-published_on"
PAGE1 = "https://useme.com/pl/jobs/category/programowanie-i-it,35/?order_by=-published_on&page=2"
BASE = "https://useme.com"
D_143984 = f"{BASE}/pl/jobs/poszukiwany-informatyk-programista-do-wdrozenia-autorskiego-projektu-ai-praca-zd,143984/"
D_143981 = f"{BASE}/pl/jobs/integracja-api-cloudtalk-io-notion,143981/"
D_143733 = f"{BASE}/pl/jobs/zalozenie-konta-stripe,143733/"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_adapter(session, **kwargs) -> UsemeJobs:
    return UsemeJobs(session=session, **kwargs)


def make_session() -> FakeSession:
    return FakeSession(
        routes={
            PAGE0: FakeResponse(load("useme_listing_page1.html")),
            PAGE1: FakeResponse(load("useme_listing_page2.html")),
        }
    )


def details_session(**overrides) -> FakeSession:
    routes = {
        PAGE0: FakeResponse(load("useme_listing_page1.html")),
        D_143984: FakeResponse(load("useme_detail_fresh.html")),
        D_143981: FakeResponse(load("useme_detail_fresh.html")),
        D_143733: FakeResponse(load("useme_detail_stale.html")),
    }
    routes.update(overrides.get("routes", {}))
    return FakeSession(routes=routes, errors=overrides.get("errors", {}))


def test_source_name_is_explicit():
    assert UsemeJobs.source == "useme"


def test_default_category_is_programming_pages_1_2_newest():
    adapter = make_adapter(make_session())
    assert adapter.max_pages == 2
    assert adapter.category_path == "/pl/jobs/category/programowanie-i-it,35"
    assert adapter.order_by == "-published_on"


def test_base_url_and_category_are_configurable():
    adapter = make_adapter(make_session(), base_url="https://example.test", category_path="/pl/jobs/category/inne-prace,13")
    assert adapter.base_url == "https://example.test"
    assert adapter.category_path == "/pl/jobs/category/inne-prace,13"


def test_rejects_bad_category_path():
    with pytest.raises(ValueError, match="absolute path"):
        make_adapter(make_session(), category_path="category/programowanie-i-it,35")


def test_collect_returns_validated_jobleads():
    leads = make_adapter(make_session()).collect()
    assert isinstance(leads, list)
    assert leads
    assert all(isinstance(lead, JobLead) for lead in leads)
    assert all(lead.source == "useme" for lead in leads)
    assert all(lead.source_id.isdigit() for lead in leads)


def test_normalizes_listing_fields():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["143981"]
    assert lead.title == "Integracja API - Cloudtalk.io -----> Notion"
    assert lead.company_or_author == "Adam"
    assert lead.description and "cloudtalk" in lead.description.lower()
    assert lead.budget == 500.0
    assert lead.currency == "PLN"
    assert lead.language == "pl"
    assert lead.skills == ("API", "web api")
    assert lead.canonical_url == "https://useme.com/pl/jobs/integracja-api-cloudtalk-io-notion,143981/"
    assert lead.url == lead.canonical_url
    assert lead.published_at is None
    assert lead.raw_data is not None
    assert lead.raw_data["expires_in_days"] == 12
    assert lead.raw_data["offers_count"] == 31
    assert lead.raw_data["category"] == "Aplikacje webowe"
    assert lead.raw_data["slug"] == "integracja-api-cloudtalk-io-notion"


def test_negotiable_budget_is_null():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["143984"]
    assert lead.title == "Poszukiwany informatyk / programista do wdrożenia autorskiego projektu AI (praca zdalna)"
    assert lead.company_or_author == "system.obscore"
    assert lead.budget is None
    assert lead.currency is None
    assert lead.skills == ("API", "automatyzacja")
    assert lead.raw_data["expires_in_days"] == 90


def test_usd_budget_parsed_from_page_two():
    leads = {lead.source_id: lead for lead in make_adapter(make_session()).collect()}
    lead = leads["143722"]
    assert lead.budget == 2000.0
    assert lead.currency == "USD"


def test_skips_completed_and_stale_cards():
    leads = make_adapter(make_session()).collect()
    ids = {lead.source_id for lead in leads}
    assert "199001" not in ids
    assert "199002" not in ids


def test_skips_malformed_card_without_job_link():
    leads = make_adapter(make_session()).collect()
    assert all("Uszkodzona karta" not in lead.title for lead in leads)


def test_dedupes_across_pages_by_source_id():
    leads = make_adapter(make_session()).collect()
    ids = [lead.source_id for lead in leads]
    assert len(ids) == len(set(ids))
    assert ids.count("143733") == 1


def test_excludes_category_and_navigation_links():
    leads = make_adapter(make_session()).collect()
    ids = {lead.source_id for lead in leads}
    assert ids == {"143984", "143981", "143733", "143652", "143722"}
    assert all("/category/" not in lead.url for lead in leads)
    assert ids.isdisjoint({"35", "34", "36", "100", "102"})


def test_pagination_fetches_pages_1_and_2_newest():
    adapter = make_adapter(make_session())
    leads = adapter.collect()
    calls = [url for url, _ in adapter._http._session.calls]
    assert calls == [PAGE0, PAGE1]
    ids = {lead.source_id for lead in leads}
    assert "143984" in ids
    assert "143652" in ids


def test_stops_pagination_early_on_empty_page():
    session = FakeSession(
        routes={
            PAGE0: FakeResponse(load("useme_listing_page1.html")),
            PAGE1: FakeResponse(load("useme_listing_empty.html")),
        }
    )
    adapter = make_adapter(session, max_pages=3)
    leads = adapter.collect()
    calls = [url for url, _ in adapter._http._session.calls]
    assert calls == [PAGE0, PAGE1]
    assert {lead.source_id for lead in leads} == {"143984", "143981", "143733"}


def test_summary_mode_does_not_fetch_details():
    adapter = make_adapter(make_session())
    adapter.collect()
    calls = [url for url, _ in adapter._http._session.calls]
    assert set(calls) == {PAGE0, PAGE1}
    assert all("order_by=" in url for url in calls)


def test_rejects_non_int_max_pages():
    with pytest.raises(TypeError):
        make_adapter(make_session(), max_pages="2")
    with pytest.raises(TypeError):
        make_adapter(make_session(), max_pages=True)


def test_rejects_max_pages_below_one():
    with pytest.raises(ValueError):
        make_adapter(make_session(), max_pages=0)


def test_caps_max_pages_at_five():
    adapter = make_adapter(make_session(), max_pages=50)
    assert adapter.max_pages == 5


def test_http_failure_raises_source_specific_error():
    session = FakeSession(routes={PAGE0: FakeResponse("err", status_code=502)})
    with pytest.raises(UsemeError):
        make_adapter(session, max_pages=1).collect()


def test_http_failure_is_a_source_error():
    session = FakeSession(routes={PAGE0: FakeResponse("err", status_code=502)})
    with pytest.raises(SourceError):
        make_adapter(session, max_pages=1).collect()


def test_parse_failure_raises_source_specific_error(monkeypatch):
    import freelance_pipeline.sources.useme as mod

    session = FakeSession(routes={PAGE0: FakeResponse("<div>ok</div>")})

    def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(mod, "BeautifulSoup", boom)
    with pytest.raises(UsemeError):
        make_adapter(session, max_pages=1).collect()


def test_first_page_empty_html_raises_source_specific_error():
    session = FakeSession(routes={PAGE0: FakeResponse("")})
    with pytest.raises(UsemeError, match="listing page structure"):
        make_adapter(session, max_pages=1).collect()


def test_first_page_challenge_html_raises_source_specific_error():
    challenge = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"
    session = FakeSession(routes={PAGE0: FakeResponse(challenge)})
    with pytest.raises(UsemeError, match="listing page structure"):
        make_adapter(session, max_pages=1).collect()


def test_valid_first_page_shell_with_zero_cards_is_empty_collection():
    session = FakeSession(routes={PAGE0: FakeResponse(load("useme_listing_empty.html"))})
    assert make_adapter(session, max_pages=1).collect() == []


# ---- details (optional) mode ----

def test_details_are_off_by_default():
    assert make_adapter(make_session()).fetch_details is False


def test_rejects_non_bool_fetch_details():
    with pytest.raises(TypeError):
        make_adapter(make_session(), fetch_details="yes")


def test_details_mode_enriches_and_skips_stale():
    adapter = make_adapter(details_session(), max_pages=1, fetch_details=True)
    leads = {lead.source_id: lead for lead in adapter.collect()}
    assert "143733" not in leads
    lead = leads["143981"]
    assert "Pełny opis oferty widoczny na stronie szczegółów zlecenia" in lead.description
    assert lead.budget == 750.0
    assert lead.currency == "EUR"
    assert lead.published_at is not None
    assert lead.published_at.tzinfo is not None
    assert lead.raw_data["published_text"] == "5 dni temu"
    assert leads["143984"].budget == 750.0
    assert leads["143984"].currency == "EUR"


def test_details_mode_skips_completed_offer():
    session = details_session(
        routes={
            D_143981: FakeResponse(load("useme_detail_completed.html")),
            D_143733: FakeResponse(load("useme_detail_fresh.html")),
        }
    )
    adapter = make_adapter(session, max_pages=1, fetch_details=True)
    leads = {lead.source_id for lead in adapter.collect()}
    assert "143981" not in leads
    assert {"143984", "143733"} <= leads


def test_details_mode_retains_summary_lead_when_detail_fetch_fails():
    session = details_session(
        routes={D_143733: FakeResponse(load("useme_detail_fresh.html"))},
        errors={D_143981: SourceHTTPError("useme", "boom", url=D_143981)},
    )
    adapter = make_adapter(session, max_pages=1, fetch_details=True)
    leads = {lead.source_id: lead for lead in adapter.collect()}
    assert "143981" in leads
    assert leads["143981"].description and "cloudtalk" in leads["143981"].description.lower()
    assert leads["143981"].budget == 500.0
    assert {"143984", "143733"} <= set(leads)


def test_parse_published_pl_accepts_absolute_iso_date():
    parsed = parse_published_pl("2026-09-03")
    assert parsed == datetime(2026, 9, 3, tzinfo=UTC)


def test_details_mode_keeps_lead_when_detail_page_unparseable():
    session = details_session(
        routes={url: FakeResponse("<html><body><p>Oops</p></body></html>") for url in (D_143984, D_143981, D_143733)}
    )
    adapter = make_adapter(session, max_pages=1, fetch_details=True)
    leads = {lead.source_id: lead for lead in adapter.collect()}
    assert set(leads) == {"143984", "143981", "143733"}
    assert all(lead.published_at is None for lead in leads.values())
    assert all(lead.description for lead in leads.values())


# ---- pure helpers ----

def test_classify_job_href_accepts_real_offer_links():
    assert classify_job_href("/pl/jobs/poszukiwany-informatyk-programista-do-wdrozenia-autorskiego-projektu-ai-praca-zd,143984/") == (
        "143984",
        "/pl/jobs/poszukiwany-informatyk-programista-do-wdrozenia-autorskiego-projektu-ai-praca-zd,143984/",
    )
    assert classify_job_href("https://useme.com/pl/jobs/foo,123/") == ("123", "/pl/jobs/foo,123/")
    assert classify_job_href("/pl/jobs/foo,123") == ("123", "/pl/jobs/foo,123")
    assert classify_job_href("https://useme.com/pl/jobs/foo,123/?utm_source=x") == ("123", "/pl/jobs/foo,123/")


def test_classify_job_href_rejects_category_and_navigation_links():
    for href in (
        "/pl/jobs/",
        "/pl/jobs/new/",
        "/pl/jobs/category/programowanie-i-it,35/",
        "/pl/jobs/category/programowanie-i-it,35/?order_by=-published_on",
        "https://useme.com/pl/jobs/category/multimedia,36/",
        "/pl/jobs/category/coding-and-it,35/software,100/",
        "/pl/jobs/foo/bar,123/",
        "",
        None,
        123,
    ):
        assert classify_job_href(href) is None, href


def test_parse_budget():
    assert parse_budget("50,00 PLN") == (50.0, "PLN")
    assert parse_budget("1 234,56 zł") == (1234.56, "PLN")
    assert parse_budget("2000,00 USD") == (2000.0, "USD")
    assert parse_budget("750,00 EUR") == (750.0, "EUR")
    assert parse_budget("1.000,00 PLN") == (1000.0, "PLN")
    assert parse_budget("1000 EUR") == (1000.0, "EUR")
    assert parse_budget("Do negocjacji") == (None, None)
    assert parse_budget("") == (None, None)
    assert parse_budget(None) == (None, None)


def test_parse_published_pl():
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert parse_published_pl("5 dni temu", now=now) == now - timedelta(days=5)
    assert parse_published_pl("przed chwilą", now=now) == now
    assert parse_published_pl("wczoraj", now=now) == now - timedelta(days=1)
    assert parse_published_pl("dziś", now=now) == now
    assert parse_published_pl("3 godziny temu", now=now) == now - timedelta(hours=3)
    assert parse_published_pl("10 minut temu", now=now) == now - timedelta(minutes=10)
    assert parse_published_pl("2 tygodnie temu", now=now) == now - timedelta(weeks=2)
    assert parse_published_pl("1 dzień temu", now=now) == now - timedelta(days=1)
    assert parse_published_pl("3 miesiące temu", now=now) is None
    assert parse_published_pl("rok temu", now=now) is None
    assert parse_published_pl("", now=now) is None
    assert parse_published_pl(None, now=now) is None


# ---------- wiele kategorii Useme (etap 5b, 2026-09-21) ----------


CAT_A = "/pl/jobs/category/programowanie-i-it,35"
CAT_B = "/pl/jobs/category/prace-biurowe,40/wprowadzanie-danych,132"
CAT_A_PAGE0 = f"https://useme.com{CAT_A}/?order_by=-published_on"
CAT_B_PAGE0 = f"https://useme.com{CAT_B}/?order_by=-published_on"


def multicat_session() -> FakeSession:
    return FakeSession(
        routes={
            CAT_A_PAGE0: FakeResponse(load("useme_listing_page1.html")),
            CAT_B_PAGE0: FakeResponse(load("useme_listing_page2.html")),
        }
    )


def test_production_category_paths_start_with_programming_and_add_office_lanes():
    """Stage 5b: the Polish board widens beyond „Programowanie i IT”."""
    assert PRODUCTION_CATEGORY_PATHS[0] == CAT_A
    assert CAT_B in PRODUCTION_CATEGORY_PATHS
    assert "/pl/jobs/category/prace-biurowe,40/customer-support,133" in PRODUCTION_CATEGORY_PATHS
    assert "/pl/jobs/category/prace-biurowe,40/bazy-danych,130" in PRODUCTION_CATEGORY_PATHS
    assert "/pl/jobs/category/marketing,37/analityka-internetowa,115" in PRODUCTION_CATEGORY_PATHS
    assert len(PRODUCTION_CATEGORY_PATHS) == len(set(PRODUCTION_CATEGORY_PATHS)) == 5


def test_adapter_default_stays_single_category():
    """The default lanes must not change: offline fixtures rely on it."""
    adapter = make_adapter(make_session())
    assert adapter.category_paths == (CAT_A,)
    assert adapter.category_path == CAT_A


def test_collects_every_category_and_tags_the_lead_with_its_lane():
    adapter = make_adapter(multicat_session(), category_paths=(CAT_A, CAT_B), max_pages=1)
    leads = adapter.collect()

    by_lane: dict[str, list[str]] = {}
    for lead in leads:
        by_lane.setdefault(lead.raw_data["category_path"], []).append(lead.source_id)
    assert set(by_lane) == {CAT_A, CAT_B}
    assert "143984" in by_lane[CAT_A]
    assert "143652" in by_lane[CAT_B]


def test_ids_are_deduplicated_across_categories():
    """The same offer in two lanes is collected once, tagged with the first lane."""
    session = FakeSession(
        routes={
            CAT_A_PAGE0: FakeResponse(load("useme_listing_page1.html")),
            CAT_B_PAGE0: FakeResponse(load("useme_listing_page1.html")),
        }
    )
    adapter = make_adapter(session, category_paths=(CAT_A, CAT_B), max_pages=1)
    leads = adapter.collect()

    assert len(leads) == len({lead.source_id for lead in leads})
    assert all(lead.raw_data["category_path"] == CAT_A for lead in leads)


def test_page_url_is_built_per_category():
    adapter = make_adapter(make_session(), category_paths=(CAT_A, CAT_B), max_pages=2)
    assert adapter._page_url(CAT_B, 0) == CAT_B_PAGE0
    assert adapter._page_url(CAT_B, 1) == CAT_B_PAGE0 + "&page=2"


def test_rejects_a_bad_path_in_the_category_list():
    with pytest.raises(ValueError):
        make_adapter(make_session(), category_paths=(CAT_A, "prace-biurowe,40"))


def test_rejects_an_empty_category_list():
    with pytest.raises(ValueError):
        make_adapter(make_session(), category_paths=())


def test_rejects_category_path_together_with_category_paths():
    with pytest.raises(ValueError):
        make_adapter(make_session(), category_path=CAT_A, category_paths=(CAT_A,))


def test_an_empty_category_page_does_not_stop_the_rest():
    """A lane with nothing new must not abort the other lanes."""
    session = FakeSession(
        routes={
            CAT_A_PAGE0: FakeResponse(load("useme_listing_page2.html")),
            CAT_B_PAGE0: FakeResponse(load("useme_listing_page1.html")),
        }
    )
    adapter = make_adapter(session, category_paths=(CAT_A, CAT_B), max_pages=1)
    leads = adapter.collect()
    assert {lead.raw_data["category_path"] for lead in leads} == {CAT_A, CAT_B}
