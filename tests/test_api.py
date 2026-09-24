"""API behavior tests: auth, origin policy, sanitization, switch/toggle flows."""

from __future__ import annotations

import asyncio
import time
import tomllib
from pathlib import Path

import pytest
from conftest import CONFIG_PATH, TEST_TOKEN, DynamicProbes, FakeHealth, FakeSystemctl
from fastapi.testclient import TestClient

import hal_dashboard
from hal_dashboard.auth import TokenError
from hal_dashboard.config import ConfigError
from hal_dashboard.main import create_app
from hal_dashboard.probes import Probes

VLLM = "vllm-deepseek-v4.service"
FLASH = "sglang-qwen38-flash-next.service"
B27 = "sglang-qwen38-27b.service"
COMFY = "comfyui.service"


def wait_operation(client: TestClient, operation_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/operations/{operation_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"operation {operation_id} did not finish in time")


# ---------------------------------------------------------------------------
# Auth & health
# ---------------------------------------------------------------------------


def test_health_is_public_and_minimal(anon_client: TestClient) -> None:
    response = anon_client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_protected_routes_require_token(anon_client: TestClient) -> None:
    for path in ("/api/config", "/api/status", "/api/operations/whatever"):
        assert anon_client.get(path).status_code == 401
    assert anon_client.post("/api/switch", json={"model_id": "x"}).status_code == 401
    assert anon_client.put("/api/services/comfyui", json={"active": True}).status_code == 401


def test_wrong_token_is_rejected(client: TestClient) -> None:
    response = client.get("/api/status", headers={"X-Hal-Token": "wrong-token-value"})
    assert response.status_code == 401


def test_token_env_rejects_missing_and_example_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAL_DASHBOARD_TOKEN", "")
    with pytest.raises(TokenError):
        create_app(CONFIG_PATH)
    for placeholder in (
        "changeme",
        "REPLACE_WITH_A_STRONG_TOKEN",
        "__REPLACE_ME__",
        "example-token",
    ):
        monkeypatch.setenv("HAL_DASHBOARD_TOKEN", placeholder)
        with pytest.raises(TokenError):
            create_app(CONFIG_PATH)


def test_token_env_enforces_minimum_length(monkeypatch: pytest.MonkeyPatch) -> None:
    # Anything below the 32-character minimum (after stripping) is rejected...
    for short in ("s" * 31, "  " + "t" * 28 + "  "):
        monkeypatch.setenv("HAL_DASHBOARD_TOKEN", short)
        with pytest.raises(TokenError):
            create_app(CONFIG_PATH)
    # ...exactly 32 characters is accepted.
    monkeypatch.setenv("HAL_DASHBOARD_TOKEN", "a" * 32)
    create_app(CONFIG_PATH)


def test_invalid_config_file_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "broken.toml"
    bad.write_text("this is not = valid = toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        create_app(bad)


# ---------------------------------------------------------------------------
# Authentication modes (token default vs. reverse-proxy opt-in)
# ---------------------------------------------------------------------------


def _build(monkeypatch: pytest.MonkeyPatch, ctl: FakeSystemctl, health: FakeHealth):
    """Fresh app with fakes wired (the auth-mode flag is whatever the test set)."""
    application = create_app(CONFIG_PATH)
    ctx = application.state.ctx
    ctx.systemctl = ctl
    ctx.health = health
    ctx.probes = DynamicProbes(ctx.config, ctl, health)
    return application


def test_auth_mode_is_public_and_minimal(anon_client: TestClient) -> None:
    response = anon_client.get("/api/auth-mode")
    assert response.status_code == 200
    # Exact, minimal body: only the boolean, no token, nothing else.
    assert response.json() == {"token_required": True}


def test_only_health_and_auth_mode_are_public(anon_client: TestClient) -> None:
    assert anon_client.get("/api/health").status_code == 200
    assert anon_client.get("/api/auth-mode").status_code == 200
    assert anon_client.get("/api/health").json() == {"status": "ok"}
    for path in ("/api/config", "/api/status", "/api/operations/whatever"):
        assert anon_client.get(path).status_code == 401
    assert anon_client.post("/api/switch", json={"model_id": "x"}).status_code == 401
    assert anon_client.put("/api/services/comfyui", json={"active": True}).status_code == 401


def test_default_auth_mode_capabilities(client: TestClient) -> None:
    caps = client.get("/api/config").json()["capabilities"]
    assert caps["token_required"] is True
    assert "GET /api/auth-mode" in caps["endpoints"]


@pytest.mark.parametrize(
    "raw", ["1", "true", "TRUE", "Yes", "on", "  true  ", "0", "false", "no", "off", "", "   "]
)
def test_auth_disabled_flag_accepted_false_and_true_values(
    monkeypatch: pytest.MonkeyPatch, ctl: FakeSystemctl, health: FakeHealth, raw: str
) -> None:
    monkeypatch.delenv("HAL_DASHBOARD_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("HAL_DASHBOARD_AUTH_DISABLED", raw)
    application = _build(monkeypatch, ctl, health)
    expected_required = raw.strip().lower() in {"", "0", "false", "no", "off"}
    assert application.state.token_required is expected_required
    # In disabled mode the token is never loaded or stored.
    assert (application.state.token != "") is expected_required


@pytest.mark.parametrize("raw", ["maybe", "2", "tru", "ture", "none", "true1", "01", "-1"])
def test_invalid_auth_disabled_flag_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("HAL_DASHBOARD_AUTH_DISABLED", raw)
    with pytest.raises(TokenError):
        create_app(CONFIG_PATH)


def test_auth_disabled_token_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # In reverse-proxy mode startup must not require a valid token: clear it and
    # the app still builds (a blank token would otherwise raise TokenError).
    monkeypatch.setenv("HAL_DASHBOARD_AUTH_DISABLED", "true")
    monkeypatch.setenv("HAL_DASHBOARD_TOKEN", "")
    application = create_app(CONFIG_PATH)
    assert application.state.token_required is False
    assert application.state.token == ""


def test_auth_disabled_allows_api_without_token(
    monkeypatch: pytest.MonkeyPatch, ctl: FakeSystemctl, health: FakeHealth
) -> None:
    monkeypatch.setenv("HAL_DASHBOARD_AUTH_DISABLED", "true")
    monkeypatch.delenv("HAL_DASHBOARD_TOKEN", raising=False)
    application = create_app(CONFIG_PATH)
    ctx = application.state.ctx
    ctx.systemctl = ctl
    ctx.health = health
    ctx.probes = DynamicProbes(ctx.config, ctl, health)
    with TestClient(application) as client:  # no X-Hal-Token header at all
        assert client.get("/api/auth-mode").json() == {"token_required": False}
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/config").status_code == 200
        caps = client.get("/api/config").json()["capabilities"]
        assert caps["token_required"] is False
        assert client.get("/api/status").status_code == 200
        response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
        assert response.status_code == 202
        operation = wait_operation(client, response.json()["operation_id"])
        assert operation["status"] == "succeeded"
        assert client.get("/api/operations/does-not-exist").status_code == 404


def test_origin_still_enforced_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch, ctl: FakeSystemctl, health: FakeHealth
) -> None:
    monkeypatch.setenv("HAL_DASHBOARD_AUTH_DISABLED", "true")
    application = create_app(CONFIG_PATH)
    ctx = application.state.ctx
    ctx.systemctl = ctl
    ctx.health = health
    ctx.probes = DynamicProbes(ctx.config, ctl, health)
    payload = {"model_id": "sglang-qwen38-27b"}
    with TestClient(application) as client:  # no token header
        bad = client.post("/api/switch", json=payload, headers={"Origin": "https://evil.example"})
        assert bad.status_code == 403
        same = client.post("/api/switch", json=payload, headers={"Origin": "http://testserver"})
        assert same.status_code == 202
        wait_operation(client, same.json()["operation_id"])
        plain = client.post("/api/switch", json=payload)  # no Origin -> allowed
        assert plain.status_code == 202
        wait_operation(client, plain.json()["operation_id"])
        # PUT origin policy holds too.
        bad_put = client.put(
            "/api/services/comfyui", json={"active": True}, headers={"Origin": "https://evil.example"}
        )
        assert bad_put.status_code == 403


# ---------------------------------------------------------------------------
# Security headers / CORS
# ---------------------------------------------------------------------------


def test_security_headers_on_api_and_errors(client: TestClient, anon_client: TestClient) -> None:
    response = client.get("/api/config")
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]
    # 401 responses are decorated too.
    unauthorized = anon_client.get("/api/status")
    assert unauthorized.status_code == 401
    assert "x-content-type-options" in unauthorized.headers
    assert "no-store" in unauthorized.headers["cache-control"]


def test_cors_is_disabled(client: TestClient) -> None:
    response = client.request("OPTIONS", "/api/status")
    assert "access-control-allow-origin" not in response.headers
    get_response = client.get("/api/config")
    assert "access-control-allow-origin" not in get_response.headers


# ---------------------------------------------------------------------------
# Origin policy for mutating requests
# ---------------------------------------------------------------------------


def test_origin_must_match_allowlist_or_host(client: TestClient) -> None:
    payload = {"model_id": "sglang-qwen38-27b", "comfyui": None}
    bad = client.post("/api/switch", json=payload, headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    # Host-derived same origin is accepted (TestClient uses Host: testserver).
    same = client.post("/api/switch", json=payload, headers={"Origin": "http://testserver"})
    assert same.status_code == 202
    # No Origin header at all: token alone decides (e.g. curl).
    plain = client.post("/api/switch", json=payload)
    assert plain.status_code == 202


def test_configured_allowed_origin_is_accepted(
    monkeypatch: pytest.MonkeyPatch, ctl: FakeSystemctl
) -> None:
    monkeypatch.setenv("HAL_DASHBOARD_ALLOWED_ORIGINS", "https://hal.lan.example,https://other.example")
    application = create_app(CONFIG_PATH)
    ctx = application.state.ctx
    ctx.systemctl = ctl
    ctx.health = FakeHealth()
    ctx.probes = DynamicProbes(ctx.config, ctl, ctx.health)
    with TestClient(application, headers={"X-Hal-Token": TEST_TOKEN}) as client:
        ok = client.post(
            "/api/switch",
            json={"model_id": "sglang-qwen38-27b"},
            headers={"Origin": "https://hal.lan.example"},
        )
        assert ok.status_code == 202
        trailing = client.post(
            "/api/switch",
            json={"model_id": "sglang-qwen38-27b"},
            headers={"Origin": "https://other.example/"},
        )
        assert trailing.status_code == 202
        bad = client.post(
            "/api/switch",
            json={"model_id": "sglang-qwen38-27b"},
            headers={"Origin": "https://hal.lan"},
        )
        assert bad.status_code == 403


# ---------------------------------------------------------------------------
# /api/config sanitization
# ---------------------------------------------------------------------------


def test_config_payload_is_sanitized(client: TestClient) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    text = response.text
    payload = response.json()
    # No filesystem paths, systemd units, or health URLs leak to the UI.
    for needle in (".service", "health_url", "/home", "systemctl", "http://127"):
        assert needle not in text, needle
    ids = [model["id"] for model in payload["models"]]
    assert ids == ["vllm-dsv4-flash-vision", "sglang-qwen38-flash-next", "sglang-qwen38-27b"]
    assert payload["server"]["name"] == "Hal"
    assert payload["server"]["ram_gb"] == 256
    assert payload["refresh"]["default_seconds"] in payload["refresh"]["choices_seconds"]
    assert payload["capabilities"]["cors_enabled"] is False
    assert payload["capabilities"]["single_uvicorn_worker"] is True
    vllm = payload["models"][0]
    assert vllm["gpus"] == [0, 1]
    assert vllm["allow_comfyui_override"] is False
    assert vllm["comfyui_default"] is False
    assert any(s["id"] == "comfyui" for s in payload["services"])


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------


def test_status_reports_values_nulls_and_conflicts(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[VLLM] = "active"
    ctl.states[COMFY] = "active"
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["cpu"]["percent"] == 12.5
    assert payload["cpu"]["reason"] is None
    assert payload["memory"]["total_bytes"] == 256_000_000_000
    # Null + reason instead of a fake zero, exactly as the probe reported.
    assert payload["storage_root"]["reason"] == "storage probe partially unavailable"
    assert payload["gpus"]["items"][0]["utilization_percent"] is None
    assert payload["gpus"]["reason"] == "some gpu metrics were unavailable"

    items = payload["services"]["items"]
    for item in items:
        assert "unit" not in item
    assert payload["active_model_ids"] == ["vllm-dsv4-flash-vision"]
    warning = payload["conflict_warning"]
    assert warning is not None
    assert any("vllm-dsv4-flash-vision" in w and "comfyui" in w for w in warning["warnings"])

    assert payload["gpu_allocation"]["models"]["vllm-dsv4-flash-vision"] == [0, 1]
    assert payload["gpu_allocation"]["services"]["comfyui"] == [1]
    assert payload["active_operation"] is None


def test_status_no_conflict_when_topology_is_consistent(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    ctl.states[B27] = "active"
    ctl.states[COMFY] = "active"
    payload = client.get("/api/status").json()
    assert payload["conflict_warning"] is None
    assert payload["active_model_ids"] == ["sglang-qwen38-27b"]


# ---------------------------------------------------------------------------
# POST /api/switch
# ---------------------------------------------------------------------------


def test_switch_rejects_unknown_model_and_bad_bodies(client: TestClient) -> None:
    assert client.post("/api/switch", json={"model_id": "nope"}).status_code == 400
    assert client.post("/api/switch", json={"model_id": "nope", "extra": 1}).status_code == 400
    assert client.post("/api/switch", json={"model_id": 7}).status_code == 400
    assert client.post("/api/switch", content=b"not-json").status_code == 400


def test_switch_vllm_cannot_run_comfyui(client: TestClient) -> None:
    response = client.post(
        "/api/switch", json={"model_id": "vllm-dsv4-flash-vision", "comfyui": True}
    )
    assert response.status_code == 400
    # The model forbids the override entirely, even to repeat its default.
    explicit_false = client.post(
        "/api/switch", json={"model_id": "vllm-dsv4-flash-vision", "comfyui": False}
    )
    assert explicit_false.status_code == 400
    # Without an override the model's declarative default applies and works.
    default = client.post("/api/switch", json={"model_id": "vllm-dsv4-flash-vision"})
    assert default.status_code == 202


def test_switch_full_ordered_procedure(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[VLLM] = "active"
    ctl.states[COMFY] = "active"
    response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
    assert response.status_code == 202
    body = response.json()
    assert body["status_url"] == f"/api/operations/{body['operation_id']}"

    operation = wait_operation(client, body["operation_id"])
    assert operation["status"] == "succeeded"
    assert [step["status"] for step in operation["steps"]] == ["done", "done", "done", "done"]
    assert ("stop", VLLM) in ctl.calls
    assert ("start", B27) in ctl.calls
    assert ctl.states[B27] == "active"
    assert ctl.states[VLLM] == "inactive"
    # comfyui default is true for this model and it was already running.
    assert ("start", COMFY) not in ctl.calls
    final_ids = {item["id"]: item["state"] for item in operation["services"]}
    assert final_ids["sglang-qwen38-27b"] == "active"
    assert final_ids["vllm-dsv4-flash-vision"] == "inactive"
    assert final_ids["comfyui"] == "active"


def test_switch_starts_companion_when_default_true(client: TestClient, ctl: FakeSystemctl) -> None:
    response = client.post("/api/switch", json={"model_id": "sglang-qwen38-flash-next"})
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("start", FLASH) in ctl.calls
    assert ("start", COMFY) in ctl.calls


def test_switch_companion_override_false_stops_it(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[FLASH] = "active"
    ctl.states[COMFY] = "active"
    response = client.post(
        "/api/switch", json={"model_id": "sglang-qwen38-flash-next", "comfyui": False}
    )
    assert response.status_code == 202
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("stop", COMFY) in ctl.calls
    assert ctl.states[COMFY] == "inactive"


def test_switch_stops_incompatible_auxiliaries(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[B27] = "active"
    ctl.states[COMFY] = "active"
    response = client.post("/api/switch", json={"model_id": "vllm-dsv4-flash-vision"})
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("stop", B27) in ctl.calls
    assert ("stop", COMFY) in ctl.calls
    assert ("start", VLLM) in ctl.calls
    assert ctl.states[COMFY] == "inactive"


def test_switch_stops_deactivating_competing_model(client: TestClient, ctl: FakeSystemctl) -> None:
    # A model still 'deactivating' is not positively stopped: the switch must
    # stop and verify it before starting the selected model (no silent no-op).
    ctl.states[FLASH] = "deactivating"
    response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("stop", FLASH) in ctl.calls
    assert ctl.states[FLASH] == "inactive"
    assert ctl.states[B27] == "active"


def test_switch_stops_unknown_competing_model(client: TestClient, ctl: FakeSystemctl) -> None:
    # An 'unknown' competing model state must be stopped and verified, never
    # treated as already stopped.
    ctl.states[VLLM] = "unknown"
    response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("stop", VLLM) in ctl.calls
    assert ctl.states[VLLM] == "inactive"
    assert ctl.states[B27] == "active"


def test_switch_stops_transitional_conflicting_auxiliary(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    # A conflicting auxiliary in a transitional state is a blocker too.
    ctl.states[COMFY] = "deactivating"
    response = client.post("/api/switch", json={"model_id": "vllm-dsv4-flash-vision"})
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("stop", COMFY) in ctl.calls
    assert ctl.states[COMFY] == "inactive"
    assert ctl.states[VLLM] == "active"


def test_switch_noop_when_topology_matches(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[B27] = "active"
    ctl.states[COMFY] = "active"
    response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
    assert response.status_code == 202
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert "no change required" in (operation["message"] or "")
    assert ctl.calls == []  # nothing was started or stopped


def test_switch_failure_rolls_back_best_effort(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[VLLM] = "active"
    ctl.fail_start_for.add(B27)
    response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "failed"
    assert operation["message"]  # sanitized category, populated by the runner
    assert "did not reach the active state" in operation["message"]
    # The previously-active model was restored by rollback.
    assert ("start", VLLM) in ctl.calls
    assert ctl.states[VLLM] == "active"
    assert ctl.states[B27] == "inactive"
    final = {item["id"]: item["state"] for item in operation["services"]}
    assert final["vllm-dsv4-flash-vision"] == "active"


def test_busy_returns_409_with_active_operation_id(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.hold = True
    try:
        first = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
        assert first.status_code == 202
        first_id = first.json()["operation_id"]
        second = client.post("/api/switch", json={"model_id": "sglang-qwen38-flash-next"})
        assert second.status_code == 409
        assert second.json()["active_operation_id"] == first_id
        blocked_put = client.put("/api/services/comfyui", json={"active": False})
        assert blocked_put.status_code == 409
    finally:
        ctl.hold = False
    wait_operation(client, first_id)
    third = client.post("/api/switch", json={"model_id": "sglang-qwen38-flash-next"})
    assert third.status_code == 202
    wait_operation(client, third.json()["operation_id"])


# ---------------------------------------------------------------------------
# PUT /api/services/{service_id}
# ---------------------------------------------------------------------------


def test_service_put_validation(client: TestClient) -> None:
    assert client.put("/api/services/comfyui", json={"active": "yes"}).status_code == 400
    assert client.put("/api/services/unknown-thing", json={"active": True}).status_code == 404
    model_id = client.put("/api/services/sglang-qwen38-27b", json={"active": True})
    assert model_id.status_code == 400


def test_service_activation_blocked_by_conflicting_model(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    ctl.states[VLLM] = "active"
    response = client.put("/api/services/comfyui", json={"active": True})
    assert response.status_code == 409
    assert response.json()["conflicts_with"] == ["vllm-dsv4-flash-vision"]
    ctl.states[VLLM] = "inactive"
    ok = client.put("/api/services/comfyui", json={"active": True})
    assert ok.status_code == 202
    operation = wait_operation(client, ok.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ("start", COMFY) in ctl.calls


def test_service_stop(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.states[COMFY] = "active"
    response = client.put("/api/services/comfyui", json={"active": False})
    assert response.status_code == 202
    operation = wait_operation(client, response.json()["operation_id"])
    assert operation["status"] == "succeeded"
    assert ctl.states[COMFY] == "inactive"


def test_status_exposes_active_operation(client: TestClient, ctl: FakeSystemctl) -> None:
    ctl.hold = True
    try:
        response = client.post("/api/switch", json={"model_id": "sglang-qwen38-27b"})
        operation_id = response.json()["operation_id"]
        deadline = time.monotonic() + 5.0
        active = None
        while time.monotonic() < deadline:
            payload = client.get("/api/status").json()
            if payload["active_operation"] is not None:
                active = payload["active_operation"]
                break
            time.sleep(0.02)
        assert active is not None
        assert active["operation_id"] == operation_id
        assert active["status"] in {"queued", "running"}
        assert active["status_url"] == f"/api/operations/{operation_id}"
    finally:
        ctl.hold = False


def test_unknown_operation_is_404_and_history_is_capped(
    client: TestClient, ctl: FakeSystemctl
) -> None:
    assert client.get("/api/operations/does-not-exist").status_code == 404
    first_id = None
    for index in range(55):
        response = client.put("/api/services/comfyui", json={"active": bool(index % 2)})
        assert response.status_code == 202
        operation_id = response.json()["operation_id"]
        wait_operation(client, operation_id)
        if index == 0:
            first_id = operation_id
    assert first_id is not None
    assert client.get(f"/api/operations/{first_id}").status_code == 404


def test_unknown_api_path_is_404(client: TestClient) -> None:
    response = client.get("/api/definitely-not-here")
    assert response.status_code == 404


STATIC_FILES = ("index.html", "styles.css", "app.js")


def test_static_frontend_is_packaged_and_served(client: TestClient) -> None:
    """The frontend ships inside the package and is served by the app."""
    # Packaging: pyproject declares every static asset as package data.
    pyproject = tomllib.loads(
        (Path(hal_dashboard.__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    package_data = pyproject["tool"]["setuptools"]["package-data"]["hal_dashboard"]
    for name in STATIC_FILES:
        assert f"static/{name}" in package_data

    # The assets exist next to the installed/imported package module.
    static_dir = Path(hal_dashboard.__file__).resolve().parent / "static"
    for name in STATIC_FILES:
        assert (static_dir / name).is_file()

    # Static serving works without any network access (in-process TestClient).
    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "review-dialog" in index.text
    app_js = client.get("/app.js")
    assert app_js.status_code == 200
    assert "hal-dashboard-token" in app_js.text
    styles = client.get("/styles.css")
    assert styles.status_code == 200
    assert "}" in styles.text


# ---------------------------------------------------------------------------
# Probe cache (coalesced, ~2s TTL)
# ---------------------------------------------------------------------------


class SyncCtl:
    def __init__(self) -> None:
        self.count = 0

    def _state_sync(self, unit: str) -> str:
        self.count += 1
        return "inactive"


def test_probe_snapshot_is_cached_and_renewed() -> None:
    from hal_dashboard.config import load_config

    config = load_config(CONFIG_PATH)
    ctl = SyncCtl()
    probes = Probes(config, systemctl=ctl, ttl_seconds=0.3)

    async def scenario() -> None:
        await probes.snapshot()
        first = ctl.count
        assert first > 0
        await probes.snapshot()
        assert ctl.count == first  # served from cache
        await asyncio.sleep(0.35)
        await probes.snapshot()
        assert ctl.count > first  # cache expired, probed again

    asyncio.run(scenario())
