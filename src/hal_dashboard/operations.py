"""Operation orchestration: single-flight mutations with ordered, verified steps.

Exactly one mutating operation may run at a time; all mutations share one
asyncio lock. A switch follows the ordered procedure: snapshot unit states,
validate conflicts/override, stop every other model unit and every
incompatible auxiliary that is not positively stopped (``inactive``/``failed``;
this includes ``unknown``, ``activating`` and ``deactivating``) and verify it
stopped, start the selected model and verify active (and optionally healthy),
then bring the companion service to its desired state and verify. A no-op is
allowed only when every non-selected model and blocker is positively stopped.
Any failure triggers a best-effort rollback to the snapshot,
after which all states are observed and a sanitized failure is reported.
Success is never inferred from an exit code alone - every transition is
verified by observing the unit state.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from hal_dashboard.config import AppConfig, ModelSpec, ServiceSpec

__all__ = [
    "AppContext",
    "BusyError",
    "Operation",
    "OperationManager",
    "ServiceStepError",
    "run_service_toggle",
    "run_switch",
]

COMPANION_SERVICE_ID = "comfyui"

_ACTIVE_STATES = frozenset({"active", "activating"})
_STOPPED_STATES = frozenset({"inactive", "failed"})
_STOP_COMMAND_SECONDS = 60.0
_STOP_VERIFY_SECONDS = 30.0
_AUX_COMMAND_SECONDS = 120.0
_AUX_VERIFY_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.2
_MAX_START_COMMAND_SECONDS = 900.0


class SystemctlLike(Protocol):
    async def state(self, unit: str) -> str: ...

    async def run(self, verb: str, unit: str, *, timeout_seconds: float) -> str: ...


class HealthLike(Protocol):
    async def check(self, url: str) -> bool | None: ...


class ServiceStepError(Exception):
    """Sanitized operation failure. Only fixed, safe category strings."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass
class Step:
    name: str
    status: str = "pending"  # pending | running | done | failed | skipped

    def public(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Operation:
    id: str
    kind: str  # "switch" | "service"
    params: dict[str, Any]
    status: str = "queued"  # queued | running | succeeded | failed
    created_at: str = field(default_factory=_utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    current_step: str | None = None
    message: str | None = None
    steps: list[Step] = field(default_factory=list)
    services: list[dict[str, Any]] | None = None

    def begin_step(self, name: str) -> None:
        self.current_step = name
        for step in self.steps:
            if step.name == name:
                step.status = "running"

    def complete_step(self, name: str, status: str = "done") -> None:
        for step in self.steps:
            if step.name == name:
                step.status = status
        if self.current_step == name:
            self.current_step = None

    def fail_step(self, name: str) -> None:
        for step in self.steps:
            if step.name == name:
                step.status = "failed"

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running"}

    def summary(self) -> dict[str, Any]:
        return {
            "operation_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "current_step": self.current_step,
            "status_url": f"/api/operations/{self.id}",
        }

    def public(self) -> dict[str, Any]:
        return {
            "operation_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_step": self.current_step,
            "message": self.message,
            "steps": [step.public() for step in self.steps],
            "services": self.services,
        }


class BusyError(Exception):
    """Raised when a mutation is requested while another one is in flight."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"operation {operation_id} is already running")
        self.operation_id = operation_id


@dataclass
class AppContext:
    """Wires the configuration and the service-control machinery together.

    Fields are read at operation time, so tests can swap components on the
    live context object.
    """

    config: AppConfig
    systemctl: SystemctlLike
    health: HealthLike
    manager: OperationManager
    probes: Any = None


class OperationManager:
    """In-memory single-flight operation registry (keeps the latest 50)."""

    MAX_HISTORY = 50

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()
        self._active_id: str | None = None

    @property
    def active_operation(self) -> Operation | None:
        if self._active_id is None:
            return None
        return self._ops.get(self._active_id)

    def get(self, operation_id: str) -> Operation | None:
        return self._ops.get(operation_id)

    def submit(
        self,
        kind: str,
        params: dict[str, Any],
        step_names: list[str],
        runner: Callable[[Operation], Awaitable[str]],
    ) -> Operation:
        """Create and start an operation. Raises :class:`BusyError` if one runs."""
        if self._active_id is not None:
            raise BusyError(self._active_id)
        operation = Operation(
            id=uuid.uuid4().hex,
            kind=kind,
            params=dict(params),
            steps=[Step(name=name) for name in step_names],
        )
        self._active_id = operation.id
        self._register(operation)
        asyncio.get_running_loop().create_task(self._execute(operation, runner))
        return operation

    def _register(self, operation: Operation) -> None:
        self._ops[operation.id] = operation
        self._order.append(operation.id)
        while len(self._order) > self.MAX_HISTORY:
            for index, candidate_id in enumerate(self._order):
                candidate = self._ops.get(candidate_id)
                if candidate is not None and not candidate.active:
                    del self._order[index]
                    self._ops.pop(candidate_id, None)
                    break
            else:
                break  # active operation plus MAX_HISTORY-1 newest; nothing droppable

    async def _execute(
        self, operation: Operation, runner: Callable[[Operation], Awaitable[str]]
    ) -> None:
        async with self._lock:
            operation.status = "running"
            operation.started_at = _utcnow()
            try:
                message = await runner(operation)
            except ServiceStepError as exc:
                operation.status = "failed"
                operation.message = exc.kind
                if operation.current_step is not None:
                    operation.fail_step(operation.current_step)
            except Exception:  # noqa: BLE001 - sanitize unexpected internals
                operation.status = "failed"
                operation.message = "operation failed unexpectedly"
                if operation.current_step is not None:
                    operation.fail_step(operation.current_step)
            else:
                operation.status = "succeeded"
                operation.message = message
            finally:
                operation.finished_at = _utcnow()
                operation.current_step = None
                self._active_id = None


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------

SWITCH_STEPS = ["snapshot", "stop-others", "start-model", "companion-service"]
SERVICE_TOGGLE_STEPS = ["snapshot", "apply-state"]


async def _unit_state(ctx: AppContext, unit: str) -> str:
    return await ctx.systemctl.state(unit)


async def _ensure_stopped(ctx: AppContext, unit: str) -> None:
    await ctx.systemctl.run("stop", unit, timeout_seconds=_STOP_COMMAND_SECONDS)
    state = await _poll_state(
        ctx, unit, expect=_STOPPED_STATES, budget_seconds=_STOP_VERIFY_SECONDS
    )
    if state not in _STOPPED_STATES:
        raise ServiceStepError("service did not stop in time")


async def _poll_state(
    ctx: AppContext, unit: str, *, expect: frozenset[str], budget_seconds: float
) -> str:
    state = "unknown"
    deadline = asyncio.get_running_loop().time() + budget_seconds
    while True:
        state = await _unit_state(ctx, unit)
        if state in expect or state not in {"unknown", "activating", "deactivating"}:
            return state
        if asyncio.get_running_loop().time() >= deadline:
            return state
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _start_and_verify_model(ctx: AppContext, model: ModelSpec) -> None:
    command_timeout = min(model.startup_eta_seconds + 120, _MAX_START_COMMAND_SECONDS)
    await ctx.systemctl.run("start", model.unit, timeout_seconds=command_timeout)
    state = await _poll_state(
        ctx,
        model.unit,
        expect=frozenset({"active"}),
        budget_seconds=model.startup_eta_seconds + 60,
    )
    if state != "active":
        raise ServiceStepError("model service did not reach the active state")
    await _verify_health(ctx, model.health_url, budget_seconds=60.0)


async def _start_and_verify_service(ctx: AppContext, service: ServiceSpec) -> None:
    await ctx.systemctl.run("start", service.unit, timeout_seconds=_AUX_COMMAND_SECONDS)
    state = await _poll_state(
        ctx, service.unit, expect=frozenset({"active"}), budget_seconds=_AUX_VERIFY_SECONDS
    )
    if state != "active":
        raise ServiceStepError("service did not start in time")
    await _verify_health(ctx, service.health_url, budget_seconds=30.0)


async def _verify_health(ctx: AppContext, url: str | None, *, budget_seconds: float) -> None:
    if not url:
        return
    deadline = asyncio.get_running_loop().time() + budget_seconds
    while True:
        result = await ctx.health.check(url)
        if result is True:
            return
        if result is False:
            raise ServiceStepError("health endpoint reported failure")
        if asyncio.get_running_loop().time() >= deadline:
            raise ServiceStepError("health endpoint unreachable")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _observe_all(ctx: AppContext) -> list[dict[str, Any]]:
    """Fresh sanitized view of every configured unit (no unit names leaked)."""
    items: list[dict[str, Any]] = []
    for model in ctx.config.models:
        state = await _unit_state(ctx, model.unit)
        items.append(
            {
                "id": model.id,
                "display_name": model.display_name,
                "kind": "model",
                "state": state,
                "healthy": True if state == "active" and model.health_url is None else None,
                "reason": None,
            }
        )
    for service in ctx.config.services:
        state = await _unit_state(ctx, service.unit)
        items.append(
            {
                "id": service.id,
                "display_name": service.display_name,
                "kind": "auxiliary",
                "state": state,
                "healthy": True if state == "active" and service.health_url is None else None,
                "reason": None,
            }
        )
    return items


async def _rollback(ctx: AppContext, snapshot: dict[str, str]) -> None:
    """Best-effort restoration of the snapshot; never raises."""
    for model in ctx.config.models:
        await _restore_unit(ctx, model.unit, snapshot.get(model.unit, "unknown"))
    for service in ctx.config.services:
        await _restore_unit(ctx, service.unit, snapshot.get(service.unit, "unknown"))


async def _restore_unit(ctx: AppContext, unit: str, snapshot_state: str) -> None:
    try:
        current = await _unit_state(ctx, unit)
        if snapshot_state == "active" and current not in _ACTIVE_STATES:
            await ctx.systemctl.run("start", unit, timeout_seconds=_AUX_COMMAND_SECONDS)
        elif snapshot_state in _STOPPED_STATES and current != "inactive":
            # Includes clearing a 'failed' unit back to 'inactive'.
            await ctx.systemctl.run("stop", unit, timeout_seconds=_STOP_COMMAND_SECONDS)
    except Exception:  # noqa: BLE001 - rollback is best effort by definition
        pass


async def run_switch(ctx: AppContext, operation: Operation) -> str:
    """Ordered model switch. Returns a sanitized message or raises ServiceStepError."""
    config = ctx.config
    model = config.model_by_id(str(operation.params["model_id"]))
    if model is None:  # validated at request time; defensive
        raise ServiceStepError("unknown model")
    desired_companion = bool(operation.params["desired_companion"])
    companion = config.service_by_id(COMPANION_SERVICE_ID)

    operation.begin_step("snapshot")
    snapshot: dict[str, str] = {}
    for spec in list(config.models) + list(config.services):
        snapshot[spec.unit] = await _unit_state(ctx, spec.unit)
    operation.complete_step("snapshot")

    # Safety: any non-selected model or conflicting auxiliary that is not
    # positively stopped (stopped means 'inactive' or 'failed') must be
    # stopped and verified - this covers 'unknown', 'activating' and
    # 'deactivating' snapshots.
    others = [
        m.unit
        for m in config.models
        if m.id != model.id and snapshot[m.unit] not in _STOPPED_STATES
    ]
    blockers = [
        service.unit
        for service in config.services
        if model.id in service.conflicts_with and snapshot[service.unit] not in _STOPPED_STATES
    ]
    companion_matches = (
        companion is None
        or (snapshot[companion.unit] in _ACTIVE_STATES) == desired_companion
    )
    if snapshot[model.unit] == "active" and not others and not blockers and companion_matches:
        for name in SWITCH_STEPS[1:]:
            operation.complete_step(name, status="skipped")
        operation.services = await _observe_all(ctx)
        return "desired topology already active; no change required"

    try:
        operation.begin_step("stop-others")
        for unit in others + blockers:
            await _ensure_stopped(ctx, unit)
        operation.complete_step("stop-others")

        operation.begin_step("start-model")
        if snapshot[model.unit] in _ACTIVE_STATES:
            state = await _poll_state(
                ctx,
                model.unit,
                expect=frozenset({"active"}),
                budget_seconds=model.startup_eta_seconds + 60,
            )
            if state != "active":
                raise ServiceStepError("model service did not reach the active state")
        else:
            await _start_and_verify_model(ctx, model)
        await _verify_health(ctx, model.health_url, budget_seconds=60.0)
        operation.complete_step("start-model")

        operation.begin_step("companion-service")
        if companion is not None:
            companion_state = snapshot[companion.unit]
            if desired_companion:
                if companion_state not in _ACTIVE_STATES:
                    await _start_and_verify_service(ctx, companion)
                else:
                    await _verify_health(ctx, companion.health_url, budget_seconds=30.0)
            elif companion_state in _ACTIVE_STATES:
                await _ensure_stopped(ctx, companion.unit)
        operation.complete_step("companion-service")
    except ServiceStepError as exc:
        await _rollback(ctx, snapshot)
        operation.services = await _observe_all(ctx)
        raise ServiceStepError(
            f"{exc.kind}; previous state restored on a best-effort basis"
        ) from exc
    except Exception as exc:
        await _rollback(ctx, snapshot)
        operation.services = await _observe_all(ctx)
        raise ServiceStepError(
            f"operation failed ({type(exc).__name__}); best-effort rollback performed"
        ) from exc

    operation.services = await _observe_all(ctx)
    return f"switched to {model.display_name}"


# ---------------------------------------------------------------------------
# Auxiliary service toggle
# ---------------------------------------------------------------------------


async def run_service_toggle(ctx: AppContext, operation: Operation) -> str:
    """Start/stop one configured auxiliary service, verified and conflict-checked."""
    config = ctx.config
    service = config.service_by_id(str(operation.params["service_id"]))
    if service is None:  # validated at request time; defensive
        raise ServiceStepError("unknown service")
    desired_active = bool(operation.params["active"])

    operation.begin_step("snapshot")
    snapshot: dict[str, str] = {}
    for spec in list(config.models) + list(config.services):
        snapshot[spec.unit] = await _unit_state(ctx, spec.unit)
    operation.complete_step("snapshot")

    try:
        operation.begin_step("apply-state")
        if desired_active:
            conflicting = [
                model.id
                for model in config.models
                if service.id in model.conflicts_with and snapshot[model.unit] in _ACTIVE_STATES
            ]
            if conflicting:
                raise ServiceStepError(
                    "activation blocked: conflicting model " + ", ".join(conflicting) + " is active"
                )
        state = snapshot[service.unit]
        if desired_active:
            if state != "active":
                if state == "activating":
                    verified = await _poll_state(
                        ctx,
                        service.unit,
                        expect=frozenset({"active"}),
                        budget_seconds=_AUX_VERIFY_SECONDS,
                    )
                    if verified != "active":
                        raise ServiceStepError("service did not start in time")
                else:
                    await _start_and_verify_service(ctx, service)
                await _verify_health(ctx, service.health_url, budget_seconds=30.0)
        elif state in _ACTIVE_STATES:
            await _ensure_stopped(ctx, service.unit)
        operation.complete_step("apply-state")
    except ServiceStepError:
        await _rollback(ctx, snapshot)
        operation.services = await _observe_all(ctx)
        raise
    except Exception as exc:
        await _rollback(ctx, snapshot)
        operation.services = await _observe_all(ctx)
        raise ServiceStepError(
            f"operation failed ({type(exc).__name__}); best-effort rollback performed"
        ) from exc

    operation.services = await _observe_all(ctx)
    return service.display_name + (" started" if desired_active else " stopped")
