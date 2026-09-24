"""Regression guards for the optional operation-dismiss control.

The ``#dismiss-operation`` button is optional: an older cached ``index.html``
may not include it while a newer ``app.js`` is already cached (mixed asset
versions). app.js must not throw during initialization or rendering when the
element is absent. This module statically verifies the guarded references and,
when node is available, executes the script against a DOM shim that omits the
element to prove initialization survives.
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


def test_dismiss_listener_and_hidden_use_are_guarded() -> None:
    app = APP_JS.read_text(encoding="utf-8")
    # Every reference is guarded by an existence check on the optional control,
    # so each appears exactly once (in its guarded form) - never bare.
    assert app.count("el.dismissOperation.addEventListener(") == 1
    assert "if (el.dismissOperation) el.dismissOperation.addEventListener(" in app
    assert app.count("el.dismissOperation.hidden") == 1
    assert "if (el.dismissOperation) el.dismissOperation.hidden =" in app
    # The dismiss handler itself must never dereference the optional control.
    offset = app.index("function dismissOperation()")
    assert "el.dismissOperation" not in app[offset:]


@requires_node
def test_initialize_survives_without_dismiss_element() -> None:
    """Simulate a DOM that lacks #dismiss-operation; initialize() must not throw."""
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
          classList: { toggle() {}, add() {}, remove() {} },
          style: {},
        });

        const document = {
          getElementById(id) { return id === "dismiss-operation" ? null : stub(); },
          addEventListener() {},
          createElement() { return stub(); },
          createTextNode() { return stub(); },
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
        };
        const sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
        // A pending fetch keeps the async work from touching further UI nodes.
        const fetch = () => new Promise(() => {});

        const sandbox = {
          document, window, sessionStorage, fetch, console,
          setTimeout(fn) { return 0; }, setInterval(fn) { return 0; },
          clearTimeout() {}, clearInterval() {},
          navigator: { onLine: true },
        };
        vm.createContext(sandbox);
        vm.runInContext(source, sandbox, { filename: "app.js" });
        vm.runInContext("initialize();", sandbox);
        console.log("INITIALIZE_OK");
        """
    ).replace("__APP_JS_PATH__", json.dumps(str(APP_JS)))
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "INITIALIZE_OK" in result.stdout
