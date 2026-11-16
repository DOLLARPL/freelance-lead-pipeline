"""Trasy panelu: pelne strony i fragmenty HTMX."""

from __future__ import annotations

import html
import json
import os
from urllib.parse import quote
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from freelance_pipeline.adlink import with_ad_reference
from freelance_pipeline import styl
from freelance_pipeline.cennik import load_cennik
from freelance_pipeline.ledger import Ledger

from . import fb as fblib
from . import mailer
from . import push as pushlib
from . import store
from .auth import check_origin, current_user, require_owner
from .thread_parser import parse_thread

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
router = APIRouter()

#: Domyslne okno wieku w panelu. Zmiana bez deployu: SNAJPER_MAX_AGE_HOURS.
DEFAULT_MAX_AGE_HOURS = 12.0


def _effective_max_age_hours(requested: float | None) -> float:
    """Prog wieku z formularza, a gdy go brak — ze srodowiska.

    Zero i wartosci ujemne znacza "pokaz wszystko": stary link bez parametru
    nie moze nagle ukryc calej kolejki.
    """
    if requested is not None:
        return max(0.0, float(requested))
    raw = os.environ.get("SNAJPER_MAX_AGE_HOURS", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MAX_AGE_HOURS
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
#: Login biezacego zadania w kazdym szablonie (naglowek z wylogowaniem).
templates.env.globals["current_user"] = current_user


# --------------------------------------------------------------------------- filtry jinja


def _money(value: Any) -> str:
    if value is None:
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{amount:,.0f}".replace(",", " ") + " zł"


def _date(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _stamp(value: Any) -> str:
    """Data i godzina z minutami, w czasie warszawskim. Do czasu zeskrejprowania."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:16]
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y %H:%M")


def _clean(value: Any) -> str:
    """Tytuly z portali przychodza z markdownem (``**``, backticki). Do widoku bez znacznikow."""
    import re as _re

    text = str(value or "")
    text = _re.sub(r"[*_`#>]+", "", text)
    return _re.sub(r"\s+", " ", text).strip()


def _age(value: Any) -> str:
    from datetime import UTC, datetime

    text = str(value or "")
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = (datetime.now(UTC) - stamp).days
    if days <= 0:
        return "dziś"
    if days == 1:
        return "1 dzień"
    return f"{days} dni"


templates.env.filters["money"] = _money
templates.env.filters["date"] = _date
templates.env.filters["stamp"] = _stamp
templates.env.filters["age"] = _age
templates.env.filters["clean"] = _clean

SOURCE_NAMES = {
    "useme": "Useme",
    "facebook_groups": "Facebook",
    "nofluffjobs": "NoFluffJobs",
    "justjoinit": "JustJoinIT",
    "rocketjobs": "RocketJobs",
}


def _source_name(value: Any) -> str:
    key = str(value or "")
    return SOURCE_NAMES.get(key, key.replace("_", " ").capitalize() or "brak")


def _source_class(value: Any) -> str:
    key = str(value or "")
    return {
        "useme": "src-useme",
        "facebook_groups": "src-facebook",
    }.get(key, "src-other")


def _link_zrodel(request: Request, zrodla) -> str:
    """Adres listy z innym zestawem zrodel: chipy przelaczaja sie linkiem.

    Puste ``zrodla`` znaczy "wszystkie", wiec odznaczenie ostatniego chipa wraca
    do pelnej listy, a nie do pustej tabeli.
    """
    q = request.query_params
    czesci = [f"view={quote(q.get('view') or 'queue')}"]
    tekst = (q.get("q") or "").strip()
    if tekst:
        czesci.append(f"q={quote(tekst)}")
    for s in sorted({str(x) for x in zrodla if x}):
        czesci.append(f"sources={quote(s)}")
    return "/?" + "&".join(czesci)


templates.env.globals["link_zrodel"] = _link_zrodel
templates.env.filters["source_name"] = _source_name
templates.env.filters["source_class"] = _source_class


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _conn(request: Request):
    return request.app.state.conn


def _ledger(request: Request) -> Ledger:
    return Ledger(request.app.state.settings.ledger_path)


def _nav_counts(request: Request) -> dict[str, int]:
    """Liczniki do raila: te same progi co lista kolejki, zeby badge nie klamal."""
    conn = _conn(request)
    counts = store.counts(conn)
    counts["queue"] = store.counts(conn, max_age_hours=_effective_max_age_hours(None))["queue"]
    return counts


# --------------------------------------------------------------------------- strony


@router.get("/", response_class=HTMLResponse)
def queue_page(
    request: Request,
    owner: str = Depends(require_owner),
    view: str = Query("queue"),
    bucket: str | None = Query(None),
    source: str | None = Query(None),
    sources: list[str] = Query(default=[]),
    q: str | None = Query(None),
    min_score: int | None = Query(None),
    sort: str = Query("newest"),
    max_age_hours: float | None = Query(None),
) -> HTMLResponse:
    ctx = _queue_context(request, view, bucket, source, q, min_score, sort, max_age_hours, owner, sources)
    return templates.TemplateResponse(request, "queue.html", ctx)


@router.get("/leads", response_class=HTMLResponse)
def leads_fragment(
    request: Request,
    owner: str = Depends(require_owner),
    view: str = Query("queue"),
    bucket: str | None = Query(None),
    source: str | None = Query(None),
    sources: list[str] = Query(default=[]),
    q: str | None = Query(None),
    min_score: int | None = Query(None),
    sort: str = Query("newest"),
    max_age_hours: float | None = Query(None),
) -> HTMLResponse:
    ctx = _queue_context(request, view, bucket, source, q, min_score, sort, max_age_hours, owner, sources)
    return templates.TemplateResponse(request, "_rows.html", ctx)


def _queue_context(
    request: Request,
    view: str,
    bucket: str | None,
    source: str | None,
    q: str | None,
    min_score: int | None,
    sort: str,
    max_age_hours: float | None = None,
    owner: str = "",
    sources_sel: list[str] | None = None,
) -> dict[str, Any]:
    conn = _conn(request)
    effective = _effective_max_age_hours(max_age_hours)
    # Okno wieku dotyczy kolejki decyzyjnej. Archiwum (wyslane, odrzucone) pokazuje
    # wszystko: filtr "mlodsze niz 12h" na historii odpowiedzi nie ma sensu i gubilby wpisy.
    queue_view = view == "queue"
    # Zapamietane platformy obowiazuja tylko wtedy, gdy adres nie niesie wlasnego
    # filtra: jawny wybor w formularzu zawsze wygrywa z preferencja.
    zapamietane: list[str] = []
    if owner:
        zapamietane = list(store.get_user_pref(conn, owner).get("platforms") or [])
    # Kolejnosc: jawne klikniecie w chipy (sources), potem pojedynczy parametr
    # z adresu (panel-shell), na koncu zapamietane platformy.
    if sources_sel:
        platformy, pojedyncze = [s for s in sources_sel if s], None
    elif source or "source" in request.query_params:
        platformy, pojedyncze = [], (source or None)
    elif zapamietane:
        platformy, pojedyncze = list(zapamietane), None
    else:
        platformy, pojedyncze = [], None
    leads = store.list_leads(
        conn,
        view=view if view != "noquote" else "all",
        bucket=bucket or None,
        source=pojedyncze,
        sources=platformy,
        q=(q or "").strip() or None,
        min_score=min_score or None,
        without_quote=view == "noquote",
        max_age_hours=effective if queue_view else 0,
        sort=sort,
        limit=400,
    )
    counts = store.counts(conn)
    counts["queue"] = store.counts(conn, max_age_hours=effective)["queue"]
    return {
        "request": request,
        "leads": leads,
        "counts": counts,
        "sources": store.sources(conn),
        "aktywne": platformy or list(store.sources(conn)),
        "platforms": platformy,
        "view": view,
        "bucket": bucket or "",
        "source": source or "",
        "q": q or "",
        "min_score": min_score or 0,
        "sort": sort,
        "max_age_hours": effective,
        "imported_at": store.last_import(conn),
    }


def _pelny_opis(lead) -> str:
    """Useme oddaje w liscie skrot (~250 znakow). Pelny opis sciagamy z cache."""
    opis = lead.description or ""
    if lead.source == "useme" and len(opis) < 400:
        from freelance_pipeline.offer_detail import fetch_useme_description

        pelny = fetch_useme_description(lead.url)
        if len(pelny) > len(opis):
            return pelny
    return opis


@router.get("/lead/{key:path}", response_class=HTMLResponse)
def lead_page(
    request: Request,
    key: str,
    owner: str = Depends(require_owner),
    sort: str = Query("newest"),
) -> HTMLResponse:
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    quote = store.quote_for(conn, _cennik(request), key) if _cennik(request) else None
    return templates.TemplateResponse(
        request,
        "lead.html",
        {
            "request": request,
            "lead": lead,
            "opis": _pelny_opis(lead),
            "quote": quote,
            "counts": store.counts(conn),
            "next_key": _next_key(conn, key, sort),
            "wiadomosci": store.messages_for(conn, key),
            "unread": store.unread_count(conn, key),
        },
    )


def _next_key(conn, key: str, sort: str = "newest") -> str | None:
    """Nastepna oferta w kolejce, w tym samym porzadku, ktory widzi uzytkownik."""
    rows = store.list_leads(conn, view="queue", sort=sort, limit=400)
    keys = [row.key for row in rows]
    if key not in keys:
        return keys[0] if keys else None
    index = keys.index(key)
    if index + 1 < len(keys):
        return keys[index + 1]
    return keys[0] if len(keys) > 1 else None


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, owner: str = Depends(require_owner)) -> HTMLResponse:
    conn = _conn(request)
    per_source = [
        dict(row)
        for row in conn.execute(
            "SELECT source, status, COUNT(*) AS n FROM leads GROUP BY source, status "
            "ORDER BY source, status"
        )
    ]
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "request": request,
            "counts": _nav_counts(request),
            "per_source": per_source,
            "events": store.daily_counts(conn, days=30),
            "imported_at": store.last_import(conn),
        },
    )


# --------------------------------------------------------------------------- zapis


@router.post("/lead/{key:path}/status", response_class=HTMLResponse)
def set_status(
    request: Request,
    key: str,
    status: str = Form(...),
    note: str = Form(""),
    next: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> Response:
    conn = _conn(request)
    try:
        lead = store.save_status(conn, _ledger(request), key, status, note=note.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty") from exc
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    target = next or _next_key(conn, key) or "/"
    # Klucz oferty nie zaczyna sie od ukosnika: bez tego RedirectResponse traktuje go jako
    # sciezke wzgledna i dokleja do biezacego URL (byl 404 po kazdej decyzji).
    if not str(target).startswith("/"):
        target = "/lead/" + str(target)
    if _is_htmx(request):
        response = Response(status_code=204)
        response.headers["HX-Redirect"] = str(target)
        return response
    return RedirectResponse(str(target), status_code=303)


@router.post("/lead/{key:path}/draft", response_class=HTMLResponse)
def save_draft(
    request: Request,
    key: str,
    body: str = Form(""),
    instruction: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    conn = _conn(request)
    try:
        lead = store.save_draft(
            conn, _ledger(request), key, body, instruction=instruction.strip(), origin="web"
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"request": request, "message": str(exc)}, status_code=422
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty") from exc
    return templates.TemplateResponse(
        request, "_draft.html", {"request": request, "lead": lead, "notice": "zapisano wersję"}
    )


@router.post("/lead/{key:path}/draft/improve", response_class=HTMLResponse)
def improve_draft(
    request: Request,
    key: str,
    instruction: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    instruction = instruction.strip()
    if not instruction:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": "wpisz, co zmienić w wiadomości"},
            status_code=422,
        )
    settings = request.app.state.settings
    if not settings.openrouter_key:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {
                "request": request,
                "message": "brak klucza modelu na serwerze, popraw przez Telegrama: „popraw 3: …”",
            },
            status_code=503,
        )
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    try:
        text = _improve_text(lead, instruction, _cennik(request))
    except Exception as exc:  # noqa: BLE001 - model, walidacja, siec
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": f"model nie napisał wersji: {exc}"},
            status_code=502,
        )
    lead = store.save_draft(
        conn, _ledger(request), key, text, instruction=instruction, origin="model"
    )
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    return templates.TemplateResponse(
        request,
        "_draft.html",
        {"request": request, "lead": lead, "notice": f"wersja {lead.version} z modelu"},
    )


def _improve_text(lead: store.LeadDetail, instruction: str, cennik=None) -> str:
    # Scraper listy Useme oddaje skrot (~250 znakow, ucięty w połowie zdania).
    # Pełny opis pobieramy dopiero tutaj, na żądanie draftu, żeby nie wpaść w 429.
    if lead.source == "useme" and len(lead.description or "") < 400:
        from freelance_pipeline.offer_detail import fetch_useme_description

        pelny = fetch_useme_description(lead.url)
        if len(pelny) > len(lead.description or ""):
            lead.description = pelny
    from freelance_pipeline.portfolio import render_portfolio_block
    from freelance_pipeline.pricing import price_drafts
    from freelance_pipeline.proposal import OfferInput, ProposalWriter

    prefix = ""
    if instruction.strip():
        prefix = (
            "\n\nUWAGA OD JANA (poprawka obowiązkowa, ważniejsza niż szablon): "
            f"{instruction}\nZachowaj wszystkie zasady kontraktu poza tym, co każe zmienić ta uwaga."
        )
    offer = OfferInput(
        key=lead.key,
        title=lead.title,
        source=lead.source,
        url=lead.url,
        author=lead.author,
        description=(lead.description or "") + prefix,
        bucket=lead.bucket,
        score=int(lead.score) if lead.score is not None else None,
        budget=lead.budget,
        currency=lead.currency,
    )
    draft = ProposalWriter().write([offer])[0]
    # Kwota wchodzi do tekstu tylko z cennika, tak samo jak w scripts/popraw.py.
    # Bez dopasowania zostaje zdanie o wycenie po szczegolach, ale nigdy znacznik
    # [WYCENA: ...], bo to instrukcja dla agenta, nie zdanie oferty.
    text = draft.text
    # Wycenia tylko Jan. Cennik nie wchodzi do draftu automatycznie: dla ofert
    # z dwoma wariantami wybiera pierwszą usługę i daje kwotę nieodpowiadającą zakresowi.
    if lead.quote_origin == "jan" and lead.amount and lead.days:
        import re as _re
        zdanie = (
            f"Wycena: {lead.amount:.0f} zł brutto, termin około {lead.days} dni roboczych."
        )
        text = _re.sub(r"\[WYCENA:[^\]]*\]", zdanie, text)
    from freelance_pipeline.pricing import strip_price_marker

    text, _ = strip_price_marker(text)
    return with_ad_reference(text, lead.source, lead.url)


@router.post("/lead/{key:path}/draft/generate", response_class=HTMLResponse)
def generate_draft(
    request: Request, key: str, owner: str = Depends(require_owner), _: None = Depends(check_origin)
) -> HTMLResponse:
    """Pierwsza wiadomosc dla oferty, ktora jej jeszcze nie ma (tryb natychmiastowy jej nie zapisal)."""
    settings = request.app.state.settings
    if not settings.openrouter_key:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": "brak klucza modelu na serwerze"},
            status_code=503,
        )
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    try:
        text = _improve_text(lead, "", _cennik(request))
    except Exception as exc:  # noqa: BLE001 - model, walidacja, siec
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": f"model nie napisał wiadomości: {exc}"},
            status_code=502,
        )
    lead = store.save_draft(conn, _ledger(request), key, text, origin="model")
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    return templates.TemplateResponse(
        request,
        "_draft.html",
        {"request": request, "lead": lead, "notice": f"wersja {lead.version} z modelu"},
    )


@router.post("/lead/{key:path}/note", response_class=HTMLResponse)
def add_note(
    request: Request,
    key: str,
    body: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    conn = _conn(request)
    try:
        store.add_note(conn, key, body)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"request": request, "message": str(exc)}, status_code=422
        )
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    return templates.TemplateResponse(
        request, "_notes.html", {"request": request, "lead": lead, "notice": "zapisane"}
    )


@router.post("/lead/{key:path}/watek/podglad", response_class=HTMLResponse)
async def thread_preview(
    request: Request,
    key: str,
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    """Podglad parsera: pokazuje rozpoznane wiadomosci, nic nie zapisuje."""
    form = await request.form()
    tekst = str(form.get("tekst") or "")
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    parsed = parse_thread(tekst)
    context: dict[str, Any] = {"request": request, "lead": lead, "podglad": parsed}
    if not parsed:
        context["blad"] = (
            "Nie rozpoznałem ani jednej wiadomości. Wklej sam tekst wątku "
            "(nie zrzut ekranu) — parser potrzebuje linii z treścią."
        )
    return templates.TemplateResponse(request, "_watek_podglad.html", context)


@router.post("/lead/{key:path}/watek/zapisz", response_class=HTMLResponse)
async def thread_save(
    request: Request,
    key: str,
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    """Zapisuje wiadomosci po korekcie Jana; duplikaty odsiewa store."""
    form = await request.form()
    kierunki = form.getlist("kierunek")
    tresci = form.getlist("tresc")
    czasy = form.getlist("czas")
    items = [
        {"kierunek": kierunek, "tresc": tresc, "czas": czas}
        for kierunek, tresc, czas in zip(kierunki, tresci, czasy)
    ]
    conn = _conn(request)
    result = store.add_messages(conn, key, items, zrodlo=str(form.get("zrodlo") or "useme"))
    if result is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    notice = f"zapisano {result['dodane']} wiadomości"
    if result["pominiete"]:
        notice += f", pominięto {result['pominiete']} (puste lub już były)"
    return templates.TemplateResponse(
        request,
        "_watek.html",
        {
            "request": request,
            "lead": lead,
            "wiadomosci": store.messages_for(conn, key),
            "unread": store.unread_count(conn, key),
            "notice": notice,
        },
    )


@router.post("/lead/{key:path}/watek/przeczytane", response_class=HTMLResponse)
def thread_read(
    request: Request,
    key: str,
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    marked = store.mark_read(conn, key)
    return templates.TemplateResponse(
        request,
        "_watek.html",
        {
            "request": request,
            "lead": lead,
            "wiadomosci": store.messages_for(conn, key),
            "unread": store.unread_count(conn, key),
            "notice": f"oznaczono {marked} jako przeczytane" if marked else "",
        },
    )


@router.post("/lead/{key:path}/contact", response_class=HTMLResponse)
def set_contact(
    request: Request,
    key: str,
    email: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    conn = _conn(request)
    if conn.execute("SELECT 1 FROM leads WHERE key = ?", (key,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    conn.execute(
        "UPDATE leads SET contact_email = ?, updated_at = ? WHERE key = ?",
        (email.strip()[:200], store.now_iso(), key),
    )
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    return templates.TemplateResponse(
        request, "_contact.html", {"request": request, "lead": lead, "notice": "zapisane"}
    )


@router.post("/lead/{key:path}/mail/send", response_class=HTMLResponse)
def send_mail(
    request: Request,
    key: str,
    to: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    """Wysyla wiadomosc na skrzynke klienta. Klikniecie przycisku jest decyzja Jana."""
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    recipient = (to or lead.contact_email or "").strip()
    subject = (subject or f"Zapytanie: {lead.title}").strip()
    body = (body or lead.draft or "").strip()
    try:
        sent = mailer.send(to=recipient, subject=subject, body=body)
    except mailer.MailError as exc:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": f"mail nie poszedl: {exc}"},
            status_code=502,
        )
    conn.execute(
        "UPDATE leads SET contact_email = ?, updated_at = ? WHERE key = ?",
        (recipient[:200], store.now_iso(), key),
    )
    store.add_note(conn, key, f"Mail wyslany do {recipient}: {subject}")
    lead = store.get_lead(conn, key)
    return templates.TemplateResponse(
        request, "_contact.html", {"request": request, "lead": lead, "notice": f"mail wysłany do {recipient}"}
    )


@router.post("/lead/{key:path}/fb/plan", response_class=HTMLResponse)
def fb_plan(
    request: Request,
    key: str,
    dm: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    """Sprawdza post na FB: autor, czy da sie napisac priv. Nic nie publikuje."""
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    if "facebook" not in (lead.source or ""):
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": "ta oferta nie jest z Facebooka"},
            status_code=422,
        )
    if not lead.url:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"request": request, "message": "oferta nie ma linku do posta"},
            status_code=422,
        )
    task_id = fblib.start("plan", lead.url)
    return templates.TemplateResponse(
        request,
        "_fb.html",
        {"request": request, "lead": lead, "task_id": task_id, "dm": dm or lead.draft or "",
         "step": "plan", "state": {"state": "running"}},
    )


@router.post("/lead/{key:path}/fb/send", response_class=HTMLResponse)
def fb_send(
    request: Request,
    key: str,
    dm: str = Form(""),
    comment: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    """Publikuje wiadomosc priv (jesli mozliwa) i komentarz. Klikniecie jest decyzja Jana."""
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    if not lead.url:
        raise HTTPException(status_code=422, detail="oferta nie ma linku do posta")
    task_id = fblib.start("send", lead.url, (dm or lead.draft or "").strip(), comment.strip())
    return templates.TemplateResponse(
        request,
        "_fb.html",
        {"request": request, "lead": lead, "task_id": task_id, "step": "send",
         "state": {"state": "running"}},
    )


@router.get("/fb-status", response_class=HTMLResponse)
def fb_status(
    request: Request,
    key: str = Query(""),
    task: str = Query(""),
    step: str = Query("send"),
    owner: str = Depends(require_owner),
) -> HTMLResponse:
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    state = fblib.status(task)
    return templates.TemplateResponse(
        request, "_fb.html", {"request": request, "lead": lead, "task_id": task,
                              "state": state, "step": step}
    )


@router.post("/lead/{key:path}/quote", response_class=HTMLResponse)
def refresh_quote(
    request: Request, key: str, owner: str = Depends(require_owner), _: None = Depends(check_origin)
) -> HTMLResponse:
    conn = _conn(request)
    quote = store.quote_for(conn, _cennik(request), key, refresh=True)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    return templates.TemplateResponse(
        request, "_quote.html", {"request": request, "lead": lead, "quote": quote}
    )


@router.post("/lead/{key:path}/quote/manual", response_class=HTMLResponse)
def save_manual_quote(
    request: Request,
    key: str,
    amount: float = Form(...),
    days: int = Form(...),
    note: str = Form(""),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> HTMLResponse:
    """Wycena wpisana przez Jana. Zapisuje sie w historii jako origin='jan'."""
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    try:
        store.save_manual_quote(conn, key, amount, days, note, author=owner)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "_quote.html",
            {"request": request, "lead": lead, "quote": store.latest_quote(conn, key), "error": str(exc)},
            status_code=422,
        )
    lead = store.get_lead(conn, key)
    return templates.TemplateResponse(
        request,
        "_quote.html",
        {"request": request, "lead": lead, "quote": store.latest_quote(conn, key), "notice": "zapisane"},
    )


UWAGI_PATH = Path("/opt/data/cache/snajper-web/uwagi.jsonl")


@router.post("/api/uwagi", response_class=JSONResponse)
async def add_uwaga(
    request: Request, owner: str = Depends(require_owner), _: None = Depends(check_origin)
):
    """Komentarz Jana do konkretnego elementu panelu (picker Alt+klik)."""
    from datetime import datetime

    data = await request.json()
    komentarz = str(data.get("komentarz") or "").strip()
    if not komentarz:
        raise HTTPException(status_code=422, detail="komentarz jest pusty")
    wpis = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "autor": owner,
        "strona": str(data.get("strona") or "")[:300],
        "selektor": str(data.get("selektor") or "")[:500],
        "html": str(data.get("html") or "")[:2000],
        "komentarz": komentarz[:2000],
    }
    UWAGI_PATH.parent.mkdir(parents=True, exist_ok=True)
    with UWAGI_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(wpis, ensure_ascii=False) + "\n")
    return JSONResponse({"ok": True})


@router.get("/api/uwagi", response_class=JSONResponse)
def list_uwagi(owner: str = Depends(require_owner)):
    if not UWAGI_PATH.is_file():
        return JSONResponse({"uwagi": []})
    out = []
    for line in UWAGI_PATH.read_text(encoding="utf-8").splitlines()[-100:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return JSONResponse({"uwagi": out})


@router.post("/api/import", response_class=JSONResponse)
def run_import(request: Request, owner: str = Depends(require_owner), _: None = Depends(check_origin)):
    settings = request.app.state.settings
    report = store.import_sources(
        _conn(request),
        cache_dir=settings.cache_dir,
        reports_dir=settings.reports_dir,
        ledger_path=settings.ledger_path,
    )
    return JSONResponse(report)


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, owner: str = Depends(require_owner)) -> HTMLResponse:
    settings = request.app.state.settings
    try:
        key = pushlib.ensure_keys(settings.vapid_private_path, settings.vapid_public_path)
        key_error = ""
    except Exception as exc:  # noqa: BLE001 - brak biblioteki nie moze wysypac panelu
        key, key_error = "", str(exc)[:200]
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "request": request,
            "vapid_key": key,
            "key_error": key_error,
            "devices": store_subscriptions(_conn(request)),
            "counts": _nav_counts(request),
            "imported_at": store.last_import(_conn(request)),
        },
    )


def store_subscriptions(conn) -> list[dict]:
    try:
        return pushlib.subscriptions(conn)
    except Exception:  # noqa: BLE001 - brak tabeli przed migracja
        return []


@router.get("/api/push/status", response_class=JSONResponse)
def push_status(request: Request, owner: str = Depends(require_owner)) -> JSONResponse:
    rows = store_subscriptions(_conn(request))
    return JSONResponse(
        {
            "devices": len(rows),
            "labels": [row["label"] or row["endpoint"][:32] for row in rows],
            "last_ok": [row["last_ok_at"] for row in rows],
        }
    )


@router.post("/api/push/subscribe", response_class=JSONResponse)
async def push_subscribe(
    request: Request, owner: str = Depends(require_owner), _: None = Depends(check_origin)
) -> JSONResponse:
    payload = await request.json()
    label = request.headers.get("User-Agent", "")[:60]
    try:
        endpoint = pushlib.save_subscription(_conn(request), payload, label=label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "endpoint": endpoint[:48] + "..."})


@router.post("/api/push/unsubscribe", response_class=JSONResponse)
async def push_unsubscribe(
    request: Request, owner: str = Depends(require_owner), _: None = Depends(check_origin)
) -> JSONResponse:
    payload = await request.json()
    removed = pushlib.delete_subscription(_conn(request), str(payload.get("endpoint") or ""))
    return JSONResponse({"ok": True, "removed": removed})


@router.post("/api/push/test", response_class=JSONResponse)
def push_test(
    request: Request,
    url: str = Query("/"),
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
) -> JSONResponse:
    settings = request.app.state.settings
    counts = store.counts(_conn(request))
    result = pushlib.send(
        _conn(request),
        private_path=settings.vapid_private_path,
        subject=settings.vapid_subject,
        title="Snajper: test powiadomień",
        body=f"W kolejce {counts['queue']} ofert do decyzji. To powiadomienie testowe.",
        url=url,
        tag="snajper-test",
    )
    return JSONResponse(
        {
            "sent": result.sent,
            "failed": result.failed,
            "removed": result.removed,
            "errors": result.errors,
        }
    )


@router.get("/api/prefs", response_class=JSONResponse)
def get_prefs(request: Request, owner: str = Depends(require_owner)) -> JSONResponse:
    """Zapisane filtry uzytkownika plus lista platform, ktore da sie wybrac.

    Shell czyta baze bezposrednio, wiec potrzebuje jednego miejsca na preference
    i jednego na slownik platform; oba sa per konto (login z naglowka tozsamosci).
    """
    conn = _conn(request)
    zapisane = store.get_user_pref(conn, owner)
    return JSONResponse(
        {
            "login": owner,
            "filters": {"platforms": list(zapisane.get("platforms") or [])},
            "sources": store.sources(conn),
        }
    )


@router.post("/api/prefs", response_class=JSONResponse)
async def save_prefs(
    request: Request, owner: str = Depends(require_owner), _: None = Depends(check_origin)
) -> JSONResponse:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="oczekiwano JSON") from exc
    conn = _conn(request)
    filt = payload.get("filters", payload) if isinstance(payload, dict) else {}
    zapisane = store.normalize_filters(conn, filt)
    store.set_user_pref(conn, owner, zapisane)
    return JSONResponse({"ok": True, "login": owner, "filters": zapisane})


@router.post("/prefs/reset")
def prefs_reset(
    request: Request,
    owner: str = Depends(require_owner),
    _: None = Depends(check_origin),
    view: str = Form("queue"),
) -> RedirectResponse:
    """Czyści zapisany filtr platformy dla tego konta (przycisk w nagłówku listy)."""
    store.set_user_pref(_conn(request), owner, {"platforms": []})
    return RedirectResponse(f"/?view={view}", status_code=303)


@router.get("/api/health", response_class=JSONResponse)
def health(request: Request) -> JSONResponse:
    """Publiczny health check: tylko liczby, zero tresci ofert."""
    conn = _conn(request)
    return JSONResponse(
        {
            "ok": True,
            "schema": store.SCHEMA_VERSION,
            "leads": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
            "imported_at": store.last_import(conn),
        }
    )


def _cennik(request: Request):
    settings = request.app.state.settings
    if not Path(settings.cennik_path).exists():
        return None
    try:
        return load_cennik(settings.cennik_path)
    except Exception:  # noqa: BLE001 - cennik bez wplywu na liste
        return None


# --------------------------------------------------------------------------- styl


def _styl_oferty(conn, limit: int = 10) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT l.key, l.title, l.status, l.first_seen_at
               FROM leads l
               WHERE l.source = 'useme' AND COALESCE(l.description, '') != ''
                 AND EXISTS (SELECT 1 FROM draft_versions d WHERE d.lead_key = l.key)
               ORDER BY l.first_seen_at DESC LIMIT ?""",
            (limit,),
        )
    ]


def _przyklady_po_ofercie() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in styl.lista_przykladow():
        lead = (p.get("meta") or {}).get("lead")
        if lead and lead != "brak":
            out[lead] = p
    return out


@router.get("/styl", response_class=HTMLResponse)
def styl_page(
    request: Request,
    owner: str = Depends(require_owner),
    ok: str | None = Query(None),
    view: str = Query("todo"),
) -> HTMLResponse:
    conn = _conn(request)
    oferty = _styl_oferty(conn)
    mapa = _przyklady_po_ofercie()
    for o in oferty:
        p = mapa.get(o["key"])
        o["zapisany"] = bool(p)
        o["zatwierdzony"] = bool(p and str(p.get("status", "")).startswith("zatwierdzona"))
        o["przyklad_id"] = p["id"] if p else None
        o["zapisany_data"] = (p.get("meta") or {}).get("data", "") if p else ""
        o["status_przykladu"] = str(p.get("status", "")) if p else ""
    todo = [o for o in oferty if not o["zapisany"]]
    done = [o for o in oferty if o["zapisany"]]
    ctx = {
        "oferty": done if view == "done" else todo,
        "view": "done" if view == "done" else "todo",
        "ile_todo": len(todo),
        "ile_done": len(done),
        "zrobione": len(done),
        "ok": ok,
        "przyklady": len(mapa),
        "counts": _nav_counts(request),
        "imported_at": store.last_import(conn),
    }
    return templates.TemplateResponse(request, "styl.html", ctx)


@router.get("/styl/przyklady", response_class=HTMLResponse)
def styl_przyklady(
    request: Request,
    owner: str = Depends(require_owner),
    ok: str | None = Query(None),
) -> HTMLResponse:
    conn = _conn(request)
    ctx = {
        "przyklady": styl.lista_przykladow(),
        "ok": ok,
        "counts": _nav_counts(request),
        "imported_at": store.last_import(conn),
    }
    return templates.TemplateResponse(request, "styl_przyklady.html", ctx)


@router.get("/styl/{key:path}", response_class=HTMLResponse)
def styl_zadanie(
    request: Request,
    key: str,
    owner: str = Depends(require_owner),
    ok: str | None = Query(None),
) -> HTMLResponse:
    conn = _conn(request)
    lead = store.get_lead(conn, key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    opis = _pelny_opis(lead)
    mapa = _przyklady_po_ofercie()
    ctx = {
        "lead": lead,
        "opis": opis,
        "przyklad": mapa.get(key),
        "przyklady": len(mapa),
        "ok": ok,
        "counts": _nav_counts(request),
        "imported_at": store.last_import(conn),
    }
    return templates.TemplateResponse(request, "styl_zadanie.html", ctx)


@router.post("/styl/zapisz")
def styl_zapisz(
    request: Request,
    owner: str = Depends(require_owner),
    lead_key: str = Form(...),
    kategoria: str = Form(""),
    odpowiedz: str = Form(...),
) -> RedirectResponse:
    check_origin(request)
    conn = _conn(request)
    row = conn.execute(
        "SELECT title, description, source FROM leads WHERE key = ?", (lead_key,)
    ).fetchone()
    oferta = ""
    if row is not None:
        oferta = f"{row['title']} [{row['source']}] {(row['description'] or '')[:400]}"
    ok = styl.zapisz_przyklad(oferta, odpowiedz, kategoria, lead_key)
    if not ok:
        return RedirectResponse("/styl?ok=err", status_code=303)
    # Ta sama treść idzie do oferty jako bieżący draft, żeby Jan mógł jej użyć.
    try:
        draft_ok = store.save_draft(conn, _ledger(request), lead_key, odpowiedz, origin="ada") is not None
    except Exception:  # noqa: BLE001 - przyklad jest juz zapisany, draft nie moze tego cofnac
        draft_ok = False
    return RedirectResponse(f"/styl?ok={'1' if draft_ok else '5'}", status_code=303)


@router.post("/styl/przywroc")
def styl_przywroc(
    request: Request,
    owner: str = Depends(require_owner),
    lead_key: str = Form(...),
) -> RedirectResponse:
    check_origin(request)
    ok = styl.przywroc_przyklad(lead_key)
    return RedirectResponse(f"/styl?ok={'4' if ok else 'err'}", status_code=303)


@router.post("/styl/zatwierdz")
def styl_zatwierdz(
    request: Request,
    owner: str = Depends(require_owner),
    id: int = Form(...),
) -> RedirectResponse:
    check_origin(request)
    ok = styl.zatwierdz_przyklad(id)
    return RedirectResponse(f"/styl/przyklady?ok={'2' if ok else 'err'}", status_code=303)


@router.post("/styl/generuj")
def styl_generuj(
    request: Request,
    owner: str = Depends(require_owner),
    lead_key: str = Form(...),
) -> RedirectResponse:
    """Nowa wersja DeepSeeka dla oferty. Zapisuje draft, wraca na tę samą ofertę."""
    check_origin(request)
    settings = request.app.state.settings
    if not settings.openrouter_key:
        return RedirectResponse(f"/styl/{quote(lead_key)}?ok=err", status_code=303)
    conn = _conn(request)
    lead = store.get_lead(conn, lead_key)
    if lead is None:
        raise HTTPException(status_code=404, detail="nie ma takiej oferty")
    try:
        text = _improve_text(lead, "", _cennik(request))
        store.save_draft(conn, _ledger(request), lead_key, text, origin="deepseek")
    except Exception:  # noqa: BLE001 - model, walidacja, siec
        return RedirectResponse(f"/styl/{quote(lead_key)}?ok=err", status_code=303)
    return RedirectResponse(f"/styl/{quote(lead_key)}?ok=3", status_code=303)
