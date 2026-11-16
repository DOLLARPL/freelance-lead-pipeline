r"""Regression: the legacy broad selector+regex also matched category URLs.

The preserved baseline (``legacy/useme_scraper.py``) collects every anchor
matching ``a[href*='/pl/jobs/']`` and extracts an id with
``re.search(r'/pl/jobs/[^,]+,(\d+)/', href)``. Because ``re.search`` is not
anchored, that pattern also matches category and navigation URLs such as
``/pl/jobs/category/programowanie-i-it,35/`` and records the *category* id
(``35``) as if it were a job id.

These tests replay that exact logic inlined here (so the legacy module, whose
import would touch ``/opt/data``, is never imported or executed) against the
same fixture, proving:

* the fixture genuinely demonstrates the legacy false-positive;
* the new adapter still accepts every offer link the legacy logic accepted
  (parity), and
* the new adapter never emits a category/navigation link as a lead.
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup

from freelance_pipeline.sources.useme import UsemeJobs
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
PAGE0 = "https://useme.com/pl/jobs/category/programowanie-i-it,35/?order_by=-published_on"

LEGACY_SELECTOR = "a[href*='/pl/jobs/']"
LEGACY_REGEX = re.compile(r"/pl/jobs/[^,]+,(\d+)/")


def legacy_job_ids(html: str) -> set[str]:
    """Replicate legacy ``scrape_page`` URL classification (and title filter)."""
    soup = BeautifulSoup(html, "lxml")
    ids: set[str] = set()
    for a in soup.select(LEGACY_SELECTOR):
        href = a.get("href", "") or ""
        match = LEGACY_REGEX.search(href)
        if not match:
            continue
        title = (a.get("title") or a.get_text(strip=True)).strip()
        if not title or len(title) < 8:
            continue
        ids.add(match.group(1))
    return ids


def legacy_category_ids(html: str) -> set[str]:
    """Ids the legacy regex would capture from category links alone."""
    soup = BeautifulSoup(html, "lxml")
    ids: set[str] = set()
    for a in soup.select(LEGACY_SELECTOR):
        href = a.get("href", "") or ""
        if "/category/" not in href:
            continue
        match = LEGACY_REGEX.search(href)
        if match:
            ids.add(match.group(1))
    return ids


def page1_html() -> str:
    return (FIXTURES / "useme_listing_page1.html").read_text(encoding="utf-8")


def test_fixture_genuinely_demonstrates_legacy_category_bug():
    html = page1_html()
    category_ids = legacy_category_ids(html)
    assert "35" in category_ids
    assert "34" in category_ids
    assert legacy_job_ids(html) >= {"143984", "143981", "143733"}


def test_legacy_logic_would_have_emitted_category_ids_as_jobs():
    html = page1_html()
    legacy = legacy_job_ids(html)
    assert "35" in legacy
    assert "34" in legacy
    assert "36" in legacy


def test_new_adapter_matches_legacy_on_real_offer_links():
    html = page1_html()
    session = FakeSession(routes={PAGE0: FakeResponse(html)})
    leads = UsemeJobs(session=session, max_pages=1).collect()
    adapter_ids = {lead.source_id for lead in leads}
    legacy = legacy_job_ids(html)
    assert adapter_ids == {"143984", "143981", "143733"}
    assert adapter_ids <= legacy


def test_new_adapter_excludes_all_category_ids():
    html = page1_html()
    session = FakeSession(routes={PAGE0: FakeResponse(html)})
    leads = UsemeJobs(session=session, max_pages=1).collect()
    adapter_ids = {lead.source_id for lead in leads}
    category_ids = legacy_category_ids(html)
    assert adapter_ids.isdisjoint(category_ids)
    assert all("/category/" not in lead.url for lead in leads)


def test_absolute_category_link_is_not_a_lead():
    html = page1_html()
    assert "https://useme.com/pl/jobs/category/multimedia,36/" in html
    session = FakeSession(routes={PAGE0: FakeResponse(html)})
    leads = UsemeJobs(session=session, max_pages=1).collect()
    assert all(lead.source_id != "36" for lead in leads)