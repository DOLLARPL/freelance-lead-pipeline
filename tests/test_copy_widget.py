"""Panel kopiowania (scripts/copy_widget.py): payload, escapowanie, komplet pozycji."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "copy_widget.py"
PAYLOAD_RE = re.compile(r'<script id="payload" type="application/json">(.*?)</script>', re.DOTALL)

MESSAGE = (
    "Cześć,\n\nkrótka wiadomość testowa z linkiem https://jnxautomation.pl/projekty/snajper-ofert\n\n"
    "Jan Noga\njnxautomation.pl\n+48 600 000 000"
)


def _run(tmp_path: Path, items: list[dict[str, str]]) -> Path:
    data = tmp_path / "dane.json"
    data.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "panel.html"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dane", str(data), "--wyjscie", str(out)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out


def _items(tmp_path: Path, count: int = 2) -> list[dict[str, str]]:
    return [
        {
            "tytul": f"Zlecenie {index + 1}",
            "url": f"https://useme.com/pl/jobs/x,14547{index}/",
            "wycena": "1 800 zł · 3 dni · ~100 zł/h",
            "wiadomosc": MESSAGE,
        }
        for index in range(count)
    ]


def test_panel_contains_every_message_in_the_payload(tmp_path: Path):
    out = _run(tmp_path, _items(tmp_path, 3))
    payload = json.loads(PAYLOAD_RE.search(out.read_text(encoding="utf-8")).group(1))
    assert len(payload["items"]) == 3
    assert payload["items"][0]["wiadomosc"] == MESSAGE
    assert payload["items"][0]["url"].startswith("https://useme.com/pl/jobs/")


def test_messages_are_not_touched_by_html_escaping(tmp_path: Path):
    tricky = MESSAGE + "\n\n<tag> & \"cudzysłów\" i 'apostrof'"
    out = _run(tmp_path, [{"tytul": "T", "wiadomosc": tricky}])
    payload = json.loads(PAYLOAD_RE.search(out.read_text(encoding="utf-8")).group(1))
    assert payload["items"][0]["wiadomosc"] == tricky


def test_closing_tag_inside_a_message_cannot_break_the_payload(tmp_path: Path):
    out = _run(tmp_path, [{"tytul": "T", "wiadomosc": MESSAGE + "\n</script> uwaga"}])
    payload = json.loads(PAYLOAD_RE.search(out.read_text(encoding="utf-8")).group(1))
    assert payload["items"][0]["wiadomosc"].endswith("</script> uwaga")
    assert out.read_text(encoding="utf-8").count("</script>") == 2


def test_panel_has_a_button_per_message_and_both_copy_paths(tmp_path: Path):
    html_text = _run(tmp_path, _items(tmp_path, 2)).read_text(encoding="utf-8")
    assert "navigator.clipboard.writeText" in html_text
    assert "window.hermes.send('skopiuj '" in html_text
    assert "document.execCommand('copy')" in html_text
    assert html_text.count("createElement('button')") == 1  # jeden konstruktor w pętli


def test_panel_uses_the_app_theme_variables(tmp_path: Path):
    html_text = _run(tmp_path, _items(tmp_path, 1)).read_text(encoding="utf-8")
    for variable in ("var(--foreground)", "var(--muted-foreground)", "var(--border)", "var(--accent)"):
        assert variable in html_text
    assert "background: var(--card)" in html_text


def test_missing_message_is_reported(tmp_path: Path):
    data = tmp_path / "dane.json"
    data.write_text(json.dumps([{"tytul": "T", "wiadomosc": "   "}]), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dane", str(data), "--wyjscie", str(tmp_path / "p.html")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "treści wiadomości" in result.stderr


def test_non_object_entry_is_reported(tmp_path: Path):
    data = tmp_path / "dane.json"
    data.write_text(json.dumps(["useme:1"]), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dane", str(data), "--wyjscie", str(tmp_path / "p.html")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "nie jest obiektem" in result.stderr
