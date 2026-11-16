"""Small offline fakes so adapter tests never touch the network."""

from __future__ import annotations

import json
from typing import Any


class FakeResponse:
    """Stand-in for ``requests.Response`` sufficient for adapter tests."""

    def __init__(self, payload: Any, *, status_code: int = 200, url: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class FakeSession:
    """Maps exact URLs to canned responses or raises recorded exceptions."""

    def __init__(self, routes: dict[str, FakeResponse] | None = None, errors: dict[str, Exception] | None = None) -> None:
        self.routes = routes or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if url in self.errors:
            raise self.errors[url]
        if url not in self.routes:
            raise AssertionError(f"no fake route registered for {url!r}")
        return self.routes[url]