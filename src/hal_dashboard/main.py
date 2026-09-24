"""FastAPI application factory and routes.

Runtime model: exactly one Uvicorn worker and no --reload. Operations and the
probe cache live in process memory; running multiple workers would split them
and break the single-flight mutation guarantee.

Environment:
  HAL_DASHBOARD_TOKEN           required unless auth is disabled; the shared
                                admin token (X-Hal-Token).
  HAL_DASHBOARD_AUTH_DISABLED   optional opt-in reverse-proxy mode. When set to
                                an accepted true value (1/true/yes/on, case-
                                insensitive) the app requires no token and all
                                API routes work unauthenticated: direct access
                                then grants full admin and must be blocked by
                                network policy / the reverse proxy. Absent/false
                                keeps token auth. Any other value is rejected.
  HAL_DASHBOARD_CONFIG          optional path to the systems TOML file.
  HAL_DASHBOARD_ALLOWED_ORIGINS optional comma-separated exact origins for
                                browser POST/PUT requests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hal_dashboard.auth import (
    TOKEN_HEADER,
    load_auth_disabled,
    load_token,
    origin_allowed,
    parse_allowed_origins,
    token_matches,
)
from hal_dashboard.config import load_config
from hal_dashboard.operations import (
    COMPANION_SERVICE_ID,
    SERVICE_TOGGLE_STEPS,
    SWITCH_STEPS,
    AppContext,
    BusyError,
    OperationManager,
    run_service_toggle,
    run_switch,
)
from hal_dashboard.probes import HealthChecker, Probes, Systemctl

__all__ = ["app", "create_app"]

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "systems.toml"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

#: The only API endpoints reachable without a token when token auth is enabled.
PUBLIC_API_PATHS = frozenset({"/api/health", "/api/auth-mode"})


class SwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    comfyui: bool | None = Field(default=None, strict=True)


class ServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = Field(strict=True)


def _json_error(status: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, **extra})


def create_app(config_path: Path | None = None) -> FastAPI:
    """Build the application. Raises ConfigError/TokenError on bad startup state."""
    path = config_path
    if path is None:
        path = Path(os.environ.get("HAL_DASHBOARD_CONFIG") or _DEFAULT_CONFIG_PATH)
    config = load_config(path)
    auth_disabled = load_auth_disabled()
    token = "" if auth_disabled else load_token()
    allowed_origins = parse_allowed_origins(os.environ.get("HAL_DASHBOARD_ALLOWED_ORIGINS"))

    systemctl = Systemctl()
    app = FastAPI(title="HAL Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    ctx = AppContext(
        config=config,
        systemctl=systemctl,
        health=HealthChecker(),
        manager=OperationManager(),
        probes=Probes(config, systemctl=systemctl),
    )
    app.state.ctx = ctx
    app.state.token = token
    app.state.token_required = not auth_disabled
    app.state.allowed_origins = allowed_origins

    # ------------------------------------------------------------------ #
    # Middleware: token auth, origin policy, security headers.
    # CORS is intentionally disabled (same-origin dashboard only).
    # The browser Origin check runs for every /api POST/PUT in BOTH modes;
    # the token check is skipped only when auth is deliberately disabled.
    # Registration order matters: the LAST registered middleware runs
    # outermost, so 'guard' is registered after 'authenticate' and decorates
    # every response - including authentication rejections - with security
    # headers.
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path.startswith("/api"):
            if request.method in {"POST", "PUT"}:
                origin = request.headers.get("origin")
                if origin is not None and not origin_allowed(
                    origin, app.state.allowed_origins, request.headers.get("host", "")
                ):
                    return _json_error(403, "origin not allowed")
            if app.state.token_required and path not in PUBLIC_API_PATHS:
                provided = request.headers.get("x-hal-token")
                if not token_matches(provided, app.state.token):
                    return _json_error(401, "missing or invalid token")
        return await call_next(request)

    def _apply_security_headers(request: Request, response) -> None:  # type: ignore[no-untyped-def]
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        _apply_security_headers(request, response)
        return response

    # ------------------------------------------------------------------ #
    # Public liveness endpoint
    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------ #
    # Public authentication-mode probe (lets the frontend decide whether to
    # show the token dialog). Minimal by design: never exposes any token.
    # ------------------------------------------------------------------ #
    @app.get("/api/auth-mode")
    async def auth_mode() -> dict[str, bool]:
        return {"token_required": bool(app.state.token_required)}

    # ------------------------------------------------------------------ #
    # Sanitized UI configuration (no paths, units, commands, or health URLs)
    # ------------------------------------------------------------------ #
    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "server": {
                "name": config.server.name,
                "os": config.server.os_name,
                "ram_gb": config.server.ram_gb,
                "cpu_summary": config.server.cpu_summary,
                "gpu_summary": config.server.gpu_summary,
            },
            "refresh": {
                "choices_seconds": list(config.ui.refresh_choices_seconds),
                "default_seconds": config.ui.default_refresh_seconds,
            },
            "models": [
                {
                    "id": model.id,
                    "display_name": model.display_name,
                    "synopsis": model.synopsis,
                    "strengths": list(model.strengths),
                    "gpus": list(model.gpus),
                    "startup_eta_seconds": model.startup_eta_seconds,
                    "conflicts_with": list(model.conflicts_with),
                    "comfyui_default": model.comfyui_default,
                    "allow_comfyui_override": model.allow_comfyui_override,
                }
                for model in config.models
            ],
            "services": [
                {
                    "id": service.id,
                    "display_name": service.display_name,
                    "synopsis": service.synopsis,
                    "gpus": list(service.gpus),
                    "conflicts_with": list(service.conflicts_with),
                }
                for service in config.services
            ],
            "capabilities": {
                "endpoints": [
                    "GET /api/health",
                    "GET /api/auth-mode",
                    "GET /api/config",
                    "GET /api/status",
                    "POST /api/switch",
                    "PUT /api/services/{service_id}",
                    "GET /api/operations/{operation_id}",
                ],
                "auth_header": TOKEN_HEADER,
                "token_required": bool(app.state.token_required),
                "cors_enabled": False,
                "single_uvicorn_worker": True,
                "max_operations_retained": OperationManager.MAX_HISTORY,
            },
        }

    # ------------------------------------------------------------------ #
    # Status snapshot
    # ------------------------------------------------------------------ #
    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        probes = ctx.probes
        raw = await probes.snapshot()
        service_items = [
            {key: value for key, value in item.items() if key != "unit"}
            for item in raw["services"]["items"]
        ]
        active_model_ids = [
            item["id"]
            for item in service_items
            if item["kind"] == "model" and item["state"] == "active"
        ]
        conflict_warnings = _conflict_warnings(service_items)
        active = ctx.manager.active_operation
        return {
            "cpu": raw["cpu"],
            "memory": raw["memory"],
            "storage_root": raw["storage_root"],
            "gpus": raw["gpus"],
            "gpu_allocation": {
                "models": {model.id: list(model.gpus) for model in config.models},
                "services": {service.id: list(service.gpus) for service in config.services},
            },
            "services": {"items": service_items, "reason": raw["services"]["reason"]},
            "active_model_ids": active_model_ids,
            "conflict_warning": ({"warnings": conflict_warnings} if conflict_warnings else None),
            "active_operation": active.summary() if active is not None else None,
        }

    def _conflict_warnings(service_items: list[dict[str, Any]]) -> list[str]:
        state_by_id = {item["id"]: item["state"] for item in service_items}
        warnings: list[str] = []
        active_models = [
            model.id
            for model in config.models
            if state_by_id.get(model.id) in {"active", "activating"}
        ]
        if len(active_models) > 1:
            warnings.append(
                "multiple model services are active at once: " + ", ".join(active_models)
            )
        for service in config.services:
            if state_by_id.get(service.id) in {"active", "activating"}:
                for model_id in service.conflicts_with:
                    if state_by_id.get(model_id) in {"active", "activating"}:
                        warnings.append(
                            f"service '{service.id}' conflicts with model '{model_id}'"
                            " but both are active"
                        )
        return warnings

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def _busy_response(exc: BusyError) -> JSONResponse:
        return _json_error(
            409,
            "another operation is already running",
            active_operation_id=exc.operation_id,
            status_url=f"/api/operations/{exc.operation_id}",
        )

    def _accepted(operation_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content={"operation_id": operation_id, "status_url": f"/api/operations/{operation_id}"},
        )

    @app.post("/api/switch")
    async def switch(request: Request) -> JSONResponse:
        try:
            body = SwitchRequest.model_validate(await request.json())
        except ValidationError:
            return _json_error(400, "invalid switch request body")
        except Exception:  # noqa: BLE001 - malformed JSON etc.
            return _json_error(400, "request body must be JSON")
        model = config.model_by_id(body.model_id)
        if model is None:
            return _json_error(400, f"unknown model_id '{body.model_id}'")
        if body.comfyui is not None and not model.allow_comfyui_override:
            return _json_error(400, f"model '{model.id}' does not allow the comfyui override")
        desired_companion = model.comfyui_default if body.comfyui is None else body.comfyui
        if desired_companion and COMPANION_SERVICE_ID in model.conflicts_with:
            return _json_error(400, f"model '{model.id}' cannot run together with comfyui")
        try:
            operation = ctx.manager.submit(
                "switch",
                {"model_id": model.id, "desired_companion": desired_companion},
                SWITCH_STEPS,
                lambda op: run_switch(ctx, op),
            )
        except BusyError as exc:
            return _busy_response(exc)
        return _accepted(operation.id)

    @app.put("/api/services/{service_id}")
    async def set_service(service_id: str, request: Request) -> JSONResponse:
        try:
            body = ServiceRequest.model_validate(await request.json())
        except ValidationError:
            return _json_error(400, "invalid service request body")
        except Exception:  # noqa: BLE001
            return _json_error(400, "request body must be JSON")
        service = config.service_by_id(service_id)
        if service is None:
            if config.model_by_id(service_id) is not None:
                return _json_error(400, f"'{service_id}' is a model; use POST /api/switch")
            return _json_error(404, f"unknown service '{service_id}'")
        if body.active:
            conflicting_models = [
                model
                for model in config.models
                if service.id in model.conflicts_with
            ]
            active_conflicts: list[str] = []
            for model in conflicting_models:
                state = await ctx.systemctl.state(model.unit)
                if state in {"active", "activating"}:
                    active_conflicts.append(model.id)
            if active_conflicts:
                return _json_error(
                    409,
                    "a conflicting model is active",
                    conflicts_with=active_conflicts,
                )
        try:
            operation = ctx.manager.submit(
                "service",
                {"service_id": service.id, "active": body.active},
                SERVICE_TOGGLE_STEPS,
                lambda op: run_service_toggle(ctx, op),
            )
        except BusyError as exc:
            return _busy_response(exc)
        return _accepted(operation.id)

    @app.get("/api/operations/{operation_id}")
    async def get_operation(operation_id: str) -> JSONResponse:
        operation = ctx.manager.get(operation_id)
        if operation is None:
            return _json_error(404, "unknown operation")
        return JSONResponse(content=operation.public())

    # ------------------------------------------------------------------ #
    # Static frontend (served by the sibling frontend workstream). The
    # catch-all is registered last so /api routes always win; /api paths
    # never fall through to static.
    # ------------------------------------------------------------------ #
    if (_STATIC_DIR / "index.html").is_file():

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise NotFoundApi
            candidate = (_STATIC_DIR / full_path).resolve()
            if (
                full_path
                and candidate.is_file()
                and candidate.is_relative_to(_STATIC_DIR.resolve())
            ):
                return FileResponse(candidate)
            return FileResponse(_STATIC_DIR / "index.html")

        class NotFoundApi(Exception):
            pass

        @app.exception_handler(NotFoundApi)
        async def _not_found_api(request: Request, exc: NotFoundApi) -> JSONResponse:
            return _json_error(404, "unknown API endpoint")

    return app


app = create_app()
