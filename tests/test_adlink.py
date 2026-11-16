"""Prefiks z linkiem do ogłoszenia w draftach facebookowych."""

from __future__ import annotations

from freelance_pipeline.adlink import AD_REFERENCE_PREFIX, with_ad_reference

URL = "https://www.facebook.com/groups/418363537774333/permalink/940268618917153/"


def test_facebook_draft_dostaje_link_na_poczatku() -> None:
    out = with_ad_reference("Cześć,\n\nOferty wystawiasz sam.", "facebook_groups", URL)
    assert out == f"{AD_REFERENCE_PREFIX} {URL}\n\nCześć,\n\nOferty wystawiasz sam."


def test_idempotentne() -> None:
    once = with_ad_reference("Cześć", "facebook_groups", URL)
    assert with_ad_reference(once, "facebook_groups", URL) == once


def test_inne_zrodla_bez_zmian() -> None:
    assert with_ad_reference("Cześć", "useme", URL) == "Cześć"
    assert with_ad_reference("Cześć", "oferteo", URL) == "Cześć"


def test_brak_url_albo_pusty_draft() -> None:
    assert with_ad_reference("Cześć", "facebook_groups", "") == "Cześć"
    assert with_ad_reference("", "facebook_groups", URL) == ""


def test_zachowuje_wiodace_biale_znaki_bez_podwojnego_prefiksu() -> None:
    out = with_ad_reference("\n Cześć", "facebook_groups", URL)
    assert out.startswith(f"{AD_REFERENCE_PREFIX} {URL}\n\nCześć")
