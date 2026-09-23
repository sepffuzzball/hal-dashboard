"""Shared fixtures and in-memory fakes for the hal-dashboard test suite.

The HAL token and config path must exist in the environment BEFORE
``hal_dashboard.main`` is imported, because the module exposes a ready ``app``
for Uvicorn which is built at import time.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "systems.toml"

TEST_TOKEN = "pytest-secret-4e2c91f0a7b6d3f8c1a5e90"
assert len(TEST_TOKEN) >= 32  # the startup policy enforces a 32-character minimum
os.environ["HAL_DASHBOARD_TOKEN"] = TEST_TOKEN
os.environ["HAL_DASHBOARD_CONFIG"] = str(CONFIG_PATH)
os.environ.pop("HAL_DASHBOARD_ALLOWED_ORIGINS", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hal_dashboard import main as main_module  # noqa: E402


class FakeSystemctl:
    """In-memory systemd replacement. Records verbs; can stall and fail starts."""

    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.states: dict[str, str] = dict(states or {})
        self.calls: list[tuple[str, str]] = []
        self.hold = False
        self.fail_start_for: set[str] = set()
        self.result_for: dict[str, str] = {}

    async def state(self, unit: str) -> str:
        return self.states.get(unit, "inactive")

    async def run(self, verb: str, unit: str, *, timeout_seconds: float) -> str:
        self.calls.append((verb, unit))
        while self.hold:
            await asyncio.sleep(0.01)
        if verb == "start":
            if unit in self.fail_start_for:
                self.states[unit] = "failed"
                return "unit did not reach active state"
            if unit in self.result_for:
                return self.result_for[unit]
            self.states[unit] = "active"
        elif verb == "stop":
            self.states[unit] = "inactive"
        return "ok"


class FakeHealth:
    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[str] = []

    async def check(self, url: str) -> bool | None:
        self.calls.append(url)
        return self.results.get(url, True)


class DynamicProbes:
    """Stand-in for hal_dashboard.probes.Probes that reads the fake systemctl."""

    def __init__(self, config: Any, ctl: FakeSystemctl, health: FakeHealth) -> None:
        self.config = config
        self.ctl = ctl
        self.health = health
        self.calls = 0

    async def snapshot(self) -> dict[str, Any]:
        self.calls += 1
        items: list[dict[str, Any]] = []
        specs = [("model", m) for m in self.config.models] + [
            ("auxiliary", s) for s in self.config.services
        ]
        for kind, spec in specs:
            state = await self.ctl.state(spec.unit)
            healthy: bool | None = None
            reason: str | None = None
            if spec.health_url is None:
                reason = "no health endpoint configured"
            elif state != "active":
                reason = "health check skipped while unit is not active"
            else:
                healthy = await self.health.check(spec.health_url)
                reason = None if healthy else "health endpoint reported failure"
            items.append(
                {
                    "id": spec.id,
                    "display_name": spec.display_name,
                    "kind": kind,
                    "unit": spec.unit,
                    "state": state,
                    "healthy": healthy,
                    "reason": reason,
                }
            )
        return {
            "cpu": {"percent": 12.5, "reason": None},
            "memory": {
                "used_bytes": 64_000_000_000,
                "total_bytes": 256_000_000_000,
                "percent": 25.0,
                "reason": None,
            },
            "storage_root": {
                "used_bytes": 500,
                "total_bytes": 1000,
                "percent": 50.0,
                "reason": "storage probe partially unavailable",
            },
            "gpus": {
                "items": [
                    {
                        "index": 0,
                        "name": "Fake RTX 6000",
                        "utilization_percent": None,
                        "memory_used_bytes": None,
                        "memory_total_bytes": None,
                        "memory_percent": None,
                        "temperature_celsius": None,
                    }
                ],
                "reason": "some gpu metrics were unavailable",
            },
            "services": {"items": items, "reason": None},
        }


@pytest.fixture()
def ctl() -> FakeSystemctl:
    return FakeSystemctl()


@pytest.fixture()
def health() -> FakeHealth:
    return FakeHealth()


@pytest.fixture()
def app(ctl: FakeSystemctl, health: FakeHealth):
    application = main_module.create_app(CONFIG_PATH)
    ctx = application.state.ctx
    ctx.systemctl = ctl
    ctx.health = health
    ctx.probes = DynamicProbes(ctx.config, ctl, health)
    return application


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app, headers={"X-Hal-Token": TEST_TOKEN}) as test_client:
        yield test_client


@pytest.fixture()
def anon_client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client
