"""Declarative TOML configuration loading and validation.

Only ``tomllib`` from the standard library is used. The schema (documented in
README.md and in ``config/systems.toml``) is validated strictly: unknown keys,
illegal identifiers, duplicate units, asymmetric conflict declarations,
negative GPU indexes, and impossible companion/conflict combinations all
raise :class:`ConfigError` at load time so the application fails closed.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AppConfig",
    "ConfigError",
    "ModelSpec",
    "ServerInfo",
    "ServiceSpec",
    "UIOptions",
    "load_config",
]

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9@._-]{0,127}\.service$")
_HEALTH_SCHEMES = ("http://", "https://")
_COMPANION_SERVICE_ID = "comfyui"
#: Inclusive bounds accepted for UI refresh intervals (seconds).
_REFRESH_MIN_SECONDS = 5
_REFRESH_MAX_SECONDS = 300

_TOP_LEVEL_KEYS = {"server", "ui", "model", "service"}
_SERVER_KEYS = {"name", "os", "ram_gb", "cpu_summary", "gpu_summary"}
_UI_KEYS = {"refresh_choices_seconds", "default_refresh_seconds"}
_MODEL_KEYS = {
    "id",
    "display_name",
    "unit",
    "gpus",
    "startup_eta_seconds",
    "synopsis",
    "strengths",
    "conflicts_with",
    "comfyui_default",
    "allow_comfyui_override",
    "health_url",
}
_SERVICE_KEYS = {
    "id",
    "display_name",
    "unit",
    "gpus",
    "synopsis",
    "conflicts_with",
    "health_url",
}


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class ServerInfo:
    name: str
    os_name: str
    ram_gb: int
    cpu_summary: str | None
    gpu_summary: str


@dataclass(frozen=True)
class UIOptions:
    refresh_choices_seconds: tuple[int, ...]
    default_refresh_seconds: int


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    unit: str
    gpus: tuple[int, ...]
    startup_eta_seconds: int
    synopsis: str
    strengths: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    comfyui_default: bool
    allow_comfyui_override: bool
    health_url: str | None


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    display_name: str
    unit: str
    gpus: tuple[int, ...]
    synopsis: str
    conflicts_with: tuple[str, ...]
    health_url: str | None


@dataclass(frozen=True)
class AppConfig:
    server: ServerInfo
    ui: UIOptions
    models: tuple[ModelSpec, ...]
    services: tuple[ServiceSpec, ...]

    def model_by_id(self, model_id: str) -> ModelSpec | None:
        for model in self.models:
            if model.id == model_id:
                return model
        return None

    def service_by_id(self, service_id: str) -> ServiceSpec | None:
        for service in self.services:
            if service.id == service_id:
                return service
        return None


# ---------------------------------------------------------------------------
# Primitive helpers (every failure message names the offending entry, never a
# filesystem path or a raw command).
# ---------------------------------------------------------------------------


def _unknown_keys(table: dict[str, Any], allowed: set[str], where: str, errors: list[str]) -> None:
    for key in table:
        if key not in allowed:
            errors.append(f"{where}: unknown key '{key}'")


def _require_str(table: dict[str, Any], key: str, where: str, errors: list[str]) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: key '{key}' must be a non-empty string")
        return ""
    return value.strip()


def _optional_str(table: dict[str, Any], key: str, where: str, errors: list[str]) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: key '{key}' must be a non-empty string when present")
        return None
    return value.strip()


def _require_bool(table: dict[str, Any], key: str, where: str, errors: list[str]) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        errors.append(f"{where}: key '{key}' must be a boolean")
        return False
    return value


def _require_int(
    table: dict[str, Any], key: str, where: str, errors: list[str], *, minimum: int, maximum: int
) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{where}: key '{key}' must be an integer")
        return minimum
    if not minimum <= value <= maximum:
        errors.append(f"{where}: key '{key}' must be between {minimum} and {maximum}")
        return minimum
    return value


def _require_str_list(
    table: dict[str, Any], key: str, where: str, errors: list[str]
) -> tuple[str, ...]:
    value = table.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(f"{where}: key '{key}' must be a list of non-empty strings")
        return ()
    return tuple(item.strip() for item in value)


def _gpus(table: dict[str, Any], where: str, errors: list[str]) -> tuple[int, ...]:
    value = table.get("gpus", [])
    if not isinstance(value, list):
        errors.append(f"{where}: key 'gpus' must be a list of integers")
        return ()
    indexes: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            errors.append(f"{where}: gpu indexes must be integers")
            return ()
        if item < 0:
            errors.append(f"{where}: gpu indexes must be non-negative (got {item})")
            return ()
        indexes.append(item)
    if len(set(indexes)) != len(indexes):
        errors.append(f"{where}: duplicate gpu index in 'gpus'")
    return tuple(indexes)


def _health_url(table: dict[str, Any], where: str, errors: list[str]) -> str | None:
    url = _optional_str(table, "health_url", where, errors)
    if url is None:
        return None
    if not url.startswith(_HEALTH_SCHEMES) or any(ch.isspace() for ch in url):
        errors.append(f"{where}: 'health_url' must be a plain http(s) URL")
        return None
    return url


def _id(table: dict[str, Any], where: str, errors: list[str]) -> str:
    value = _require_str(table, "id", where, errors)
    if value and not _ID_RE.match(value):
        errors.append(
            f"{where}: id '{value}' is not a legal id (lowercase letters, digits, '-', '_')"
        )
    return value


def _unit(table: dict[str, Any], where: str, errors: list[str]) -> str:
    value = _require_str(table, "unit", where, errors)
    if value and not _UNIT_RE.match(value):
        errors.append(
            f"{where}: unit '{value}' must be a plain systemd unit name ending in '.service'"
        )
    return value


def _conflicts(table: dict[str, Any], where: str, errors: list[str]) -> tuple[str, ...]:
    return _require_str_list(table, "conflicts_with", where, errors)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_config(path: Path) -> AppConfig:
    """Load and validate a systems TOML file. Raises :class:`ConfigError`."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"configuration file could not be read ({type(exc).__name__})") from exc
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"configuration file is not valid TOML ({type(exc).__name__})") from exc
    return validate_config(doc)


def validate_config(doc: Any) -> AppConfig:
    """Validate a parsed TOML document mapping into an :class:`AppConfig`."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        raise ConfigError("configuration root must be a TOML table")

    _unknown_keys(doc, _TOP_LEVEL_KEYS, "top level", errors)

    server = _parse_server(doc.get("server"), errors)
    ui = _parse_ui(doc.get("ui"), errors)
    models = _parse_models(doc.get("model"), errors)
    services = _parse_services(doc.get("service"), errors)

    _cross_validate(models, services, errors)

    if errors:
        raise ConfigError("; ".join(errors))
    return AppConfig(server=server, ui=ui, models=models, services=services)


def _parse_server(table: Any, errors: list[str]) -> ServerInfo:
    where = "server"
    if not isinstance(table, dict):
        errors.append(f"{where}: missing [server] table")
        return ServerInfo(name="", os_name="", ram_gb=0, cpu_summary=None, gpu_summary="")
    _unknown_keys(table, _SERVER_KEYS, where, errors)
    name = _require_str(table, "name", where, errors)
    os_name = _require_str(table, "os", where, errors)
    ram_gb = _require_int(table, "ram_gb", where, errors, minimum=1, maximum=1_048_576)
    cpu_summary = _optional_str(table, "cpu_summary", where, errors)
    gpu_summary = _require_str(table, "gpu_summary", where, errors)
    return ServerInfo(
        name=name,
        os_name=os_name,
        ram_gb=ram_gb,
        cpu_summary=cpu_summary,
        gpu_summary=gpu_summary,
    )


def _parse_ui(table: Any, errors: list[str]) -> UIOptions:
    where = "ui"
    if not isinstance(table, dict):
        errors.append(f"{where}: missing [ui] table")
        return UIOptions(refresh_choices_seconds=(5,), default_refresh_seconds=5)
    _unknown_keys(table, _UI_KEYS, where, errors)
    raw_choices = table.get("refresh_choices_seconds")
    if not isinstance(raw_choices, list) or not raw_choices or any(
        isinstance(item, bool)
        or not isinstance(item, int)
        or not _REFRESH_MIN_SECONDS <= item <= _REFRESH_MAX_SECONDS
        for item in raw_choices
    ):
        errors.append(
            f"{where}: 'refresh_choices_seconds' must be a non-empty list of unique integers"
            f" between {_REFRESH_MIN_SECONDS} and {_REFRESH_MAX_SECONDS}"
        )
        choices: tuple[int, ...] = ()
    else:
        choices = tuple(sorted(set(raw_choices)))
        if len(choices) != len(raw_choices):
            errors.append(f"{where}: 'refresh_choices_seconds' contains duplicates")
    default = _require_int(
        table,
        "default_refresh_seconds",
        where,
        errors,
        minimum=_REFRESH_MIN_SECONDS,
        maximum=_REFRESH_MAX_SECONDS,
    )
    if choices and default not in choices:
        errors.append(f"{where}: 'default_refresh_seconds' must be one of the configured choices")
    return UIOptions(refresh_choices_seconds=choices, default_refresh_seconds=default)


def _require_table_list(value: Any, key: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        errors.append(f"'{key}' must be a non-empty array of tables")
        return []
    if not all(isinstance(item, dict) for item in value):
        errors.append(f"'{key}' entries must all be tables")
        return []
    return value


def _parse_models(value: Any, errors: list[str]) -> tuple[ModelSpec, ...]:
    entries = _require_table_list(value, "model", errors)
    models: list[ModelSpec] = []
    for index, table in enumerate(entries):
        where = f"model[{index}]"
        _unknown_keys(table, _MODEL_KEYS, where, errors)
        model_id = _id(table, where, errors)
        display_name = _require_str(table, "display_name", where, errors)
        unit = _unit(table, where, errors)
        gpus = _gpus(table, where, errors)
        eta = _require_int(table, "startup_eta_seconds", where, errors, minimum=1, maximum=3600)
        synopsis = _require_str(table, "synopsis", where, errors)
        strengths = _require_str_list(table, "strengths", where, errors)
        conflicts = _conflicts(table, where, errors)
        comfyui_default = _require_bool(table, "comfyui_default", where, errors)
        allow_override = _require_bool(table, "allow_comfyui_override", where, errors)
        health = _health_url(table, where, errors)
        models.append(
            ModelSpec(
                id=model_id,
                display_name=display_name,
                unit=unit,
                gpus=gpus,
                startup_eta_seconds=eta,
                synopsis=synopsis,
                strengths=strengths,
                conflicts_with=conflicts,
                comfyui_default=comfyui_default,
                allow_comfyui_override=allow_override,
                health_url=health,
            )
        )
    return tuple(models)


def _parse_services(value: Any, errors: list[str]) -> tuple[ServiceSpec, ...]:
    entries = [] if value is None else _require_table_list(value, "service", errors)
    services: list[ServiceSpec] = []
    for index, table in enumerate(entries):
        where = f"service[{index}]"
        _unknown_keys(table, _SERVICE_KEYS, where, errors)
        service_id = _id(table, where, errors)
        display_name = _require_str(table, "display_name", where, errors)
        unit = _unit(table, where, errors)
        gpus = _gpus(table, where, errors)
        synopsis = _require_str(table, "synopsis", where, errors)
        conflicts = _conflicts(table, where, errors)
        health = _health_url(table, where, errors)
        services.append(
            ServiceSpec(
                id=service_id,
                display_name=display_name,
                unit=unit,
                gpus=gpus,
                synopsis=synopsis,
                conflicts_with=conflicts,
                health_url=health,
            )
        )
    return tuple(services)


def _cross_validate(
    models: tuple[ModelSpec, ...], services: tuple[ServiceSpec, ...], errors: list[str]
) -> None:
    # Unique legal ids across both kinds.
    seen: dict[str, str] = {}
    for spec in list(models) + list(services):
        kind = "model" if isinstance(spec, ModelSpec) else "service"
        if spec.id and spec.id in seen:
            errors.append(f"duplicate id '{spec.id}' (declared twice, second as {kind})")
        elif spec.id:
            seen[spec.id] = kind

    # Unique systemd units.
    units: dict[str, str] = {}
    for spec in list(models) + list(services):
        if spec.unit in units:
            errors.append(
                f"duplicate unit '{spec.unit}' (used by '{units[spec.unit]}' and '{spec.id}')"
            )
        elif spec.unit:
            units[spec.unit] = spec.id

    service_ids = {service.id for service in services}

    # Conflict references must exist and be symmetric.
    for model in models:
        for target in model.conflicts_with:
            if target not in service_ids:
                errors.append(
                    f"model '{model.id}': conflicts_with references unknown service '{target}'"
                )
        for service in services:
            declared_model = service.id in model.conflicts_with
            declared_service = model.id in service.conflicts_with
            if declared_model != declared_service:
                errors.append(
                    f"conflict between model '{model.id}' and service '{service.id}'"
                    " is not declared symmetrically"
                )

    # Companion (comfyui) policy: impossible default combinations.
    for model in models:
        if _COMPANION_SERVICE_ID in model.conflicts_with:
            if model.comfyui_default:
                errors.append(
                    f"model '{model.id}': comfyui_default true is impossible"
                    " while it conflicts with 'comfyui'"
                )
            if model.allow_comfyui_override:
                errors.append(
                    f"model '{model.id}': allow_comfyui_override is impossible"
                    " while it conflicts with 'comfyui'"
                )
        if model.comfyui_default and _COMPANION_SERVICE_ID not in service_ids:
            errors.append(
                f"model '{model.id}': comfyui_default true requires a 'comfyui' service entry"
            )
