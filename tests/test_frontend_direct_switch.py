"""Static regression guards for the dependency-free model switch interface."""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "hal_dashboard" / "static"


def test_review_interface_and_radio_semantics_are_removed() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "review-dialog" not in index
    assert "review-button" not in index
    assert 'role="radiogroup"' not in index
    assert "selectedModelId" not in app
    assert "aria-checked" not in app


def test_model_cards_expose_direct_accessible_activation() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'card.setAttribute("role", "button")' in app
    assert 'card.addEventListener("click", () => activateModel(model))' in app
    assert "if (event.repeat) return" in app
    assert 'event.key !== " " && event.key !== "Enter"' in app
    assert "moveModelFocus(next.id)" in app


def test_direct_switch_has_exact_default_payload_and_synchronous_guard() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    submit = app.index('submitMutation("/api/switch"')
    guard = app.index("state.submittingModelId = model.id")

    assert guard < submit
    assert '{ model_id: model.id, comfyui: null }' in app
    assert "state.submittingMutation || state.submittingModelId" in app
    assert "state.operationUrl || (state.status && state.status.active_operation)" in app
    assert "target_model_id" in app


def test_loading_state_and_load_window_are_rendered() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert 'statusBadge("Loading", "loading")' in app
    assert 'metaItem("Load window", formatLoadWindow(model))' in app
    assert "startup_eta_min_seconds" in app
    assert ".model-card.loading::before" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
