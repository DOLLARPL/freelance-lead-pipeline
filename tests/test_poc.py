"""Tests for the portfolio-gap backlog (what clients ask for and Jan cannot show)."""

from __future__ import annotations

import json
from pathlib import Path

from freelance_pipeline.models import JobLead
from freelance_pipeline.poc import (
    MAX_ENTRIES,
    load_backlog,
    normalize_gap,
    render_backlog,
    update_backlog,
)
from freelance_pipeline.scoring import ScoreBucket, ScoredLead


def make_lead(url: str, title: str = "Zlecenie") -> JobLead:
    return JobLead(source="useme", source_id=url, url=url, title=title)


def scored(url: str, gap: str | None, title: str = "Zlecenie") -> ScoredLead:
    return ScoredLead(make_lead(url, title), 80, ScoreBucket.TOP, "pasuje", gap)


def test_normalize_gap_is_a_stable_key():
    assert normalize_gap("  Brak case study: SAP ↔ BaseLinker! ") == normalize_gap(
        "brak case study sap baselinker"
    )


def test_first_observation_creates_an_entry(tmp_path: Path):
    path = tmp_path / "poc.json"
    entries = update_backlog(path, [scored("https://x/1", "brak integracji ERP")])
    assert len(entries) == 1
    assert entries[0].count == 1
    assert entries[0].urls == ("https://x/1",)
    assert load_backlog(path)[0].gap == "brak integracji ERP"


def test_second_offer_increments_the_same_entry(tmp_path: Path):
    path = tmp_path / "poc.json"
    update_backlog(path, [scored("https://x/1", "brak integracji ERP")])
    entries = update_backlog(path, [scored("https://x/2", "Brak integracji ERP")])
    assert len(entries) == 1
    assert entries[0].count == 2
    assert entries[0].urls == ("https://x/1", "https://x/2")


def test_the_same_offer_twice_does_not_inflate_the_count(tmp_path: Path):
    """Powtórny przebieg na tych samych ofertach nie może udawać dwóch klientów."""
    path = tmp_path / "poc.json"
    update_backlog(path, [scored("https://x/1", "brak ERP")])
    entries = update_backlog(path, [scored("https://x/1", "brak ERP")])
    assert entries[0].count == 1


def test_gap_is_optional_and_never_creates_noise(tmp_path: Path):
    path = tmp_path / "poc.json"
    entries = update_backlog(path, [scored("https://x/1", None), scored("https://x/2", "   ")])
    assert entries == []
    assert not path.exists()


def test_backlog_survives_a_corrupt_file(tmp_path: Path):
    path = tmp_path / "poc.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_backlog(path) == []
    assert len(update_backlog(path, [scored("https://x/1", "luka")])) == 1


def test_backlog_is_capped(tmp_path: Path):
    path = tmp_path / "poc.json"
    leads = [scored(f"https://x/{index}", f"luka {index}") for index in range(MAX_ENTRIES + 5)]
    entries = update_backlog(path, leads)
    assert len(entries) == MAX_ENTRIES


def test_backlog_file_is_valid_json(tmp_path: Path):
    path = tmp_path / "poc.json"
    update_backlog(path, [scored("https://x/1", "luka")])
    assert isinstance(json.loads(path.read_text(encoding="utf-8"))["entries"], list)


def test_rejected_offers_never_shape_the_backlog(tmp_path: Path):
    """Luka z ogłoszenia, które Jan odrzuca (księgowość, etat), nie jest sygnałem."""
    path = tmp_path / "poc.json"
    rejected = ScoredLead(make_lead("https://x/1"), 0, ScoreBucket.ODRZUC, "odrzucone", "księgowość")
    entries = update_backlog(path, [rejected])
    assert entries == []
    assert not path.exists()


def test_render_hides_single_observations_by_default(tmp_path: Path):
    path = tmp_path / "poc.json"
    update_backlog(path, [scored("https://x/1", "rzadka luka")])
    rendered = render_backlog(load_backlog(path))
    assert "rzadka luka" not in rendered
    assert "rzadka luka" in render_backlog(load_backlog(path), min_count=1)
