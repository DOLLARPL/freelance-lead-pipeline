"""Rolling lista na cronie: warstwa dostawy nie może zniknąć po cichu.

Awaria 2026-10-07..09: `run_rolling.sh` uruchamiał `rolling_report.py` bez `--send`,
więc lista powstawała w plikach i kończyła się pustym stdout — cron raportował
„silent (empty output)”, a Jan nie dostawał nic przez trzy dni i w rejestrze nie było
ani jednego statusu `seen`. Te testy pilnują, że skrypt produkcyjny nadal prosi o wysyłkę
(i o wersję do czytania na telefonie), a tryb próbny nic nie wysyła.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN_ROLLING = REPO / "scripts" / "run_rolling.sh"
REPORT = REPO / "scripts" / "rolling_report.py"


def run_rolling_text() -> str:
    return RUN_ROLLING.read_text(encoding="utf-8")


def test_run_rolling_asks_for_delivery():
    text = run_rolling_text()
    assert "rolling_report.py" in text
    assert "--send" in text, "bez --send lista nie jedzie na Telegram"
    assert "--docs" in text, "wersja do czytania na telefonie ma być domyślnie"


def test_run_rolling_keeps_statuses_and_send_inside_the_report_script():
    """Shim nie oznacza nic sam: `new -> seen` dzieje się po potwierdzonej wysyłce."""
    text = run_rolling_text()
    assert "ledger" not in text.lower()
    report = REPORT.read_text(encoding="utf-8")
    assert "mark_delivered" in report
    # Kolejność wywołań: wysyłka, dopiero potem oznaczenie statusów.
    assert report.index("send_document(target") < report.index("mark_delivered([entry.key")


def test_the_report_script_still_sends_even_when_one_price_fails():
    """Wycena jednej oferty nie może wywalać całej listy (awaria 2026-10-09)."""
    report = REPORT.read_text(encoding="utf-8")
    assert "price_best_effort" in report
