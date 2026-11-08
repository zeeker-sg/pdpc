"""HTTP-layer tests: httpx (client config, transports, params, multipart, status errors)
and tenacity (retry count, back-off, RetryError) driven through httpx.MockTransport.
"""

import importlib
import time

import enforcement_decisions as ed
import httpx
import pytest
import tenacity
from helpers import FETCH_RESOURCES
from zeeker import Skip


@pytest.fixture(params=FETCH_RESOURCES)
def mod(request):
    return importlib.import_module(request.param)


@pytest.fixture
def sleeps(monkeypatch):
    """Record the seconds tenacity (and _polite_sleep) ask to sleep for."""
    recorded = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


def _flaky(failures, status=503, ok=None):
    """Handler that fails ``failures`` times with ``status`` then returns ``ok``."""
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] <= failures:
            return httpx.Response(status, text="upstream trouble")
        return ok(request) if ok else httpx.Response(200, text="fine")

    return handler


# --- retry behaviour of the three fetch helpers ----------------------------------


def test_fetch_text_retries_5xx_then_succeeds(mod, mock_network, sleeps):
    seen = mock_network(_flaky(2, 503, lambda r: httpx.Response(200, text="<p>ok</p>")))
    with mod._make_client() as client:
        assert mod._fetch_text(client, "https://www.pdpc.gov.sg/x") == "<p>ok</p>"
    assert len(seen) == 3
    # Exponential back-off between attempts: two waits, bounded by the decorator (1..10s).
    assert len(sleeps) == 2
    assert all(1 <= s <= 10 for s in sleeps) and sleeps[0] <= sleeps[1]


def test_fetch_json_retries_then_parses(mod, mock_network):
    seen = mock_network(
        _flaky(1, 500, lambda r: httpx.Response(200, json={"totalItems": 7, "data": []}))
    )
    with mod._make_client() as client:
        assert mod._fetch_json(client, "https://www.pdpc.gov.sg/api", params={"a": "1"}) == {
            "totalItems": 7,
            "data": [],
        }
    assert len(seen) == 2
    assert seen[-1].url.params["a"] == "1"


def test_fetch_bytes_retries_and_returns_content(mod, mock_network):
    seen = mock_network(_flaky(1, 502, lambda r: httpx.Response(200, content=b"%PDF-1.7 bytes")))
    with mod._make_client() as client:
        assert mod._fetch_bytes(client, "https://www.pdpc.gov.sg/assets/x") == b"%PDF-1.7 bytes"
    assert len(seen) == 2


@pytest.mark.parametrize("helper", ["_fetch_text", "_fetch_json", "_fetch_bytes"])
def test_persistent_failure_raises_retryerror_after_max_attempts(mod, mock_network, helper):
    seen = mock_network(lambda r: httpx.Response(500, text="boom"))
    with mod._make_client() as client:
        with pytest.raises(tenacity.RetryError) as excinfo:
            getattr(mod, helper)(client, "https://www.pdpc.gov.sg/x")
    assert len(seen) == mod.MAX_RETRIES == 3
    cause = excinfo.value.last_attempt.exception()
    assert isinstance(cause, httpx.HTTPStatusError)
    assert cause.response.status_code == 500


def test_transport_errors_are_retried_too(mod, mock_network):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("proxy down", request=request)
        return httpx.Response(200, text="recovered")

    mock_network(handler)
    with mod._make_client() as client:
        assert mod._fetch_text(client, "https://www.pdpc.gov.sg/x") == "recovered"
    assert state["n"] == 2


def test_redirects_are_followed(mod, mock_network):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="landed")

    mock_network(handler)
    with mod._make_client() as client:
        assert mod._fetch_text(client, "https://www.pdpc.gov.sg/old") == "landed"


# --- client construction ---------------------------------------------------------


def test_make_client_defaults(mod):
    with mod._make_client() as client:
        assert client.headers["user-agent"] == mod.USER_AGENT
        assert client.follow_redirects is True
        assert client.timeout == httpx.Timeout(mod.REQUEST_TIMEOUT)


def test_make_client_builds_socks_proxy_transport(mod, monkeypatch):
    """TAILSCALE_PROXY is a socks5h:// URL in production: needs httpx[socks] (socksio)."""
    monkeypatch.setenv("TAILSCALE_PROXY", "socks5h://127.0.0.1:1055")
    with mod._make_client() as client:  # constructing must not need the network
        assert client is not None


# --- docling POST ----------------------------------------------------------------


def test_convert_pdf_with_docling_posts_multipart_and_reads_markdown(mod, mock_network):
    def handler(request):
        assert request.method == "POST"
        assert request.url == httpx.URL(f"{mod.DOCLING_SERVE_URL}/v1/convert/file")
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        body = request.read()
        assert b'name="files"' in body and b"%PDF-bytes" in body
        assert b'name="options"' in body and b'"to_formats": ["md"]' in body
        return httpx.Response(200, json=[{"document": {"md_content": "# Hello"}}])

    seen = mock_network(handler)
    assert mod._convert_pdf_with_docling(b"%PDF-bytes") == "# Hello"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"document": {"md_content": "dict form"}}, "dict form"),
        ([{"document": {"md_content": "list form"}}], "list form"),
        ([{"document": {"md_content": None}}], ""),
        ([{"document": {}}], ""),
    ],
)
def test_convert_pdf_with_docling_response_shapes(mod, mock_network, payload, expected):
    mock_network(lambda r: httpx.Response(200, json=payload))
    assert mod._convert_pdf_with_docling(b"x") == expected


def test_convert_pdf_with_docling_retries_5xx(mod, mock_network):
    seen = mock_network(
        _flaky(2, 503, lambda r: httpx.Response(200, json=[{"document": {"md_content": "ok"}}]))
    )
    assert mod._convert_pdf_with_docling(b"x") == "ok"
    assert len(seen) == 3


def test_convert_pdf_with_docling_persistent_failure(mod, mock_network):
    mock_network(lambda r: httpx.Response(500))
    with pytest.raises(tenacity.RetryError):
        mod._convert_pdf_with_docling(b"x")


# --- fetch_data failure handling (zeeker Skip contract) --------------------------


def test_fetch_data_listing_failure_raises_blocked_skip(mod, mock_network):
    seen = mock_network(lambda r: httpx.Response(403, text="CloudFront says no"))
    with pytest.raises(Skip) as excinfo:
        mod.fetch_data(None)
    assert excinfo.value.kind == "blocked"
    assert "listing fetch failed" in excinfo.value.reason
    assert len(seen) == 3  # one listing request, retried to the cap


def test_fetch_data_stops_when_first_page_is_all_known(monkeypatch, mock_network):
    """A listing page whose ids/urls are all already stored ends the crawl (incremental sync)."""
    known = {"id": "known", "title": "t", "topic": "x", "href": "", "date": ""}

    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json={"totalItems": 395, "data": [known]})
        return httpx.Response(500)

    class Existing:
        rows = [{"id": "known", "decision_url": ""}]
        db = None

    # A truthy existing table triggers dedupe/backfill (covered in test_zeeker_build.py).
    monkeypatch.setattr(ed, "_dedupe_existing_decisions", lambda db: None)
    monkeypatch.setattr(ed, "_run_fragment_backfill", lambda db: None)
    seen = mock_network(handler)
    assert ed.fetch_data(Existing()) == []
    assert len(seen) == 1  # page 2 was never requested


def test_fetch_data_gives_up_after_consecutive_listing_page_failures(mock_network):
    """New items on page 1 keep the loop going; pages 2-6 all fail -> abort after 5."""
    item = {
        "id": "n1",
        "title": "Breach of Foo by Bar",
        "topic": "Commission's Decisions",
        "href": "",
        "date": "1 Jan 2026",
    }

    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json={"totalItems": 395, "data": [item]})
        return httpx.Response(500)

    seen = mock_network(handler)
    records = ed.fetch_data(None)
    assert [r["id"] for r in records] == ["n1"]
    page_requests = [r for r in seen if r.url.params["page"] != "1"]
    assert len(page_requests) == ed.MAX_CONSECUTIVE_FAILURES * ed.MAX_RETRIES
