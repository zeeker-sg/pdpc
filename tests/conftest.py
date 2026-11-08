"""Shared pytest configuration: strictly offline, no sleeping, no env leakage."""

import sys
import time

import httpx
import pytest
from helpers import RESOURCES_DIR

# zeeker loads resource files by path and puts resources/ on sys.path so the
# sibling ``_backfill`` module imports. Mirror that so tests can import_module().
if str(RESOURCES_DIR) not in sys.path:
    sys.path.insert(0, str(RESOURCES_DIR))


@pytest.fixture(autouse=True)
def _offline_and_fast(monkeypatch):
    """Make every test offline, instant and independent of the developer's env."""
    # Neuters _polite_sleep() and tenacity's exponential back-off (tenacity's
    # default sleep is time.sleep, resolved at call time).
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    for var in (
        "TAILSCALE_PROXY",
        "PDPC_BACKFILL_RETRY_AFTER",
        "PDPC_BACKFILL_RETRY_QUARANTINED",
        "PDPC_DETAIL_BACKFILL_BATCH_SIZE",
        "PDPC_DETAIL_BACKFILL_MAX_RETRIES",
        "PDPC_DETAIL_BACKFILL_RECHECK",
    ):
        monkeypatch.delenv(var, raising=False)

    def _blocked(self, request):
        raise AssertionError(f"test attempted a real network call: {request.method} {request.url}")

    # Any request that is not routed to an httpx.MockTransport lands here.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)


@pytest.fixture
def mock_network(monkeypatch):
    """Route every httpx request in the process (clients and httpx.post) to a handler.

    Usage: ``calls = mock_network(handler)``; returns the list of requests seen.
    Patching ``HTTPTransport.handle_request`` keeps the real ``_make_client()`` /
    ``httpx.post`` code paths (headers, limits, redirects, multipart encoding)
    in play while the bytes never leave the process.
    """

    def install(handler):
        seen = []

        def counting_handler(request):
            seen.append(request)
            return handler(request)

        transport = httpx.MockTransport(counting_handler)
        monkeypatch.setattr(
            httpx.HTTPTransport,
            "handle_request",
            lambda self, request: transport.handle_request(request),
        )
        return seen

    return install
