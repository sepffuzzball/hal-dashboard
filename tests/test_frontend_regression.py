"""Static regression guards for the redesigned dashboard frontend.

These are lightweight, intent-level assertions on the shipped static assets
rather than brittle full-source snapshots. They pin the behavior the product
depends on:

  * redundant section titles must be sr-only (not rendered as visible text),
  * the operation overlay must keep proper dialog/a11y semantics and a backdrop
    that is hidden from assistive tech,
  * app.js must toggle a body overlay class, capture/restore focus, and only
    allow dismissing the overlay in terminal operation states,
  * the ComfyUI controls must expose Auto/On/Off with aria-pressed, reconcile
    Auto from the model's declarative ``comfyui_default``, and reset back to
    Auto whenever a model switch is submitted,
  * the build id must never drift from the versioned asset URLs in the HTML.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "hal_dashboard" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "styles.css"

BUILD_ID = "20260926-3"


def _function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_redundant_section_titles_are_sr_only() -> None:
    """The visible duplicate headings are hidden visually-only (sr-only)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for heading in (
        '<h1 id="overview-heading" class="sr-only">',
        '<h2 id="inference-heading">',
    ):
        assert heading in html
    assert html.count('class="sr-only"') >= 1


def test_operation_overlay_a11y_semantics() -> None:
    """The operation overlay is a real modal dialog with a passive backdrop."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Backdrop: present, explicitly non-interactive/hidden from AT, starts hidden.
    assert 'id="operation-backdrop" class="operation-backdrop" aria-hidden="true" hidden' in html
    # Panel: a modal dialog that names itself and is the focus target.
    assert (
        'id="operation-panel" class="operation-panel" role="dialog" aria-modal="true"'
        ' aria-labelledby="operation-heading" tabindex="-1" hidden' in html
    )


def test_operation_overlay_is_centered_on_desktop_and_inset_on_mobile() -> None:
    """Desktop centers on both axes while the mobile rule restores edge insets."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    desktop = re.search(r"\.operation-panel\s*\{(?P<body>[^}]*)\}", css, re.DOTALL)
    assert desktop is not None
    declarations = desktop.group("body")
    for declaration in (
        r"top:\s*50%\s*;",
        r"right:\s*auto\s*;",
        r"left:\s*50%\s*;",
        r"transform:\s*translate\(\s*-50%\s*,\s*-50%\s*\)\s*;",
    ):
        assert re.search(declaration, declarations)

    mobile_start = css.index("@media (max-width: 44rem)")
    mobile_end = css.index("@media (max-width: 28rem)", mobile_start)
    mobile_panel = re.search(
        r"\.operation-panel\s*\{(?P<body>[^}]*)\}",
        css[mobile_start:mobile_end],
        re.DOTALL,
    )
    assert mobile_panel is not None
    mobile_declarations = mobile_panel.group("body")
    for declaration in (
        r"top:\s*1rem\s*;",
        r"right:\s*1rem\s*;",
        r"left:\s*1rem\s*;",
        r"width:\s*auto\s*;",
        r"transform:\s*none\s*;",
    ):
        assert re.search(declaration, mobile_declarations)


def test_operation_overlay_js_uses_body_class_and_focus_management() -> None:
    """The overlay must be driven off body state and trap/restore focus."""
    app = APP_JS.read_text(encoding="utf-8")
    # Body overlay class is toggled on open and closed.
    assert 'document.body.classList.add("operation-overlay-active")' in app
    assert 'document.body.classList.remove("operation-overlay-active")' in app
    # Focus moves into the panel and is restored via a remembered return node.
    assert (
        "requestAnimationFrame(() => el.operationPanel.focus({ preventScroll: true }))" in app
    )
    assert "state.operationFocusReturn" in app
    assert "state.operationFocusReturn = null" in app
    # The dismiss control is only revealed in terminal states (never while running).
    assert (
        "if (el.dismissOperation) el.dismissOperation.hidden = "
        "!TERMINAL_STATES.has(operation.status)" in app
    )


def test_operation_overlay_dismiss_is_terminal_only() -> None:
    """Dismissing an active (non-terminal) operation is a guarded no-op."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function dismissOperation()", "function showOperationOverlay()")
    assert "if (state.operation && !TERMINAL_STATES.has(state.operation.status)) return;" in body
    assert "hideOperationOverlay(true)" in body
    # The TERMINAL_STATES set is what gates dismissibility.
    assert 'const TERMINAL_STATES = new Set(["succeeded", "failed"])' in app


def test_comfy_controls_include_auto_on_off_with_aria_pressed() -> None:
    """ComfyUI controls expose Auto/On/Off and always mark the selected one."""
    app = APP_JS.read_text(encoding="utf-8")
    assert 'create("button", "quiet-button", "Auto")' in app
    assert 'create("button", "quiet-button", "On")' in app
    assert 'create("button", "quiet-button", "Off")' in app
    assert (
        '[auto, on, off].forEach((button) => '
        'button.setAttribute("aria-pressed", "false"))' in app
    )
    assert 'selected.setAttribute("aria-pressed", "true")' in app


def test_comfy_auto_reconciles_via_model_default() -> None:
    """Auto mode applies the selected model's declarative comfyui_default."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function selectComfyAuto(", "function activeConflictForService(")
    assert 'state.comfyControlMode = "auto"' in body
    assert "activeHealthyModel()" in body
    assert "model.comfyui_default" in body
    assert 'Boolean(model.comfyui_default)' in body
    assert 'setService("comfyui", desired)' in body


def test_model_switch_resets_comfy_mode_to_auto() -> None:
    """Submitting a model switch resets the ComfyUI control to Auto."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(
        app, "async function activateModel(", "function renderConflictWarnings("
    )
    assert 'state.comfyControlMode = "auto";' in body
    # The switch is sent with comfyui=null so the server applies the model default.
    assert 'comfyui: null' in body


def test_operation_overlay_backdrop_is_optional() -> None:
    """The backdrop is optional for mixed-cache compat: both show/hide guard it."""
    app = APP_JS.read_text(encoding="utf-8")
    assert "if (el.operationBackdrop) el.operationBackdrop.hidden = false" in app
    assert "if (el.operationBackdrop) el.operationBackdrop.hidden = true" in app
    # The panel remains required and is always toggled.
    assert "el.operationPanel.hidden = false" in app
    assert "el.operationPanel.hidden = true" in app


def test_operation_overlay_scrolls_to_top_only_on_open() -> None:
    """A freshly shown operation panel scrolls to top before focus, only once."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function showOperationOverlay(", "function hideOperationOverlay(")
    assert "el.operationPanel.scrollTop = 0;" in body
    # The scroll reset lives inside the "was hidden" branch, so polling rerenders
    # (which call showOperationOverlay while the panel is already visible) do not
    # reset the scroll position.
    start = body.index("function showOperationOverlay(")
    hidden_branch = body.index("if (el.operationPanel.hidden) {", start)
    assert hidden_branch < body.index("el.operationPanel.scrollTop = 0;", hidden_branch)
    assert body.index("el.operationPanel.scrollTop = 0;", hidden_branch) < body.index(
        "el.operationPanel.focus(", hidden_branch
    )


def test_comfy_auto_disabled_while_busy_or_indeterminate() -> None:
    """The Comfy Auto button is disabled during a mutation or unknown state."""
    app = APP_JS.read_text(encoding="utf-8")
    assert "auto.disabled = busy || indeterminate;" in app


def test_comfy_auto_returns_early_when_busy() -> None:
    """selectComfyAuto does nothing (no mode change, no reconcile) while busy."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function selectComfyAuto(", "function activeConflictForService(")
    lines = [line.strip() for line in body.splitlines()]
    assert lines[1] == "if (mutationBusy()) return;"


def test_active_conflict_derived_from_service_states() -> None:
    """Conflicts come from observed service states, active OR activating."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function activeConflictForService(", "function renderModels(")
    assert "state.status.services.items" in body
    assert 'ACTIVE_STATES.has(item.state)' in body
    assert 'item.kind === "model"' in body
    assert "active_model_ids" not in body


def test_build_id_matches_versioned_asset_urls() -> None:
    """The meta build marker and every versioned asset URL agree."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert f'content="{BUILD_ID}"' in html
    versions = set(re.findall(r"\?v=([0-9A-Za-z-]+)", html))
    assert versions == {BUILD_ID}
    # favicon.svg, styles.css, and app.js carry the versioned query.
    assert html.count(f"?v={BUILD_ID}") == 3

    app = APP_JS.read_text(encoding="utf-8")
    assert f'const BUILD_ID = "{BUILD_ID}";' in app


def test_runtime_uses_semantic_service_cards_without_role_data() -> None:
    """Runtime status is an article grid, not a table with role metadata."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    assert 'id="services-body" class="service-grid"' in html
    assert 'id="model-list" class="model-list"' in html
    assert html.count('class="inference-section"') == 1
    assert not re.search(r"<(?:table|thead|tbody|th|td)\b", html)
    assert 'create("article", "service-card")' in app
    assert '"System"' in app and '"HTTP"' in app
    assert "Inference model" not in app
    assert "Auxiliary" not in app
    assert "function cell(" not in app


def test_inactive_unit_health_skip_reason_is_hidden_for_both_card_types() -> None:
    """Suppress only the inactive-unit probe reason on model/service cards."""
    app = APP_JS.read_text(encoding="utf-8")
    assert 'function appendHealthDetail(container, reason)' in app
    assert 'typeof reason !== "string" || !reason.trim() ||' in app
    assert 'reason.trim().toLowerCase() === "health check skipped while unit is not active"' in app
    assert "appendHealthDetail(httpStatus, observed.reason);" in app
    assert "if (observed) appendHealthDetail(http, observed.reason);" in app
    assert (
        'append(create("span", "health-copy", '
        'safeReason(reason, "Health detail unavailable")))' in app
    )


def test_model_runtime_and_meta_share_one_separator() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    runtime = re.search(r"\.model-runtime\s*\{(?P<body>[^}]*)\}", css, re.DOTALL)
    meta = re.search(r"\.model-meta\s*\{(?P<body>[^}]*)\}", css, re.DOTALL)
    assert runtime and "border-bottom: 1px solid var(--line-soft)" in runtime.group("body")
    assert meta and "border-bottom: 1px solid var(--line-soft)" in meta.group("body")
    assert "border-top" not in meta.group("body")


def test_runtime_grid_and_telemetry_are_compact_and_responsive() -> None:
    """Wide runtime is four columns and telemetry shares the GPU card height."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(auto-fit, minmax(min(20rem, 100%), 1fr));" in css
    assert re.search(r"\.metric-card\s*\{[^}]*min-height:\s*10\.5rem", css, re.DOTALL)
    assert ".gpu-card { min-height: 10.5rem; }" in css
    assert not re.search(r"(?:table|thead|tbody|\bth\b|\btd\b)", css)


def test_restart_button_exists_only_on_comfyui_card() -> None:
    """Restart is created (and appended) only under the isComfy guard."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function serviceControls(", "function activeHealthyModel(")
    assert 'const restart = isComfy ? create("button", "quiet-button", "Restart") : null;' in body
    # Auto/On/Off stay unchanged for every service card.
    assert 'const auto = isComfy ? create("button", "quiet-button", "Auto") : null;' in body
    assert 'controls.append(auto, on, off);' in body
    # The restart button is only appended when it exists (ComfyUI card only).
    assert "if (restart) {" in body
    assert "controls.append(restart);" in body
    assert body.index("if (restart) {") < body.index("controls.append(restart);")
    # No Restart button can be created outside the ComfyUI guard: the literal
    # label appears exactly once in the whole script.
    assert app.count('"Restart"') == 1


def test_restart_enabled_only_when_exactly_active_and_idle() -> None:
    """Restart is disabled while busy or when ComfyUI is not exactly active."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "function serviceControls(", "function activeHealthyModel(")
    assert "restart.disabled = busy || observed.state !== \"active\";" in body
    # It is an action button, not an aria-pressed mode toggle.
    toggle_loop = body.index(
        '[auto, on, off].forEach((button) => button.setAttribute("aria-pressed", "false"));'
    )
    pressed = body.index('selected.setAttribute("aria-pressed", "true");')
    assert "restart" not in body[toggle_loop:pressed]


def test_restart_action_uses_dedicated_endpoint_and_busy_guard() -> None:
    """restartComfyUI posts to the dedicated endpoint through submitMutation."""
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "async function restartComfyUI(", "async function submitMutation(")
    lines = [line.strip() for line in body.splitlines()]
    assert lines[1] == "if (mutationBusy()) return;"
    assert 'await submitMutation("/api/services/comfyui/restart", "POST", {});' in body
    assert "state.submittingMutation = true;" in body
    # Live-region announcement for assistive tech, same pattern as model switch.
    assert 'el.liveRegion.textContent = "ComfyUI restart submitted.";' in body


def test_local_timer_starts_at_zero_counts_from_client_and_freezes() -> None:
    """Locally submitted operations display from a fresh client baseline."""
    app = APP_JS.read_text(encoding="utf-8")
    # Explicit display-timing state exists and is reset per operation.
    assert 'operationTimingMode: "resumed",' in app
    assert "function resetOperationTiming() {" in app
    body = _function_body(
        app, "function beginOperationPolling(", "async function pollOperation("
    )
    assert "resetOperationTiming();" in body
    assert 'state.operationTimingMode = "local";' in body
    assert "state.operationTimingBaseline = Date.now();" in body
    assert "options.localDisplay" in body
    # Only a locally accepted mutation switches to local mode; 409 following is
    # explicitly resumed mode (backend timestamps).
    submit = _function_body(
        app, "async function submitMutation(", "function beginOperationPolling("
    )
    assert "{ localDisplay: true }" in submit
    assert "{ localDisplay: false }" in submit
    # Status discovery (outside submitMutation) never passes localDisplay.
    discovery = _function_body(app, "async function refreshStatus(", "function renderStatus(")
    assert "localDisplay" not in discovery

    clock = _function_body(app, "function updateElapsed(", "function dismissOperation(")
    assert 'state.operationTimingMode === "local"' in clock
    assert "state.operationTimingFrozenAt = Date.now();" in clock
    assert "formatClock(frozen)" in clock
    assert "formatClock(running)" in clock
    # Resumed mode keeps using the backend timestamps.
    assert "Date.parse(operation.started_at || operation.created_at)" in clock
    assert "operation.finished_at ? Date.parse(operation.finished_at)" in clock


def test_timing_state_resets_on_dismiss_and_lock() -> None:
    """Dismiss and lock clear the display timing so baselines never leak."""
    app = APP_JS.read_text(encoding="utf-8")
    dismiss = _function_body(app, "function dismissOperation()", "function showOperationOverlay(")
    assert "resetOperationTiming();" in dismiss
    lock = _function_body(app, "function lockConsole()", "function handleUnauthorized(")
    assert "resetOperationTiming();" in lock
