"""Aplikacja FastAPI panelu snajpera.

Uruchomienie lokalne::

    SNAJPER_ALLOW_LOCAL=1 PYTHONPATH=src .venv-web/bin/python -m uvicorn \
        freelance_pipeline.web.app:app --host 127.0.0.1 --port 8787

Produkcja: ``scripts/run_web.sh`` na VPS, za ``tailscale serve``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth as authlib
from . import store
from .config import Settings
from .routes import router
from .outreach_tab import router as outreach_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading

    settings: Settings = app.state.settings
    authlib.init_schema(settings.db_path)
    authlib.cleanup_sessions(settings.db_path)
    conn = store.connect(settings.db_path)
    app.state.conn = conn
    try:
        store.import_sources(
            conn,
            cache_dir=settings.cache_dir,
            reports_dir=settings.reports_dir,
            ledger_path=settings.ledger_path,
        )
    except Exception as exc:  # noqa: BLE001 - panel ma wstac nawet z pustym importem
        app.state.import_error = str(exc)

    stop = threading.Event()

    def reimport_loop() -> None:
        """Cron pisze do ledgera co 10 minut; panel nie moze czekac na reczne odswiezenie."""
        if settings.auto_import_seconds <= 0:
            return
        own = store.connect(settings.db_path)
        try:
            while not stop.wait(settings.auto_import_seconds):
                try:
                    store.import_sources(
                        own,
                        cache_dir=settings.cache_dir,
                        reports_dir=settings.reports_dir,
                        ledger_path=settings.ledger_path,
                    )
                except Exception:  # noqa: BLE001 - jeden nieudany przebieg nie zabija petli
                    continue
        finally:
            own.close()

    thread = threading.Thread(target=reimport_loop, name="snajper-reimport", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Snajper ofert", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings or Settings.from_env()
    app.state.import_error = ""
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        # Service worker musi byc serwowany z korzenia: rejestracja z /static/sw.js
        # daje zasieg tylko /static/, przez co navigator.serviceWorker.ready nigdy
        # nie wraca na stronie /notifications i przycisk "Włącz" milczy.
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    app.include_router(authlib.router)
    app.include_router(router)
    app.include_router(outreach_router)

    @app.middleware("http")
    async def brama_dostepu(request: Request, call_next):
        """Bez sesji nie ma panelu: GET dostaje login, POST dostaje 401.

        Publiczne sa tylko ekran logowania, statyki, service worker i health.
        Kazdy zapis laduje w ``audit_log`` z loginem, sciezka, kodem i adresem.
        """
        login = authlib.identity(request)
        authlib.set_current_user(login or "")
        sciezka = request.url.path
        if login is None and not authlib.is_public(sciezka):
            if request.method in ("GET", "HEAD"):
                cel = sciezka + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(cel, safe='/')}", status_code=303)
            return JSONResponse({"ok": False, "blad": "wymagane logowanie"}, status_code=401)
        response = await call_next(request)
        if login and request.method not in ("GET", "HEAD", "OPTIONS"):
            authlib.audit(
                app.state.settings.db_path,
                login,
                request.method,
                sciezka,
                response.status_code,
                authlib.client_ip(request),
            )
        return response

    return app


app = create_app()
