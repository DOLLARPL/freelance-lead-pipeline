import pytest
import requests

from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    HTTPClient,
    SourceHTTPError,
    SourceTimeoutError,
)
from tests.fakes import FakeResponse, FakeSession

URL = "https://example.test/"


def make_client(**kwargs):
    return HTTPClient(source="demo", **kwargs)


def test_sends_honest_user_agent():
    session = FakeSession(routes={URL: FakeResponse("ok")})
    make_client(session=session).get(URL)
    _, call_kwargs = session.calls[0]
    assert call_kwargs["headers"]["User-Agent"] == DEFAULT_USER_AGENT


def test_default_user_agent_is_honest_not_spoofed():
    assert DEFAULT_USER_AGENT.startswith("freelance-pipeline-v2/")
    assert "Mozilla" not in DEFAULT_USER_AGENT
    assert "Chrome" not in DEFAULT_USER_AGENT


def test_timeout_is_configurable_and_forwarded():
    session = FakeSession(routes={URL: FakeResponse("ok")})
    make_client(session=session, timeout=7.5).get(URL)
    _, call_kwargs = session.calls[0]
    assert call_kwargs["timeout"] == 7.5


def test_base_session_can_be_injected():
    session = FakeSession(routes={URL: FakeResponse("ok")})
    client = make_client(session=session)
    assert client._session is session


def test_http_error_status_raises_source_http_error_with_details():
    session = FakeSession(routes={URL: FakeResponse("nope", status_code=503)})
    with pytest.raises(SourceHTTPError) as exc:
        make_client(session=session).get(URL)
    assert exc.value.status_code == 503
    assert exc.value.url == URL
    assert "demo" in str(exc.value)


def test_connection_error_raises_source_http_error():
    session = FakeSession(errors={URL: requests.ConnectionError("boom")})
    with pytest.raises(SourceHTTPError):
        make_client(session=session).get(URL)


def test_timeout_raises_source_timeout_error():
    session = FakeSession(errors={URL: requests.Timeout("slow")})
    with pytest.raises(SourceTimeoutError):
        make_client(session=session, timeout=1.0).get(URL)


def test_returns_response_with_text_and_json():
    session = FakeSession(routes={URL: FakeResponse({"a": 1})})
    resp = make_client(session=session).get(URL)
    assert resp.text == '{"a": 1}'
    assert resp.json() == {"a": 1}