from __future__ import annotations

from typing import Any

import requests

# Honest, descriptive user agent. We do not impersonate a browser.
DEFAULT_USER_AGENT = "freelance-pipeline-v2/0.1 (read-only job collector)"


class SourceError(Exception):
    """Base class for every source adapter error.

    Adapters translate raw transport/parse failures into a source-specific
    subclass so callers can catch one precise type per source.
    """


class SourceHTTPError(SourceError):
    """A source request failed at the transport or HTTP layer."""

    def __init__(
        self,
        source: str,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.source = source
        self.url = url
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else "request failed"
        super().__init__(f"[{source}] {detail}: {message}")

    @property
    def is_http_error(self) -> bool:
        return self.status_code is not None


class SourceTimeoutError(SourceHTTPError):
    """A source request exceeded its configured timeout."""

    def __init__(self, source: str, message: str, *, url: str | None = None) -> None:
        self.source = source
        self.url = url
        self.status_code = None
        super().__init__(source, message, url=url, status_code=None)


class SourceParseError(SourceError):
    """A source returned a payload that could not be interpreted."""

    def __init__(self, source: str, message: str, *, url: str | None = None) -> None:
        self.source = source
        self.url = url
        super().__init__(f"[{source}] parse error: {message}")


class HTTPClient:
    """Thin ``requests`` wrapper with an honest UA and strict failure semantics.

    The session is injectable so tests can run fully offline against fakes.
    """

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
        source: str = "source",
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.user_agent = user_agent
        self.source = source

    def get(self, url: str, *, params: dict[str, str] | None = None) -> Any:
        try:
            resp = self._session.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise SourceTimeoutError(
                self.source, f"timed out after {self.timeout}s -> {url}", url=url
            ) from exc
        except requests.RequestException as exc:
            raise SourceHTTPError(self.source, f"{exc} -> {url}", url=url) from exc
        if resp.status_code >= 400:
            raise SourceHTTPError(
                self.source,
                f"{resp.status_code} for {url}",
                url=url,
                status_code=resp.status_code,
            )
        return resp