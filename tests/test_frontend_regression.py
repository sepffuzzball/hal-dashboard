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

BUILD_ID = "20260924-4"


def _function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_redundant_section_titles_are_sr_only() -> None:
    """The visible duplicate headings are hidden visually-only (sr-only)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for heading in (
        '<h1 id="overview-heading" class="sr-only">',
        '<h2 id="services-heading" class="sr-only">',
        '<h2 id="models-heading" class="sr-only">',
    ):
        assert heading in html
    assert html.count('class="sr-only"') >= 3


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
    assert not re.search(r"<(?:table|thead|tbody|th|td)\b", html)
    assert 'create("article", "service-card")' in app
    assert '"System"' in app and '"HTTP"' in app
    assert "Inference model" not in app
    assert "Auxiliary" not in app
    assert "function cell(" not in app


def test_runtime_grid_and_telemetry_are_compact_and_responsive() -> None:
    """Wide runtime is four columns and telemetry shares the GPU card height."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert re.search(
        r"\.service-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)",
        css,
        re.DOTALL,
    )
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert re.search(r"\.metric-card\s*\{[^}]*min-height:\s*10\.5rem", css, re.DOTALL)
    assert ".gpu-card { min-height: 10.5rem; }" in css
    assert not re.search(r"(?:table|thead|tbody|\bth\b|\btd\b)", css)
