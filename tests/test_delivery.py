"""Offline tests for delivery sinks: file/console success contract and the
configurable-but-never-sending Telegram stub."""

from __future__ import annotations

from pathlib import Path

import pytest

from freelance_pipeline.delivery import (
    DeliveryError,
    ReportDelivery,
    TelegramConfigError,
    TelegramDelivery,
)


def test_report_delivery_accepts_text_and_returns_none(tmp_path: Path):
    path = tmp_path / "report.txt"
    delivery = ReportDelivery(path=str(path))
    assert delivery.send_report("hello\nworld") is None
    assert path.read_text() == "hello\nworld"


def test_report_delivery_overwrites_on_second_send(tmp_path: Path):
    path = tmp_path / "report.txt"
    delivery = ReportDelivery(path=str(path))
    delivery.send_report("first")
    delivery.send_report("second")
    assert path.read_text() == "second"


def test_report_delivery_unwritable_path_raises_delivery_error(tmp_path: Path):
    (tmp_path / "blocked.txt").write_text("i am a file")
    delivery = ReportDelivery(path=str(tmp_path / "blocked.txt" / "report.txt"))
    with pytest.raises(DeliveryError):
        delivery.send_report("x")


def test_report_delivery_dash_writes_to_stdout(capsys):
    ReportDelivery(path="-").send_report("na ekran")
    assert "na ekran" in capsys.readouterr().out


def test_telegram_delivery_send_is_always_disabled():
    delivery = TelegramDelivery(token="t", chat_id="123456789")
    with pytest.raises(DeliveryError, match="disabled"):
        delivery.send_report("never sent")
    assert delivery.messages_sent == 0


def test_telegram_delivery_validate_accepts_wellformed_config():
    delivery = TelegramDelivery(token="tok", chat_id="-1001234567890")
    assert delivery.validate() is None


def test_telegram_delivery_validate_rejects_missing_parts():
    with pytest.raises(TelegramConfigError):
        TelegramDelivery(token="   ", chat_id="12").validate()
    with pytest.raises(TelegramConfigError):
        TelegramDelivery(token="tok", chat_id="abc").validate()
    with pytest.raises(TelegramConfigError):
        TelegramDelivery(token="tok", chat_id="").validate()


def test_telegram_delivery_never_holds_transport():
    """No network client/session is ever constructed by the Telegram stub."""
    delivery = TelegramDelivery(token="t", chat_id="1")
    assert getattr(delivery, "session", None) is None
    assert getattr(delivery, "http", None) is None
    assert getattr(delivery, "client", None) is None