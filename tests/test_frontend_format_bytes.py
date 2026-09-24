"""Regression tests for the frontend byte formatter in static/app.js.

The project has no JavaScript test harness, so these tests verify the
formatter by executing the extracted ``formatBytes`` function with node
and, as a static guard, by checking the unit sequence in the source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "hal_dashboard" / "static" / "app.js"

MIB = 1024 * 1024

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node executable not available"
)


def _extract_function(source: str, name: str) -> str:
    """Return the full source of the top-level function ``name`` via brace matching."""
    start = source.index(f"function {name}(")
    depth = 0
    for i in range(start, len(source)):
        char = source[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise ValueError(f"unterminated function {name!r} in app.js")


def test_unit_sequence_includes_mb() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert 'const units = ["B", "KB", "MB", "GB", "TB"];' in source


@pytest.mark.parametrize(
    ("nbytes", "expected"),
    [
        (270262607872, "252 GB"),
        (500533600256, "466 GB"),
        (97887 * MIB, "95.6 GB"),
    ],
)
@requires_node
def test_format_bytes_labels_gigabytes(nbytes: int, expected: str) -> None:
    source = APP_JS.read_text(encoding="utf-8")
    fn_source = _extract_function(source, "formatBytes")
    script = (
        f"{fn_source}\n"
        f"const inputs = {json.dumps([nbytes])};\n"
        "console.log(JSON.stringify(inputs.map((v) => formatBytes(v))));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    formatted = json.loads(result.stdout)[0]
    assert "TB" not in formatted
    assert formatted == expected
