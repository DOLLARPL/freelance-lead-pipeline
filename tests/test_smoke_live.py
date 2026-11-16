"""Offline regression tests for the live smoke script's acceptance checks."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from freelance_pipeline.models import JobLead
from scripts.smoke_live import (
    CollectTimeoutError,
    collect_with_timeout,
    validate_leads,
    validate_nofluffjobs_enrichment,
    validate_rocketjobs_enrichment,
)


def make_lead(**overrides):
    base = {
        "source": "demo",
        "source_id": "1",
        "url": "https://example.com/t/1",
        "title": "Demo lead",
    }
    base.update(overrides)
    return JobLead(**base)


def make_rocketjobs_lead(**overrides):
    base = {
        "source": "rocketjobs",
        "source_id": "rocketjobs-slug",
        "url": "https://rocketjobs.pl/oferta-pracy/rocketjobs-slug",
        "title": "RocketJobs offer",
        "company_or_author": "Acme",
        "work_mode": "Zdalnie",
        "location": "Warszawa",
        "skills": ("python",),
        "budget": 8000.0,
        "currency": "PLN",
        "raw_data": None,
    }
    base.update(overrides)
    return JobLead(**base)


def make_nofluffjobs_lead(**overrides):
    base = {
        "source": "nofluffjobs",
        "source_id": "seeking-job-slug",
        "url": "https://nofluffjobs.com/job/seeking-job-slug",
        "title": "No Fluff offer",
        "company_or_author": "Acme",
        "skills": ("Python",),
        "budget": 18000.0,
        "currency": "PLN",
        "raw_data": None,
    }
    base.update(overrides)
    return JobLead(**base)


def make_bad_lead(**overrides):
    """A duck-typed lead that would violate JobLead's own validation."""
    base = {
        "source": "demo",
        "source_id": "bad",
        "url": "https://example.com/t/bad",
        "title": "Bad lead",
        "raw_data": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_clean_leads_produce_no_errors():
    assert validate_leads([make_lead(), make_lead(source_id="2")]) == []


def test_duplicate_source_ids_are_reported():
    errors = validate_leads([make_lead(), make_lead(source_id="1", title="dup")])
    assert any("duplicate" in error and "1" in error for error in errors)


def test_invalid_required_fields_are_reported():
    errors = validate_leads(
        [
            make_bad_lead(url="   "),
            make_bad_lead(source_id=""),
            make_bad_lead(source=""),
        ]
    )
    assert any("url" in error for error in errors)
    assert any("source_id" in error for error in errors)
    assert any("source" in error for error in errors)


def test_pinned_metadata_topic_is_reported():
    lead = make_lead(raw_data={"pinned": True, "closed": False, "archived": False})
    assert any("pinned" in error for error in validate_leads([lead]))


def test_closed_topic_is_reported():
    lead = make_lead(raw_data={"pinned": False, "closed": True, "archived": False})
    assert any("closed" in error for error in validate_leads([lead]))


def test_archived_topic_is_reported():
    lead = make_lead(raw_data={"pinned": False, "closed": False, "archived": True})
    assert any("archived" in error for error in validate_leads([lead]))


def test_about_category_title_is_reported():
    lead = make_lead(title="About the Demo category", raw_data=None)
    assert any("about" in error for error in validate_leads([lead]))


def test_useme_category_url_is_reported():
    lead = make_lead(
        source="useme",
        source_id="35",
        url="https://useme.com/pl/jobs/category/programowanie-i-it,35/",
    )
    errors = validate_leads([lead])
    assert any("category" in error for error in errors)


def test_useme_non_numeric_source_id_is_reported():
    lead = make_lead(source="useme", source_id="abc", url="https://useme.com/pl/jobs/abc,1/")
    errors = validate_leads([lead])
    assert any("numeric" in error for error in errors)


def test_useme_category_url_is_reported_even_with_valid_source_id():
    lead = make_lead(
        source="useme",
        source_id="143984",
        url="https://useme.com/pl/jobs/category/programowanie-i-it,35/",
    )
    errors = validate_leads([lead])
    assert any("category" in error for error in errors)


def test_justjoinit_non_job_offer_url_is_reported():
    lead = make_lead(
        source="justjoinit",
        source_id="04d217ac-6753-4ba3-a6b8-9f1e44250ea3",
        url="https://justjoin.it/job-offers/all-locations",
    )
    assert any("job-offer" in error for error in validate_leads([lead]))


def test_justjoinit_hidden_or_expired_offer_is_reported():
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    hidden = make_lead(
        source="justjoinit",
        source_id="hidden",
        url="https://justjoin.it/job-offer/hidden",
        raw_data={"displayOffer": False},
    )
    expired = make_lead(
        source="justjoinit",
        source_id="expired",
        url="https://justjoin.it/job-offer/expired",
        raw_data={"displayOffer": True, "expiredAt": expired_at},
    )
    errors = validate_leads([hidden, expired])
    assert any("hidden" in error for error in errors)
    assert any("expired" in error for error in errors)


def test_rocketjobs_category_url_is_reported():
    lead = make_lead(
        source="rocketjobs",
        source_id="sales",
        url="https://rocketjobs.pl/oferty-pracy/wszystkie-lokalizacje/sales",
    )
    assert any("job offer" in error for error in validate_leads([lead]))


def test_rocketjobs_api_url_is_reported():
    lead = make_lead(
        source="rocketjobs",
        source_id="api",
        url="https://rocketjobs.pl/api/candidate-api",
    )
    assert any("api" in error for error in validate_leads([lead]))


def test_rocketjobs_source_id_must_match_offer_slug():
    lead = make_lead(
        source="rocketjobs",
        source_id="different-slug",
        url="https://rocketjobs.pl/oferta-pracy/real-offer-slug",
    )
    errors = validate_leads([lead])
    assert any("slug" in error for error in errors)


def test_rocketjobs_enrichment_clean_batch_passes():
    leads = [make_rocketjobs_lead(source_id=f"slug-{i}") for i in range(4)]
    assert validate_rocketjobs_enrichment(leads) == []


def test_rocketjobs_enrichment_empty_batch_is_not_a_drift_failure():
    assert validate_rocketjobs_enrichment([]) == []


def test_rocketjobs_enrichment_fails_when_field_below_threshold():
    leads = []
    for i in range(4):
        company = "Acme" if i < 1 else None
        leads.append(make_rocketjobs_lead(source_id=f"slug-{i}", company_or_author=company))
    errors = validate_rocketjobs_enrichment(leads)
    assert any("company_or_author" in error for error in errors)


def test_rocketjobs_enrichment_at_threshold_passes():
    leads = []
    for i in range(4):
        company = "Acme" if i < 2 else None
        leads.append(make_rocketjobs_lead(source_id=f"slug-{i}", company_or_author=company))
    assert validate_rocketjobs_enrichment(leads) == []


def test_rocketjobs_enrichment_message_names_field_and_ratio():
    leads = []
    for i in range(4):
        skills = ("python",) if i < 1 else ()
        leads.append(make_rocketjobs_lead(source_id=f"slug-{i}", skills=skills))
    errors = validate_rocketjobs_enrichment(leads)
    assert any("skills" in error and "50%" in error for error in errors)


def test_rocketjobs_enrichment_requires_at_least_one_budget_currency_pair():
    leads = [
        make_rocketjobs_lead(source_id=f"slug-{i}", budget=None, currency=None) for i in range(4)
    ]
    errors = validate_rocketjobs_enrichment(leads)
    assert any("budget" in error and "currency" in error for error in errors)


def test_validate_leads_empty_list_returns_no_errors():
    assert validate_leads([]) == []


def test_nofluffjobs_bad_url_is_reported_even_when_not_last():
    good = make_nofluffjobs_lead(
        source_id="slug-1", url="https://nofluffjobs.com/job/slug-1"
    )
    bad = make_nofluffjobs_lead(
        source_id="backend", url="https://nofluffjobs.com/backend"
    )
    errors = validate_leads([good, bad, good])
    assert any("job offer" in error for error in errors)


def test_nofluffjobs_bad_url_is_reported_even_when_last():
    lead = make_lead(
        source="nofluffjobs",
        source_id="backend",
        url="https://nofluffjobs.com/backend",
    )
    assert any("job offer" in error for error in validate_leads([lead]))


def test_nofluffjobs_disallowed_path_url_is_reported():
    lead = make_lead(
        source="nofluffjobs",
        source_id="api",
        url="https://nofluffjobs.com/api/postings",
    )
    errors = validate_leads([lead])
    assert any("disallowed" in error for error in errors)


def test_nofluffjobs_source_id_must_match_offer_slug():
    lead = make_lead(
        source="nofluffjobs",
        source_id="different-slug",
        url="https://nofluffjobs.com/job/real-offer-slug",
    )
    errors = validate_leads([lead])
    assert any("slug" in error for error in errors)


def test_nofluffjobs_enrichment_clean_batch_passes():
    leads = [make_nofluffjobs_lead(source_id=f"slug-{i}") for i in range(4)]
    assert validate_nofluffjobs_enrichment(leads) == []


def test_nofluffjobs_enrichment_empty_batch_is_not_a_drift_failure():
    assert validate_nofluffjobs_enrichment([]) == []


def test_nofluffjobs_enrichment_fails_when_field_below_threshold():
    leads = []
    for i in range(4):
        company = "Acme" if i < 1 else None
        leads.append(make_nofluffjobs_lead(source_id=f"slug-{i}", company_or_author=company))
    errors = validate_nofluffjobs_enrichment(leads)
    assert any("company_or_author" in error for error in errors)


def test_nofluffjobs_enrichment_requires_at_least_one_budget_currency_pair():
    leads = [
        make_nofluffjobs_lead(source_id=f"slug-{i}", budget=None, currency=None) for i in range(3)
    ]
    errors = validate_nofluffjobs_enrichment(leads)
    assert any("budget" in error and "currency" in error for error in errors)


def test_nofluffjobs_drift_batch_fails_validate_leads():
    leads = [
        make_nofluffjobs_lead(
            source_id=f"slug-{i}",
            url=f"https://nofluffjobs.com/job/slug-{i}",
            company_or_author=None,
            skills=(),
            budget=None,
            currency=None,
        )
        for i in range(4)
    ]
    errors = validate_leads(leads)
    assert any("enrichment" in error for error in errors)
    assert any("budget" in error and "currency" in error for error in errors)


def test_nofluffjobs_clean_batch_passes_validate_leads():
    leads = [
        make_nofluffjobs_lead(
            source_id=f"slug-{i}", url=f"https://nofluffjobs.com/job/slug-{i}"
        )
        for i in range(4)
    ]
    assert validate_leads(leads) == []


def test_rocketjobs_selector_drift_batch_fails_validate_leads():
    leads = [
        make_rocketjobs_lead(
            source_id=f"slug-{i}",
            url=f"https://rocketjobs.pl/oferta-pracy/slug-{i}",
            company_or_author=None,
            work_mode=None,
            location=None,
            skills=(),
            budget=None,
            currency=None,
        )
        for i in range(4)
    ]
    errors = validate_leads(leads)
    assert any("enrichment" in error for error in errors)
    assert any("budget" in error and "currency" in error for error in errors)


def test_rocketjobs_clean_batch_passes_validate_leads():
    leads = [
        make_rocketjobs_lead(
            source_id=f"slug-{i}", url=f"https://rocketjobs.pl/oferta-pracy/slug-{i}"
        )
        for i in range(4)
    ]
    assert validate_leads(leads) == []


def test_collect_with_timeout_returns_leads():
    class Fast:
        source = "fast"

        def collect(self):
            return [make_lead()]

    result = collect_with_timeout(Fast(), timeout=1.0)
    assert [lead.source_id for lead in result] == ["1"]


def test_collect_with_timeout_re_raises_source_error():
    class Broken:
        source = "broken"

        def collect(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        collect_with_timeout(Broken(), timeout=1.0)


def test_collect_with_timeout_raises_on_slow_source():
    class Slow:
        source = "slow"

        def collect(self):
            time.sleep(0.5)
            return [make_lead()]

    with pytest.raises(CollectTimeoutError):
        collect_with_timeout(Slow(), timeout=0.05)