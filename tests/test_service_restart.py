"""Focused tests for the ComfyUI-only restart operation and its endpoint.

Covers the contract of ``POST /api/services/comfyui/restart``:
  * the route exists only for ComfyUI (no generic model/service restart),
  * validation happens in the runner after the snapshot (fail-closed on any
    non-exactly-active ComfyUI state and on conflicting models that are not
    positively stopped) with zero systemctl calls before mutation,
  * success requires a verified stop, a verified start, HTTP health, and the
    unit still being exactly active after verification,
  * post-mutation failures roll back to the snapshot best-effort,
  * an active operation yields the shared 409 busy semantics.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from conftest import CONFIG_PATH, TEST_TOKEN, FakeHealth, FakeSystemctl
from fastapi.testclient import TestClient
from test_api import wait_operation

from hal_dashboard.config import ServiceSpec, load_config
from hal_dashboard.operations import (
    SERVICE_RESTART_STEPS,
    AppContext,
    Operation,
    OperationManager,
    ServiceStepError,
    Step,
    run_service_restart,
)

VLLM = "vllm-deepseek-v4.service"
FLASH = "sglang-qwen38-flash-next.service"
COMFY = "comfyui.service"
COMFY_HEALTH = "http://127.0.0.1:8188/"


class ExitAfterHealth(FakeHealth):
    """Simulates a unit that exits right after its HTTP health check passes."""

    def __init__(self, ctl: FakeSystemctl, unit: str) -> None:
        super().__init__()
        self.ctl = ctl
        self.unit = unit

    async def check(self, url: str) -> bool | None:
        result = await super().check(url)
        if result is True:
            self.ctl.states[self.unit] = "inactive"  # process dies post-health
        return result


def _restart(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/services/comfyui/restart")
    assert response.status_code == 202
    body = response.json()
    # Same accepted-operation shape as every other mutation, no model target.
    assert body["status_url"] == f"/api/operations/{body['operation_id']}"
    assert "target_model_id" not in body
    return body


# ---------------------------------------------------------------------------
# Endpoint surface (ComfyUI-only, no generic restart capability)
# ---------------------------------------------------------------------------


def test_restart_success_verified_stop_then_start(
    client: TestClient, ctl: FakeSystemctl, health: FakeHealth
) -> None:
    ctl.states[COMFY] = "active"
    body = _restart(client)
    operation = wait_operation(client, body["operation_id"])
    assert operation["status"] == "succeeded"
    assert operation["kind"] == "service-restart"
    assert operation["message"] == "ComfyUI restarted"
    assert [step["name"] for step in operation["steps"]] == SERVICE_RESTART_STEPS
    assert [step["status"] for step in operation["steps"]] == ["done", "done", "done"]
    # Exactly one verified stop followed by exactly one start (start/stop only;
    # systemctl 'restart' is never invoked and polkit never granted for it).
    assert ctl.calls == [("stop", COMFY), ("start", COMFY)]
    # HTTP health was verified against the ComfyUI health URL.
    assert COMFY_HEALTH in health.calls
    final = {item["id"]: item["state"] for item in operation["services"]}
    assert final["comfyui"] == "active"


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/services/vllm-deepseek-v4/restart", {}),
        ("post", "/api/services/sglang-qwen38-flash-next/restart", {}),
        ("post", "/api/services/sglang-qwen38-27b/restart", {}),
        ("post", "/api/switch/restart", {}),
        ("post", "/api/services/comfyui/restart/extra", {}),
        ("put", "/api/services/comfyui/restart", {"json": {}}),
        ("delete", "/api/services/comfyui/restart", {}),
    ],
)
def test_no_generic_restart_capability(
    client: TestClient, method: str, path: str, kwargs: dict
) -> None:
    """Only POST /api/services/comfyui/restart restarts; nothing else does."""
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code in {404, 405}
    # And nothing was queued: the manager stays idle for every rejected form.
    assert client.get("/api/status").json()["active_operation"] is None


@pytest.mark.parametrize("state", ["inactive", "failed", "activating", "deactivating", "unknown"])
def test_restart_rejects_comfyui_not_exactly_active(
    client: TestClient, ctl: FakeSystemctl, state: str
) -> None:
    ctl.states[COMFY] = state
    body = _restart(client)
    operation = wait_operation(client, body["operation_id"])
    assert operation["status"] == "failed"
    assert "restart requires the service to be exactly active" in operation["message"]
    # Validation failed before mutation: nothing was ever stopped or started.
    assert ctl.calls == []
    assert [step["status"] for step in operation["steps"]] == ["done", "pending", "pending"]


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "unknown"])
def test_restart_rejects_conflicting_model_not_stopped(
    client: TestClient, ctl: FakeSystemctl, state: str
) -> None:
    """Every model conflicting with ComfyUI must be positively stopped."""
    ctl.states[COMFY] = "active"
    ctl.states[VLLM] = state
    body = _restart(client)
    operation = wait_operation(client, body["operation_id"])
    assert operation["status"] == "failed"
    assert (
        "restart blocked: conflicting model vllm-dsv4-flash-vision is not stopped"
        in operation["message"]
    )
    assert ctl.calls == []
    assert [step["status"] for step in operation["steps"]] == ["done", "pending", "pending"]


def test_restart_tolerates_conflict_free_stopped_states(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    """'inactive'/'failed' conflicting models are known stable non-active states."""
    ctl.states[COMFY] = "active"
    ctl.states[VLLM] = "failed"
    body = _restart(client)
    operation = wait_operation(client, body["operation_id"])
    assert operation["status"] == "succeeded"
    assert ctl.calls == [("stop", COMFY), ("start", COMFY)]


def test_restart_busy_returns_409_like_other_mutations(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    ctl.states[COMFY] = "active"
    ctl.hold = True
    try:
        first = client.put("/api/services/comfyui", json={"active": False})
        assert first.status_code == 202
        first_id = first.json()["operation_id"]
        restart = client.post("/api/services/comfyui/restart")
        assert restart.status_code == 409
        payload = restart.json()
        assert payload["active_operation_id"] == first_id
        assert payload["status_url"] == f"/api/operations/{first_id}"
        assert "target_model_id" not in payload  # the active op is not a switch
    finally:
        ctl.hold = False
    wait_operation(client, first_id)


def test_restart_after_terminal_operation_is_accepted(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    ctl.states[COMFY] = "active"
    first = wait_operation(client, _restart(client)["operation_id"])
    assert first["status"] == "succeeded"
    second = wait_operation(client, _restart(client)["operation_id"])
    assert second["status"] == "succeeded"
    assert ctl.calls == [("stop", COMFY), ("start", COMFY)] * 2


# ---------------------------------------------------------------------------
# Verified success and rollback on post-mutation failure
# ---------------------------------------------------------------------------


def test_restart_fails_when_unit_exits_after_health_verification(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    """A process exiting during verification can never be reported success."""
    app = client.app  # type: ignore[attr-defined]
    ctx = app.state.ctx
    ctl.states[COMFY] = "active"
    ctx.health = ExitAfterHealth(ctl, COMFY)
    try:
        body = _restart(client)
        operation = wait_operation(client, body["operation_id"])
        assert operation["status"] == "failed"
        assert "did not remain active after verification" in operation["message"]
        assert "previous state restored on a best-effort basis" in operation["message"]
        assert [step["status"] for step in operation["steps"]] == ["done", "done", "failed"]
        # Rollback observed the snapshot ('active') and attempted a re-start.
        assert ctl.calls[-1] == ("start", COMFY)
        assert ctl.states[COMFY] == "active"
    finally:
        ctx.health = FakeHealth()


def test_restart_start_failure_rolls_back(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[COMFY] = "active"
    ctl.fail_start_for.add(COMFY)
    body = _restart(client)
    operation = wait_operation(client, body["operation_id"])
    assert operation["status"] == "failed"
    assert "service did not start in time" in operation["message"]
    assert "previous state restored on a best-effort basis" in operation["message"]
    # The stop succeeded and the start was attempted; rollback re-attempted it.
    assert ("stop", COMFY) in ctl.calls
    assert ctl.calls.count(("start", COMFY)) >= 1
    final = {item["id"]: item["state"] for item in operation["services"]}
    assert "comfyui" in final  # observed final states are always reported


class ComfyFailingAfterDrift(FakeSystemctl):
    """Fails the ComfyUI start while an unrelated unit drifts after snapshot.

    The drift happens when the stop command runs - i.e. strictly after the
    operation's snapshot recorded the unrelated models as 'inactive'. A
    whole-topology rollback would issue a stop for the drifted model; the
    ComfyUI-only rollback must not touch it.
    """

    async def run(self, verb: str, unit: str, *, timeout_seconds: float) -> str:
        if verb == "stop" and unit == COMFY:
            self.states[FLASH] = "active"  # rises after the snapshot
            self.states[VLLM] = "deactivating"
            self.fail_start_for.add(COMFY)  # the restart's start then fails
        return await super().run(verb, unit, timeout_seconds=timeout_seconds)


def test_restart_rollback_restores_only_comfyui_after_unrelated_drift(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    """Post-failure restoration issues commands only for the ComfyUI unit."""
    app = client.app  # type: ignore[attr-defined]
    ctx = app.state.ctx
    drifting = ComfyFailingAfterDrift(states={COMFY: "active"})
    ctx.systemctl = drifting
    try:
        body = _restart(client)
        operation = wait_operation(client, body["operation_id"])
        assert operation["status"] == "failed"
        assert "service did not start in time" in operation["message"]
        assert "previous state restored on a best-effort basis" in operation["message"]
        # Every systemctl command of the whole operation targeted ComfyUI only:
        # stop, the failed start, and the best-effort restore-start.
        assert drifting.calls == [("stop", COMFY), ("start", COMFY), ("start", COMFY)]
        # The drifted unrelated units were left exactly as found: no rollback
        # command touched them even though the snapshot recorded 'inactive'.
        assert drifting.states[FLASH] == "active"
        assert drifting.states[VLLM] == "deactivating"
        # Observed final states still reflect the real world, drift included.
        final = {item["id"]: item["state"] for item in operation["services"]}
        assert final["sglang-qwen38-flash-next"] == "active"
        assert final["vllm-dsv4-flash-vision"] == "deactivating"
    finally:
        ctx.systemctl = ctl


def test_restart_stop_failure_rolls_back(client: TestClient, ctl: FakeSystemctl) -> None:
    """If the unit never reaches a stopped state the operation fails cleanly."""

    class StopRefusing(FakeSystemctl):
        async def run(self, verb: str, unit: str, *, timeout_seconds: float) -> str:
            if verb == "stop" and unit == COMFY:
                self.calls.append((verb, unit))
                self.states[unit] = "deactivating"  # never settles into stopped
                return "ok"
            return await super().run(verb, unit, timeout_seconds=timeout_seconds)

    app = client.app  # type: ignore[attr-defined]
    ctx = app.state.ctx
    refusing = StopRefusing(states={COMFY: "active"})
    ctx.systemctl = refusing
    try:
        body = _restart(client)
        operation = wait_operation(client, body["operation_id"], timeout=60.0)
        assert operation["status"] == "failed"
        assert "service did not stop in time" in operation["message"]
        assert "previous state restored on a best-effort basis" in operation["message"]
        assert [step["status"] for step in operation["steps"]] == ["done", "failed", "pending"]
        # No start was attempted before the stop was verified.
        assert ("start", COMFY) not in refusing.calls[: refusing.calls.index(("stop", COMFY)) + 1]
    finally:
        ctx.systemctl = ctl


# ---------------------------------------------------------------------------
# Runner-level defense: restricted to the companion service id
# ---------------------------------------------------------------------------


def test_runner_rejects_unknown_and_non_companion_services() -> None:
    base = load_config(CONFIG_PATH)
    other = ServiceSpec(
        id="other",
        display_name="Other",
        unit="other.service",
        gpus=(0,),
        synopsis="extra",
        conflicts_with=(),
        health_url=None,
    )
    config = dataclasses.replace(base, services=(*base.services, other))
    ctl = FakeSystemctl(states={COMFY: "active", "other.service": "active"})
    ctx = AppContext(
        config=config, systemctl=ctl, health=FakeHealth(), manager=OperationManager()
    )

    async def scenario() -> None:
        for service_id, expected in (
            ("other", "restart is only supported for the comfyui service"),
            ("does-not-exist", "unknown service"),
        ):
            operation = Operation(
                id=f"op-{service_id}",
                kind="service-restart",
                params={"service_id": service_id},
                steps=[Step(name=name) for name in SERVICE_RESTART_STEPS],
            )
            with pytest.raises(ServiceStepError) as excinfo:
                await run_service_restart(ctx, operation)
            assert excinfo.value.kind == expected
            # Defensive rejections happen before any systemctl mutation call.
            assert ctl.calls == []

    asyncio.run(scenario())


def test_origin_policy_applies_to_restart(client: TestClient) -> None:
    bad = client.post(
        "/api/services/comfyui/restart", headers={"Origin": "https://evil.example"}
    )
    assert bad.status_code == 403
    ok = client.post(
        "/api/services/comfyui/restart", headers={"Origin": "http://testserver"}
    )
    assert ok.status_code == 202
    operation = wait_operation(client, ok.json()["operation_id"])
    assert operation["status"] == "failed"  # comfyui is inactive in this fixture
    assert TEST_TOKEN  # token mode is exercised by the shared client fixture
