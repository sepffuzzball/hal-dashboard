"""Focused orchestration tests for the model startup readiness deadline.

These prove the refactor that lets a selected model take up to
``startup_timeout_seconds`` (one monotonic deadline) to become systemd-active
and HTTP-healthy, instead of a short 60-second health window. All of them run
quickly: the wall clock is faked and ``asyncio.sleep`` advances it, so a
simulated 900-second budget never actually waits real seconds.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import CONFIG_PATH, FakeSystemctl

import hal_dashboard.operations as ops
from hal_dashboard.config import load_config
from hal_dashboard.operations import (
    SWITCH_STEPS,
    AppContext,
    Operation,
    OperationManager,
    Step,
    _start_and_verify_model,
    run_switch,
)


class FakeClock:
    """Controls the monotonic clock and advances it only via sleeps."""

    def __init__(self, step: float = 0.2) -> None:
        self.now = 0.0
        self.step = step
        self.sleeps = 0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NeverHealthy:
    async def check(self, url: str) -> bool | None:  # noqa: ARG002 - url unused
        return None


class LateHealthy:
    """Reports unavailable for the first ``unavailable_checks`` calls, then True."""

    def __init__(self, unavailable_checks: int) -> None:
        self.unavailable_checks = unavailable_checks
        self.count = 0

    async def check(self, url: str) -> bool | None:  # noqa: ARG002 - url unused
        self.count += 1
        if self.count <= self.unavailable_checks:
            return None
        return True


def _wire_fake_time(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    """Point the operations module at the fake clock and fake sleep."""
    monkeypatch.setattr(ops, "_monotonic", lambda: clock.now)

    async def fake_sleep(delay: float) -> None:
        clock.advance(delay)
        clock.sleeps += 1

    # Replace the global asyncio.sleep used inside operations polling loops.
    monkeypatch.setattr(ops.asyncio, "sleep", fake_sleep)


async def _ctx(config, ctl, health) -> AppContext:
    return AppContext(config=config, systemctl=ctl, health=health, manager=OperationManager())


def test_start_and_verify_model_waits_past_old_60s_health_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Type=simple unit is active immediately, but health is unavailable for
    more than the old hard-coded 60 seconds (i.e. more than 300 poll rounds at
    0.2s); the model must still succeed well inside the 900s deadline."""
    clock = FakeClock()
    _wire_fake_time(monkeypatch, clock)

    async def scenario() -> None:
        config = load_config(CONFIG_PATH)
        model = config.model_by_id("vllm-dsv4-flash-vision")
        assert model is not None and model.startup_timeout_seconds == 900
        ctl = FakeSystemctl()
        # Health is unavailable for 310 polls -> 62 simulated seconds, then OK.
        health = LateHealthy(unavailable_checks=310)
        ctx = await _ctx(config, ctl, health)
        await _start_and_verify_model(ctx, model)
        # It waited past the old 60s budget (300 polls) before succeeding.
        assert health.count > 300
        assert ctl.states[model.unit] == "active"

    asyncio.run(scenario())


def test_start_and_verify_model_fails_when_health_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When health never becomes available, the deadline expires and the model
    start-and-verify sequence raises a sanitized failure."""
    clock = FakeClock()
    _wire_fake_time(monkeypatch, clock)

    async def scenario() -> None:
        config = load_config(CONFIG_PATH)
        model = config.model_by_id("sglang-qwen38-flash-next")
        assert model is not None and model.startup_timeout_seconds == 900
        ctl = FakeSystemctl()
        ctx = await _ctx(config, ctl, NeverHealthy())
        with pytest.raises(ops.ServiceStepError, match="health endpoint unreachable"):
            await _start_and_verify_model(ctx, model)
        # The clock truly advanced through the full deadline.
        assert clock.now >= 900.0

    asyncio.run(scenario())


def test_switch_rolls_back_best_effort_when_health_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full switch whose selected model never becomes healthy must fail after
    the deadline and restore the previous active model on a best-effort basis."""
    clock = FakeClock()
    _wire_fake_time(monkeypatch, clock)

    async def scenario() -> None:
        config = load_config(CONFIG_PATH)
        ctl = FakeSystemctl()
        # The previous model is running; the target model starts fresh.
        ctl.states["vllm-deepseek-v4.service"] = "active"
        ctx = await _ctx(config, ctl, NeverHealthy())
        operation = Operation(
            id="t1",
            kind="switch",
            params={"model_id": "sglang-qwen38-27b", "desired_companion": True},
            steps=[Step(name=name) for name in SWITCH_STEPS],
        )
        with pytest.raises(ops.ServiceStepError, match="previous state restored"):
            await run_switch(ctx, operation)
        # Rollback restored the previously-active model.
        assert ctl.states["vllm-deepseek-v4.service"] == "active"
        # The selected model was stopped again.
        assert ctl.states["sglang-qwen38-27b.service"] == "inactive"
        # The deadline really elapsed.
        assert clock.now >= 900.0

    asyncio.run(scenario())
