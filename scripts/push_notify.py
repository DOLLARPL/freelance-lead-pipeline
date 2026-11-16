"""Wysyla powiadomienie push z panelu snajpera. Uzywane przez crony i recznie.

Przyklady::

    # nowe oferty w kolejce (to samo, co robi cron po przebiegu kolekcji)
    .venv/bin/python scripts/push_notify.py --new-leads

    # wlasny tekst
    .venv/bin/python scripts/push_notify.py --title "Snajper" --body "3 nowe oferty" --url /

Skrypt czyta baze panelu i klucze VAPID, wiec dziala niezaleznie od tego, czy panel jest uruchomiony.
Gdy nie ma zadnej zapisanej subskrypcji, konczy sie kodem 0 i mowi o tym jednym zdaniem (cron nie ma
krzyczec o braku telefonu).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from freelance_pipeline.web import push as pushlib  # noqa: E402
from freelance_pipeline.web import store  # noqa: E402

DB = Path("/opt/data/cache/snajper-web/snajper.db")
VAPID_PRIVATE = Path("/opt/data/cache/snajper-web/vapid_private.pem")
VAPID_PUBLIC = Path("/opt/data/cache/snajper-web/vapid_public.txt")
SUBJECT = "mailto:jan@jnxautomation.pl"
#: Jeden raport = jedno powiadomienie (instant i rolling razem). Bez tego cron co 10 minut budzil telefon
#: tym samym "N ofert do decyzji", dopoki Jan nie oznaczyl wszystkiego.
REPORTS = Path("/opt/data/cache/snajper-v2/reports")
PUSH_MARKER = Path("/opt/data/cache/snajper-web/push_last_report.txt")


def _newest_report() -> tuple[Path | None, str]:
    files = sorted(REPORTS.glob("*.keys.json"), key=lambda item: item.stat().st_mtime)
    if not files:
        return None, ""
    newest = files[-1]
    return newest, f"{newest.name}:{int(newest.stat().st_mtime)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="push-notify", description=__doc__)
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument("--url", default="/sniper")
    parser.add_argument("--tag", default="snajper")
    parser.add_argument("--new-leads", action="store_true", help="tresc z aktualnej kolejki")
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args(argv)

    conn = store.connect(Path(args.db))
    try:
        title = args.title or "Snajper"
        body = args.body or ""
        report_stamp = ""
        if args.new_leads or not body:
            counts = store.counts(conn)
            queue = counts["queue"]
            if queue == 0:
                print("push: kolejka pusta, nic nie wysylam")
                return 0
            if args.new_leads:
                _, report_stamp = _newest_report()
                if report_stamp and PUSH_MARKER.is_file():
                    try:
                        seen = PUSH_MARKER.read_text(encoding="utf-8").strip()
                    except OSError:
                        seen = ""
                    if seen == report_stamp:
                        print("push: ten raport juz wyslany, pomijam")
                        return 0
            newest = store.list_leads(conn, view="queue", limit=1)
            top = newest[0].title if newest else ""
            body = f"{queue} ofert do decyzji" + (f", najnowsza: {top[:70]}" if top else "")
            title = args.title or "Snajper: nowe oferty"

        if not pushlib.subscriptions(conn):
            print("push: brak zapisanych urzadzen, pominieto")
            return 0

        result = pushlib.send(
            conn,
            private_path=VAPID_PRIVATE,
            subject=SUBJECT,
            title=title,
            body=body,
            url=args.url,
            tag=args.tag,
        )
        print(
            f"push: wyslane {result.sent}, bledy {result.failed}, usuniete {result.removed}"
            + (f" ({'; '.join(result.errors)})" if result.errors else "")
        )
        if report_stamp and result.failed == 0 and result.sent:
            try:
                PUSH_MARKER.write_text(report_stamp, encoding="utf-8")
            except OSError:
                pass  # brak wpisu tylko powtorzy push, nie gubi oferty
        return 0 if result.failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
