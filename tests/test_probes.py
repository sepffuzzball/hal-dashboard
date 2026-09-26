"""Focused unit tests for the HTTP health probe (hal_dashboard.probes).

Regression guard: urllib raises ``urllib.error.HTTPError`` (a ``URLError``
subclass) for non-success responses. ``urllib.error.HTTPStatusError`` only
exists in httpx-style APIs; referencing it made the probe crash with
``AttributeError`` while evaluating the except clause on Debian Python 3.13.

The 502/503/504 gate is the transient-readiness guard: those statuses can be
returned by a backend (e.g. SGLang) that is still finishing startup, so the
probe reports them as not-ready (``None``) to keep the caller polling up to the
readiness deadline rather than treating them as a permanent failure.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

import pytest

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


def _check(monkeypatch, code: int) -> bool | None:
    def fake_urlopen(request, *args, **kwargs):  # noqa: ANN001, ARG001
        raise _http_error(code)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return asyncio.run(
        HealthChecker(timeout_seconds=0.5).check("http://127.0.0.1:8188/health")
    )


@pytest.mark.parametrize("code", [502, 503, 504])
def test_health_transient_gateway_status_yields_none(monkeypatch, code: int) -> None:
    """502/503/504 during startup is transient not-ready (None), not failure."""
    assert _check(monkeypatch, code) is None


@pytest.mark.parametrize("code", [404, 500])
def test_health_non_transient_error_status_yields_false(monkeypatch, code: int) -> None:
    """4xx and non-gateway 5xx are a real answer: reachable but unhealthy."""
    assert _check(monkeypatch, code) is False


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
