"""Focused unit tests for the HTTP health probe (hal_dashboard.probes).

Regression guard: urllib raises ``urllib.error.HTTPError`` (a ``URLError``
subclass) for non-success responses. ``urllib.error.HTTPStatusError`` only
exists in httpx-style APIs; referencing it made the probe crash with
``AttributeError`` while evaluating the except clause on Debian Python 3.13.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from hal_dashboard.probes import HealthChecker


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:8188/health", code, "status", hdrs=None, fp=None
    )


def test_health_error_status_yields_false(monkeypatch) -> None:
    """A configured endpoint replying 5xx must yield False, not raise."""

    def fake_urlopen(request, *args, **kwargs):  # noqa: ANN001, ARG001
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = asyncio.run(HealthChecker(timeout_seconds=0.5).check("http://127.0.0.1:8188/health"))
    assert result is False


def test_health_client_error_status_yields_false(monkeypatch) -> None:
    """4xx is an answer too: reachable but unhealthy -> False, not raise."""

    def fake_urlopen(request, *args, **kwargs):  # noqa: ANN001, ARG001
        raise _http_error(404)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = asyncio.run(HealthChecker(timeout_seconds=0.5).check("http://127.0.0.1:8188/health"))
    assert result is False


def test_health_success_status_yields_true(monkeypatch) -> None:
    def fake_urlopen(request, *args, **kwargs):  # noqa: ANN001, ARG001
        return _FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = asyncio.run(HealthChecker(timeout_seconds=0.5).check("http://127.0.0.1:8188/health"))
    assert result is True


def test_health_connection_error_yields_none(monkeypatch) -> None:
    """A bare URLError (endpoint unreachable) must stay None, not False."""

    def fake_urlopen(request, *args, **kwargs):  # noqa: ANN001, ARG001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = asyncio.run(HealthChecker(timeout_seconds=0.5).check("http://127.0.0.1:8188/health"))
    assert result is None
