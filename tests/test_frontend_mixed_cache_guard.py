"""Regression guard for mixed cache versions: operation-backdrop is optional.

An older cached ``index.html`` may predate the ``#operation-backdrop`` element
while a newer ``app.js`` is already cached (mixed asset versions). app.js must
not throw when the overlay is shown, hidden, or the console is locked. This
module verifies the guarded references statically and, when node is available,
executes the script against a DOM shim that omits the backdrop to prove the
show/hide/lock path survives.

The operation *panel* remains required; only the backdrop is optional.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "hal_dashboard" / "static" / "app.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node executable not available"
)


def test_operation_backdrop_references_are_guarded() -> None:
    app = APP_JS.read_text(encoding="utf-8")
    # Both the show and hide paths guard the optional backdrop.
    assert app.count("if (el.operationBackdrop) el.operationBackdrop.hidden") == 2
    assert "if (el.operationBackdrop) el.operationBackdrop.hidden = false" in app
    assert "if (el.operationBackdrop) el.operationBackdrop.hidden = true" in app
    # The panel itself is never guarded: it must be present in every version.
    assert "el.operationPanel.hidden = false" in app
    assert "el.operationPanel.hidden = true" in app


@requires_node
def test_overlay_show_hide_and_lock_survive_without_backdrop() -> None:
    """Simulate a DOM lacking #operation-backdrop; show/hide/lock must not throw."""
    script = textwrap.dedent(
        """
        const vm = require("vm");
        const fs = require("fs");
        const source = fs.readFileSync(__APP_JS_PATH__, "utf8");

        const stub = () => ({
          addEventListener() {},
          append() {},
          appendChild() {},
          removeChild() {},
          setAttribute() {},
          focus() {},
          showModal() {},
          close() {},
          querySelectorAll() { return []; },
          classList: { toggle() {}, add() {}, remove() {} },
          style: {},
          children: [],
          firstChild: null,
          hidden: true,
        });

        const document = {
          getElementById(id) {
            // Older cached HTML has no #operation-backdrop and no dismiss control.
            if (id === "operation-backdrop" || id === "dismiss-operation") return null;
            return stub();
          },
          addEventListener() {},
          createElement() { return stub(); },
          createTextNode() { return stub(); },
          body: { classList: { add() {}, remove() {}, toggle() {} } },
          activeElement: null,
          hidden: false,
          title: "",
        };
        const window = {
          addEventListener() {},
          setTimeout(fn) { return 0; },
          setInterval(fn) { return 0; },
          clearTimeout() {},
          clearInterval() {},
          requestAnimationFrame(fn) { fn(); return 0; },
        };
        const sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
        // A pending fetch keeps the async beginSession/checkPublicHealth from
        // exercising further UI nodes; we drive the overlay path directly.
        const fetch = () => new Promise(() => {});

        const sandbox = {
          document, window, sessionStorage, fetch, console, Date, JSON,
          setTimeout(fn) { return 0; }, setInterval(fn) { return 0; },
          clearTimeout() {}, clearInterval() {},
          navigator: { onLine: true },
        };
        vm.createContext(sandbox);
        vm.runInContext(source, sandbox, { filename: "app.js" });
        vm.runInContext("initialize();", sandbox);
        // Drive the show/hide/lock path against a DOM without the backdrop.
        const drive =
          "state.operation = { status: 'queued', steps: [], "
          + "created_at: new Date().toISOString(), target_model_id: null };"
          + "renderOperation();"
          + "hideOperationOverlay(false);"
          + "showOperationOverlay();"
          + "hideOperationOverlay(true);"
          + "lockConsole();";
        vm.runInContext(drive, sandbox);
        console.log("OVERLAY_PATH_OK");
        """
    ).replace("__APP_JS_PATH__", json.dumps(str(APP_JS)))
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "OVERLAY_PATH_OK" in result.stdout
