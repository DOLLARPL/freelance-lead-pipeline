"""Panel „skopiuj jednym kliknięciem” do wiadomości z raportu.

Użycie::

    cd /opt/data/projects/snajper-ofert
    uv run python scripts/copy_widget.py --dane plik.json --wyjscie /opt/data/assets/panel.html

Wejście: JSON z listą obiektów ``{tytul, url, wycena, wiadomosc}`` (kolejność = numeracja
w panelu). Wyjście: HTML do wyświetlenia w czacie przez ``::preview{file=...}``.

Dwie drogi kopiowania, w tej kolejności:

1. ``navigator.clipboard.writeText`` — jedno kliknięcie, jeśli środowisko pozwala (Electron
   zwykle pozwala),
2. fallback: przycisk wysyła ukryte polecenie ``skopiuj <numer>`` do agenta, a agent wkłada
   dokładnie ten tekst do schowka systemowego (``cua-mac clipboard write``). Wtedy zostaje
   jedno Cmd+V.

Dzięki temu ścieżka kopiowania **nie przechodzi przez renderowanie markdown** — to właśnie
renderowanie dokładało spacje i łamanie linii przy ręcznym kopiowaniu z podglądu.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_PAGE = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<title>Kopiuj wiadomości — Snajper</title>
<style>
  body {{ margin: 0; font-family: inherit; color: var(--foreground); background: transparent; }}
  .row {{ display: flex; align-items: flex-start; gap: 10px; padding: 8px 0;
          border-bottom: 1px solid var(--border); }}
  .row:last-child {{ border-bottom: none; }}
  .num {{ min-width: 1.4em; font-variant-numeric: tabular-nums; color: var(--muted-foreground); }}
  .info {{ flex: 1 1 auto; }}
  .title {{ display: block; }}
  .meta {{ display: block; font-size: 12px; color: var(--muted-foreground); margin-top: 2px; }}
  .meta a {{ color: var(--accent); }}
  button {{ font: inherit; padding: 5px 12px; border-radius: 8px; cursor: pointer;
            border: 1px solid var(--border); background: var(--card); color: var(--foreground); }}
  button:hover {{ border-color: var(--accent); }}
  button.done {{ border-color: var(--accent); color: var(--accent); }}
  button.failed {{ border-color: var(--border); color: var(--muted-foreground); }}
  #status {{ font-size: 12px; color: var(--muted-foreground); margin-top: 8px; min-height: 1em; }}
</style>
</head>
<body>
<div id="panel"></div>
<div id="status"></div>
<script id="payload" type="application/json">{payload}</script>
<script>
(function () {{
  var data = JSON.parse(document.getElementById('payload').textContent);
  var panel = document.getElementById('panel');
  var status = document.getElementById('status');

  function say(text) {{ status.textContent = text; }}

  function selectAll(node) {{
    var range = document.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }}

  function sendToAgent(index) {{
    if (window.hermes && window.hermes.send) {{
      window.hermes.send('skopiuj ' + index);
      return true;
    }}
    return false;
  }}

  data.items.forEach(function (item, position) {{
    var row = document.createElement('div');
    row.className = 'row';

    var num = document.createElement('div');
    num.className = 'num';
    num.textContent = String(position + 1);
    row.appendChild(num);

    var info = document.createElement('div');
    info.className = 'info';

    var title = document.createElement('span');
    title.className = 'title';
    title.textContent = item.tytul;
    info.appendChild(title);

    var meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = item.wycena;
    if (item.url) {{
      meta.appendChild(document.createTextNode(' · '));
      var link = document.createElement('a');
      link.href = item.url;
      link.textContent = 'ogłoszenie';
      link.target = '_blank';
      meta.appendChild(link);
    }}
    info.appendChild(meta);
    row.appendChild(info);

    var button = document.createElement('button');
    button.textContent = 'Kopiuj';
    button.addEventListener('click', function () {{
      var text = item.wiadomosc;
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).then(function () {{
          button.textContent = 'Skopiowane';
          button.className = 'done';
          say('Wiadomość ' + (position + 1) + ' jest w schowku — wklej Cmd+V.');
          setTimeout(function () {{ button.textContent = 'Kopiuj'; button.className = ''; }}, 2500);
        }}).catch(function () {{
          fallback();
        }});
        return;
      }}
      fallback();

      function fallback() {{
        if (sendToAgent(position + 1)) {{
          button.textContent = 'Kopiuję…';
          say('Wkładam wiadomość ' + (position + 1) + ' do schowka systemowego — po chwili Cmd+V.');
        }} else {{
          var area = document.createElement('textarea');
          area.value = text;
          area.setAttribute('readonly', 'readonly');
          document.body.appendChild(area);
          area.select();
          try {{
            document.execCommand('copy');
            button.textContent = 'Skopiowane';
            button.className = 'done';
            say('Wiadomość ' + (position + 1) + ' w schowku — wklej Cmd+V.');
          }} catch (error) {{
            selectAll(area);
            button.textContent = 'Cmd+C';
            button.className = 'failed';
            say('Zaznaczyłem treść — naciśnij Cmd+C i wklej.');
          }}
          document.body.removeChild(area);
        }}
      }}
    }});
    row.appendChild(button);
    panel.appendChild(row);
  }});
}})();
</script>
</body>
</html>
"""


def render(items: list[dict[str, str]]) -> str:
    payload = json.dumps({"items": items}, ensure_ascii=False)
    # JSON wewnątrz <script> nie może zawierać sekwencji zamykającej tag
    payload = payload.replace("</", "<\\/")
    return _PAGE.format(payload=payload)


def load_items(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items: list[dict[str, str]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SystemExit(f"pozycja {index + 1} w {path} nie jest obiektem")
        message = entry.get("wiadomosc") or entry.get("message") or ""
        if not message.strip():
            raise SystemExit(f"pozycja {index + 1} nie ma treści wiadomości")
        items.append(
            {
                "tytul": str(entry.get("tytul") or entry.get("title") or f"Oferta {index + 1}"),
                "url": str(entry.get("url") or ""),
                "wycena": str(entry.get("wycena") or ""),
                "wiadomosc": message,
            }
        )
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="copy_widget", description=__doc__)
    parser.add_argument("--dane", required=True, help="JSON z wiadomościami")
    parser.add_argument("--wyjscie", required=True, help="ścieżka HTML")
    args = parser.parse_args(argv)

    items = load_items(Path(args.dane))
    out = Path(args.wyjscie)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(items), encoding="utf-8")
    print(f"panel: {out} ({out.stat().st_size} B, {len(items)} wiadomości)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
