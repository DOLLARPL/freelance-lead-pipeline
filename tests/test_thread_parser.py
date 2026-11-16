"""Parser wklejonego watku z UseMe: przypadki, ktore psuly reczne kopiowanie."""

from __future__ import annotations

from freelance_pipeline.web.thread_parser import parse_thread

PAGE_DUMP = """UseMe
Oferty  Zlecenia  Freelancerzy  Jak to działa  Cennik
Moje konto  Wiadomości  Powiadomienia  Wyloguj
Strona główna > Wiadomości > Wątek
Marta Zielińska
Online
Zleceniodawca · zweryfikowany

Marta Zielińska  (09:12)
Dzień dobry,
potrzebuję przenieść sklep z Shoper na WooCommerce.

Pokaż więcej

Jan Noga  (09:26)
Dzień dobry, wycena 3600 zł netto, realizacja 5 dni.

Odpowiedz

Marta Zielińska  (09:41)
Czy w cenie jest płatności online?

Wszelkie prawa zastrzeżone © 2026 UseMe
Regulamin  Polityka prywatności  Pomoc  Kontakt
"""


def test_strona_useme_daje_trzy_wiadomosci_bez_smieci() -> None:
    messages = parse_thread(PAGE_DUMP)
    assert [m["kierunek"] for m in messages] == ["klient", "ja", "klient"]
    assert [m["czas"] for m in messages] == ["09:12", "09:26", "09:41"]
    joined = " ".join(m["tresc"] for m in messages)
    for noise in ("Pokaż więcej", "Odpowiedz", "Regulamin", "Wyloguj", "Zleceniodawca"):
        assert noise not in joined


def test_godzina_w_nawiasie_nie_zostaje_w_tresci() -> None:
    messages = parse_thread(PAGE_DUMP)
    assert "(09:12)" not in messages[0]["tresc"]
    assert messages[0]["tresc"].startswith("Dzień dobry,")


def test_naglowki_bez_godzin_i_powtorzone_imiona() -> None:
    text = (
        "Anna Kowalska\nIle kosztuje wizytówka?\n"
        "Jan Noga\n1200 zł, tydzień.\n"
        "Anna Kowalska\nOk, robimy.\n"
    )
    messages = parse_thread(text)
    assert [m["kierunek"] for m in messages] == ["klient", "ja", "klient"]
    assert messages[0]["tresc"] == "Ile kosztuje wizytówka?"


def test_dwa_naprzemienne_bloki_bez_naglowkow() -> None:
    text = (
        "Dzień dobry, potrzebuję landing page.\n\n"
        "Jaki budżet Pan przewiduje?\n\n"
        "Około 2000 zł.\n\n"
        "W tej kwocie zrobię stronę z formularzem.\n"
    )
    messages = parse_thread(text)
    assert [m["kierunek"] for m in messages] == ["klient", "ja", "klient", "ja"]


def test_smieci_nie_tworza_jednej_wielkiej_wiadomosci() -> None:
    text = " ".join(["To jest długie zdanie o wymaganiach klienta."] * 60)
    messages = parse_thread(text)
    assert len(messages) > 1
    assert all(len(m["tresc"]) <= 1200 for m in messages)


def test_pusty_tekst_i_same_menu() -> None:
    assert parse_thread("") == []
    assert parse_thread("   \n\n  ") == []
    assert parse_thread("Oferty\nZlecenia\nCennik\nPokaż więcej\n") == []


def test_etykieta_w_linii_imie_dwukropek() -> None:
    text = "Anna: Dzień dobry, ile to kosztuje?\nJa: 1500 zł.\nAnna: Ok, robimy."
    messages = parse_thread(text)
    assert [m["kierunek"] for m in messages] == ["klient", "ja", "klient"]
    assert messages[1]["tresc"] == "1500 zł."
