"""Behavioral guard for the operation overlay's client display timing.

Executes the real app.js in a node vm against a minimal DOM shim with a
controllable fake clock and drives the display-timing paths directly:

  * a locally submitted operation renders 00:00 immediately and counts up from
    the client baseline (never from backend timestamps),
  * the local elapsed value freezes when the operation turns terminal,
  * a resumed operation (409 follow or /api/status discovery) uses backend
    created/started/finished timestamps,
  * starting a new operation or dismissing/locking resets the timing state so
    no baseline leaks between operations.
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


@requires_node
def test_display_timing_contract_under_fake_clock() -> None:
    script = textwrap.dedent(
        """
        const vm = require("vm");
        const fs = require("fs");
        const source = fs.readFileSync(__APP_JS_PATH__, "utf8");

        // Controllable clock: everything (app + test) reads time via FakeDate.
        let NOW = 1_700_000_000_000;
        class FakeDate extends Date {
          constructor(...args) { if (args.length === 0) super(NOW); else super(...args); }
          static now() { return NOW; }
        }
        // ISO timestamps are computed host-side so vm code never sees NOW.
        const isoAgo = (ms) => new FakeDate(NOW - ms).toISOString();
        const TIMING_PROBE =
          "JSON.stringify([state.operationTimingMode, state.operationTimingBaseline,"
          + " state.operationTimingFrozenAt])";

        const nodes = new Map();
        const makeStub = (id) => ({
          id,
          textContent: "",
          className: "",
          hidden: true,
          disabled: false,
          value: "",
          open: false,
          scrollTop: 0,
          firstChild: null,
          children: [],
          dataset: {},
          addEventListener() {},
          append() {},
          appendChild() {},
          removeChild() {},
          setAttribute() {},
          focus() {},
          showModal() {},
          close() {},
          select() {},
          querySelectorAll() { return []; },
          classList: { toggle() {}, add() {}, remove() {} },
          style: {},
        });

        const document = {
          getElementById(id) {
            if (!nodes.has(id)) nodes.set(id, makeStub(id));
            return nodes.get(id);
          },
          addEventListener() {},
          createElement() { return makeStub("created"); },
          createTextNode() { return makeStub("text"); },
          body: { classList: { add() {}, remove() {}, toggle() {} } },
          activeElement: null,
          hidden: false,
          title: "",
        };
        const window = {
          addEventListener() {},
          setTimeout() { return 0; },
          setInterval() { return 0; },
          clearTimeout() {},
          clearInterval() {},
          requestAnimationFrame(fn) { fn(); return 0; },
        };
        const sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
        // Pending forever: polling never mutates state during the drive.
        const fetch = () => new Promise(() => {});

        const sandbox = {
          document, window, sessionStorage, fetch, console,
          Date: FakeDate, JSON, Math, Number, String, Set, Array, Object, Error,
          Promise, RegExp, Intl,
          setTimeout() { return 0; }, setInterval() { return 0; },
          clearTimeout() {}, clearInterval() {},
          navigator: { onLine: true },
        };
        vm.createContext(sandbox);
        vm.runInContext(source, sandbox, { filename: "app.js" });

        const elapsed = () => document.getElementById("operation-elapsed").textContent;
        const assertEqual = (actual, expected, label) => {
          if (actual !== expected) {
            throw new Error(
              `${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`
            );
          }
        };

        vm.runInContext("initialize();", sandbox);

        // --- local mode: immediate 00:00, counts from the client baseline.
        vm.runInContext(
          "beginOperationPolling('/api/operations/op1', null, { localDisplay: true });",
          sandbox,
        );
        assertEqual(elapsed(), "00:00", "local first render");
        NOW += 5_000;
        vm.runInContext("updateElapsed();", sandbox);
        assertEqual(elapsed(), "00:05", "local counts from client baseline");

        // Backend timestamps must NOT drive the local display: a long-running
        // backend operation still shows the client-observed value.
        vm.runInContext(
          "state.operation = { ...state.operation, status: 'running',"
          + " created_at: '" + isoAgo(500000) + "',"
          + " started_at: '" + isoAgo(500000) + "' };",
          sandbox,
        );
        vm.runInContext("updateElapsed();", sandbox);
        assertEqual(elapsed(), "00:05", "local mode ignores backend timestamps");

        // --- terminal freezes the client elapsed value.
        vm.runInContext(
          "state.operation = { ...state.operation, status: 'succeeded' }; renderOperation();",
          sandbox,
        );
        assertEqual(elapsed(), "00:05", "terminal local clock freezes");
        NOW += 60_000;
        vm.runInContext("renderOperation();", sandbox);
        assertEqual(elapsed(), "00:05", "frozen clock stays frozen past terminal");

        // --- resumed mode: backend timestamps drive the elapsed display.
        vm.runInContext(
          "beginOperationPolling('/api/operations/op2', null, { localDisplay: false });",
          sandbox,
        );
        vm.runInContext(
          "state.operation = { status: 'running', steps: [], target_model_id: null,"
          + " created_at: '" + isoAgo(90000) + "' };",
          sandbox,
        );
        vm.runInContext("updateElapsed();", sandbox);
        assertEqual(elapsed(), "01:30", "resumed mode uses backend created_at");
        vm.runInContext(
          "state.operation = { ...state.operation, status: 'succeeded',"
          + " started_at: '" + isoAgo(90000) + "',"
          + " finished_at: '" + isoAgo(30000) + "' };",
          sandbox,
        );
        vm.runInContext("updateElapsed();", sandbox);
        assertEqual(elapsed(), "01:00", "resumed terminal uses backend finished-started");

        // --- new local operation: fresh 00:00 baseline, no leak from op1/op2.
        vm.runInContext(
          "beginOperationPolling('/api/operations/op3', null, { localDisplay: true });",
          sandbox,
        );
        assertEqual(elapsed(), "00:00", "new operation resets the local baseline");

        // --- dismiss resets the timing state.
        vm.runInContext(
          "state.operation = { ...state.operation, status: 'succeeded' }; dismissOperation();",
          sandbox,
        );
        const timing = vm.runInContext(TIMING_PROBE, sandbox);
        assertEqual(timing, '["resumed",null,null]', "dismiss resets timing state");

        // --- lock resets the timing state as well.
        vm.runInContext(
          "beginOperationPolling('/api/operations/op4', null, { localDisplay: true });",
          sandbox,
        );
        NOW += 12_000;
        vm.runInContext("updateElapsed();", sandbox);
        assertEqual(elapsed(), "00:12", "second local operation counts");
        vm.runInContext("lockConsole();", sandbox);
        const timing2 = vm.runInContext(TIMING_PROBE, sandbox);
        assertEqual(timing2, '["resumed",null,null]', "lock resets timing state");

        console.log("TIMER_DISPLAY_OK");
        """
    ).replace("__APP_JS_PATH__", json.dumps(str(APP_JS)))
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "TIMER_DISPLAY_OK" in result.stdout
