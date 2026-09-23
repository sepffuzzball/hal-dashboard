"""Status probes: hardware metrics, GPU metrics, systemd states, HTTP health.

All subprocess calls are argv-only (never a shell), bounded by timeouts, and
executed through absolute binaries resolved from a trusted fixed search path.
Snapshots are cached for ~2 seconds and coalesced behind one asyncio lock so
concurrent dashboard refreshes share a single probe round. Every probe is
partial-failure tolerant: an unavailable value is reported as ``null`` plus a
short sanitized reason - never as a fake zero.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

import psutil

from hal_dashboard.config import AppConfig

__all__ = ["HealthChecker", "Probes", "Systemctl", "resolve_binary"]

TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UNIT_STATES = frozenset({"active", "inactive", "activating", "deactivating", "failed", "unknown"})

_NVIDIA_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"


def resolve_binary(name: str) -> str | None:
    """Resolve an executable from a trusted fixed PATH (empty PATH, no cwd)."""
    return shutil.which(name, path=TRUSTED_PATH)


def _int_or_none(token: str) -> int | None:
    token = token.strip()
    if not token or token.upper().startswith("N/A"):
        return None
    try:
        return int(float(token))
    except ValueError:
        return None


class Systemctl:
    """Minimal, bounded systemd client (argv-only, absolute binary, no shell)."""

    def __init__(self, *, binary: str | None = None, query_timeout_seconds: float = 5.0) -> None:
        self._binary = binary if binary is not None else resolve_binary("systemctl")
        self._query_timeout = query_timeout_seconds

    @property
    def binary(self) -> str | None:
        return self._binary

    async def state(self, unit: str) -> str:
        """Return one of UNIT_STATES. 'unknown' when systemd is unreachable."""
        return await asyncio.to_thread(self._state_sync, unit)

    def _state_sync(self, unit: str) -> str:
        if self._binary is None:
            return "unknown"
        try:
            completed = subprocess.run(
                [self._binary, "is-active", "--no-reload", unit],
                capture_output=True,
                text=True,
                timeout=self._query_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        state = completed.stdout.strip().lower()
        return state if state in UNIT_STATES else "unknown"

    async def run(self, verb: str, unit: str, *, timeout_seconds: float) -> str:
        """Invoke systemctl verb against one unit; return a sanitized status kind."""
        return await asyncio.to_thread(self._run_sync, verb, unit, timeout_seconds)

    def _run_sync(self, verb: str, unit: str, timeout_seconds: float) -> str:
        if self._binary is None:
            return "systemctl unavailable"
        try:
            subprocess.run(
                [self._binary, verb, unit],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "command timed out"
        except OSError:
            return "command could not be executed"
        # The exit code alone is never treated as success: callers verify the
        # observed unit state afterwards.
        return "ok"


class HealthChecker:
    """Optional HTTP health probe for a configured unit (bounded, urllib only)."""

    def __init__(self, *, timeout_seconds: float = 2.5) -> None:
        self._timeout = timeout_seconds

    async def check(self, url: str) -> bool | None:
        return await asyncio.to_thread(self._check_sync, url)

    def _check_sync(self, url: str) -> bool | None:
        try:
            request = urllib.request.Request(url, method="GET")  # noqa: S310 - scheme validated at config load
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return 200 <= response.status < 400
        except urllib.error.HTTPStatusError:
            # The endpoint answered: the service is reachable but unhealthy.
            return False
        except (urllib.error.URLError, OSError, ValueError):
            return None


class Probes:
    """Coalesced ~2s cache of a full hardware/service status snapshot."""

    def __init__(
        self,
        config: AppConfig,
        *,
        systemctl: Systemctl | None = None,
        health: HealthChecker | None = None,
        nvidia_smi_path: str | None = None,
        ttl_seconds: float = 2.0,
        command_timeout_seconds: float = 5.0,
    ) -> None:
        self._config = config
        self._systemctl = systemctl or Systemctl()
        self._health = health or HealthChecker()
        self._nvidia = (
            nvidia_smi_path
            if nvidia_smi_path is not None
            else resolve_binary("nvidia-smi")
        )
        self._ttl = ttl_seconds
        self._command_timeout = command_timeout_seconds
        self._lock = asyncio.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def snapshot(self) -> dict[str, Any]:
        """Return the (possibly cached) raw probe sections."""
        cached = self._cache
        if cached is not None and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        async with self._lock:
            cached = self._cache
            if cached is not None and time.monotonic() - cached[0] < self._ttl:
                return cached[1]
            data = await asyncio.to_thread(self._snapshot_sync)
            # Service health needs an async HTTP round; run it concurrently.
            data["services"] = await self._attach_health(data["services"])
            self._cache = (time.monotonic(), data)
            return data

    def _snapshot_sync(self) -> dict[str, Any]:
        return {
            "cpu": self._cpu_section(),
            "memory": self._memory_section(),
            "storage_root": self._storage_section(),
            "gpus": self._gpu_section(),
            "services": {"items": self._service_stub(), "reason": None},
        }

    # -- individual sections (each independently failure tolerant) ---------

    def _cpu_section(self) -> dict[str, Any]:
        try:
            percent = psutil.cpu_percent(interval=0.05)
            return {"percent": round(float(percent), 1), "reason": None}
        except Exception:  # noqa: BLE001 - never fail the whole snapshot
            return {"percent": None, "reason": "cpu probe failed"}

    def _memory_section(self) -> dict[str, Any]:
        try:
            vm = psutil.virtual_memory()
            return {
                "used_bytes": int(vm.used),
                "total_bytes": int(vm.total),
                "percent": round(float(vm.percent), 1),
                "reason": None,
            }
        except Exception:  # noqa: BLE001
            return {
                "used_bytes": None,
                "total_bytes": None,
                "percent": None,
                "reason": "memory probe failed",
            }

    def _storage_section(self) -> dict[str, Any]:
        try:
            du = psutil.disk_usage("/")
            return {
                "used_bytes": int(du.used),
                "total_bytes": int(du.total),
                "percent": round(float(du.percent), 1),
                "reason": None,
            }
        except Exception:  # noqa: BLE001
            return {
                "used_bytes": None,
                "total_bytes": None,
                "percent": None,
                "reason": "storage probe failed",
            }

    def _gpu_section(self) -> dict[str, Any]:
        if self._nvidia is None:
            return {"items": [], "reason": "nvidia-smi not available"}
        try:
            completed = subprocess.run(
                [
                    self._nvidia,
                    f"--query-gpu={_NVIDIA_QUERY}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"items": [], "reason": "nvidia-smi query failed"}
        if completed.returncode != 0:
            return {"items": [], "reason": "nvidia-smi query failed"}
        items: list[dict[str, Any]] = []
        problems: list[str] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 6:
                problems.append("malformed nvidia-smi row")
                continue
            index = _int_or_none(fields[0])
            name = fields[1] or None
            util = _int_or_none(fields[2])
            mem_used = _int_or_none(fields[3])
            mem_total = _int_or_none(fields[4])
            temp = _int_or_none(fields[5])
            if index is None:
                problems.append("malformed nvidia-smi row")
                continue
            mem_percent = (
                round(mem_used * 100.0 / mem_total, 1)
                if mem_used is not None and mem_total
                else None
            )
            items.append(
                {
                    "index": index,
                    "name": name,
                    "utilization_percent": util,
                    "memory_used_bytes": mem_used * 1024 * 1024 if mem_used is not None else None,
                    "memory_total_bytes": (
                        mem_total * 1024 * 1024 if mem_total is not None else None
                    ),
                    "memory_percent": mem_percent,
                    "temperature_celsius": temp,
                }
            )
        reason = "some gpu metrics were unavailable" if problems or any(
            item["utilization_percent"] is None for item in items
        ) else None
        return {"items": items, "reason": reason}

    def _service_stub(self) -> list[dict[str, Any]]:
        """Systemd-only service entries; health is attached asynchronously."""
        items: list[dict[str, Any]] = []
        for model in self._config.models:
            items.append(
                {
                    "id": model.id,
                    "display_name": model.display_name,
                    "kind": "model",
                    "unit": model.unit,
                    "state": self._systemctl._state_sync(model.unit),
                    "healthy": None,
                    "reason": None,
                    "_health_url": model.health_url,
                }
            )
        for service in self._config.services:
            items.append(
                {
                    "id": service.id,
                    "display_name": service.display_name,
                    "kind": "auxiliary",
                    "unit": service.unit,
                    "state": self._systemctl._state_sync(service.unit),
                    "healthy": None,
                    "reason": None,
                    "_health_url": service.health_url,
                }
            )
        return items

    async def _attach_health(self, section: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for item in section["items"]:
            entry = dict(item)
            url = entry.pop("_health_url")
            if not url:
                entry["healthy"] = None
                entry["reason"] = "no health endpoint configured"
            elif entry["state"] != "active":
                entry["healthy"] = None
                entry["reason"] = "health check skipped while unit is not active"
            else:
                result = await self._health.check(url)
                if result is True:
                    entry["healthy"] = True
                    entry["reason"] = None
                elif result is False:
                    entry["healthy"] = False
                    entry["reason"] = "health endpoint reported failure"
                else:
                    entry["healthy"] = None
                    entry["reason"] = "health endpoint unreachable"
            items.append(entry)
        return {"items": items, "reason": section.get("reason")}
