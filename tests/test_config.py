"""Configuration loading and validation tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from conftest import CONFIG_PATH

from hal_dashboard.config import ConfigError, load_config, validate_config

REPO_TOML = CONFIG_PATH.read_text(encoding="utf-8")


def parse(text: str) -> dict:
    return tomllib.loads(text)


def test_repo_config_loads_and_matches_required_entries() -> None:
    config = load_config(CONFIG_PATH)
    assert config.server.name == "Hal"
    assert config.server.os_name == "Debian 13 (trixie) VM"
    assert config.server.ram_gb == 256
    assert "RTX 6000" in config.server.gpu_summary

    assert [m.id for m in config.models] == [
        "vllm-dsv4-flash-vision",
        "sglang-qwen38-flash-next",
        "sglang-qwen38-27b",
    ]
    vllm = config.model_by_id("vllm-dsv4-flash-vision")
    assert vllm is not None
    assert vllm.display_name == "vLLM DSv4 Flash Vision Exp"
    assert vllm.unit == "vllm-deepseek-v4.service"
    assert vllm.gpus == (0, 1)
    assert vllm.startup_eta_min_seconds == 300
    assert vllm.startup_eta_seconds == 420
    assert vllm.startup_timeout_seconds == 900
    assert vllm.strengths  # user-provided strengths present
    assert "creative writing" in vllm.synopsis.lower()
    assert vllm.conflicts_with == ("comfyui",)
    assert vllm.comfyui_default is False
    assert vllm.allow_comfyui_override is False

    flash = config.model_by_id("sglang-qwen38-flash-next")
    assert flash is not None
    assert flash.display_name == "SGLang Qwen3.8 Flash Next"
    assert flash.unit == "sglang-qwen38-flash-next.service"
    assert flash.gpus == (0,)
    assert flash.startup_eta_min_seconds == 180
    assert flash.startup_eta_seconds == 360
    assert flash.startup_timeout_seconds == 900
    assert "comfyui" in flash.synopsis.lower()
    assert flash.comfyui_default is True

    q27 = config.model_by_id("sglang-qwen38-27b")
    assert q27 is not None
    assert q27.display_name == "SGLang Qwen3.8 27B"
    assert q27.unit == "sglang-qwen38-27b.service"
    assert q27.gpus == (0,)
    assert q27.startup_eta_min_seconds == 120
    assert q27.startup_eta_seconds == 180
    assert q27.startup_timeout_seconds == 900
    assert q27.comfyui_default is True

    comfy = config.service_by_id("comfyui")
    assert comfy is not None
    assert comfy.unit == "comfyui.service"
    assert comfy.gpus == (1,)
    assert comfy.conflicts_with == ("vllm-dsv4-flash-vision",)
    lowered_synopsis = comfy.synopsis.lower()
    for medium in ("image", "music", "video", "3d"):
        assert medium in lowered_synopsis
    assert "gpu 1" in lowered_synopsis

    # All three mutually exclusive model backends share one external identity.
    assert {model.health_url for model in config.models} == {"http://127.0.0.1:8000/health"}

    # Exact shipped refresh policy.
    assert config.ui.refresh_choices_seconds == (5, 15, 30, 60, 120, 300)
    assert config.ui.default_refresh_seconds == 60
    assert config.ui.default_refresh_seconds in config.ui.refresh_choices_seconds


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def patched(old: str, new: str) -> dict:
    assert old in REPO_TOML, old
    return parse(REPO_TOML.replace(old, new, 1))


def test_duplicate_ids_rejected() -> None:
    doc = parse(REPO_TOML.replace('id = "comfyui"', 'id = "sglang-qwen38-27b"', 1))
    with pytest.raises(ConfigError, match="duplicate id"):
        validate_config(doc)


def test_duplicate_units_rejected() -> None:
    doc = patched(
        'unit = "comfyui.service"',
        'unit = "sglang-qwen38-27b.service"',
    )
    with pytest.raises(ConfigError, match="duplicate unit"):
        validate_config(doc)


def test_conflict_reference_must_exist() -> None:
    doc = parse(REPO_TOML.replace('conflicts_with = ["comfyui"]', 'conflicts_with = ["ghost"]', 1))
    with pytest.raises(ConfigError, match="unknown service"):
        validate_config(doc)


def test_conflicts_must_be_symmetric() -> None:
    doc = parse(
        REPO_TOML.replace('conflicts_with = ["vllm-dsv4-flash-vision"]', "conflicts_with = []", 1)
    )
    with pytest.raises(ConfigError, match="symmetric"):
        validate_config(doc)


def test_negative_gpu_index_rejected() -> None:
    doc = parse(REPO_TOML.replace("gpus = [1]\nsynopsis", "gpus = [-1]\nsynopsis", 1))
    with pytest.raises(ConfigError, match="non-negative"):
        validate_config(doc)


def test_companion_default_true_with_conflict_rejected() -> None:
    with pytest.raises(ConfigError, match="impossible"):
        validate_config(patched("comfyui_default = false", "comfyui_default = true"))


def test_companion_override_with_conflict_rejected() -> None:
    with pytest.raises(ConfigError, match="impossible"):
        validate_config(
            patched(
                "comfyui_default = false\nallow_comfyui_override = false",
                "comfyui_default = false\nallow_comfyui_override = true",
            )
        )


def test_default_refresh_must_be_in_choices() -> None:
    doc = parse(
        REPO_TOML.replace("default_refresh_seconds = 60", "default_refresh_seconds = 7", 1)
    )
    with pytest.raises(ConfigError, match="default_refresh_seconds"):
        validate_config(doc)


@pytest.mark.parametrize("value", [4, 301, 0, -5, 1000])
def test_refresh_choices_outside_5_to_300_rejected(value: int) -> None:
    doc = patched(
        "refresh_choices_seconds = [5, 15, 30, 60, 120, 300]",
        f"refresh_choices_seconds = [60, {value}]",
    )
    with pytest.raises(ConfigError, match="between 5 and 300"):
        validate_config(doc)


@pytest.mark.parametrize("value", [4, 301, 3600])
def test_default_refresh_outside_5_to_300_rejected(value: int) -> None:
    doc = patched("default_refresh_seconds = 60", f"default_refresh_seconds = {value}")
    with pytest.raises(ConfigError, match="between 5 and 300"):
        validate_config(doc)


def test_refresh_boundaries_5_and_300_accepted() -> None:
    text = REPO_TOML.replace(
        "refresh_choices_seconds = [5, 15, 30, 60, 120, 300]",
        "refresh_choices_seconds = [5, 300]",
        1,
    )
    text = text.replace("default_refresh_seconds = 60", "default_refresh_seconds = 300", 1)
    config = validate_config(parse(text))
    assert config.ui.refresh_choices_seconds == (5, 300)
    assert config.ui.default_refresh_seconds == 300


def test_unknown_keys_rejected() -> None:
    doc = parse(REPO_TOML.replace("[ui]", "[ui]\nunknown_key = 3", 1))
    with pytest.raises(ConfigError, match="unknown key"):
        validate_config(doc)


def test_unit_must_be_a_service_unit() -> None:
    doc = parse(REPO_TOML.replace('unit = "comfyui.service"', 'unit = "comfyui.timer"', 1))
    with pytest.raises(ConfigError, match=".service"):
        validate_config(doc)


def test_illegal_id_rejected() -> None:
    doc = parse(REPO_TOML.replace('id = "comfyui"', 'id = "Comfy UI!"', 1))
    with pytest.raises(ConfigError, match="legal id"):
        validate_config(doc)


def test_missing_model_section_rejected() -> None:
    doc = parse(REPO_TOML)
    doc["model"] = []
    with pytest.raises(ConfigError, match="non-empty array"):
        validate_config(doc)


def test_health_url_must_be_http() -> None:
    doc = patched(
        'health_url = "http://127.0.0.1:8188/"',
        'health_url = "ftp://host/"',
    )
    with pytest.raises(ConfigError, match="health_url"):
        validate_config(doc)


def test_startup_eta_min_is_required() -> None:
    doc = patched(
        "startup_eta_min_seconds = 300\nstartup_eta_seconds = 420",
        "startup_eta_seconds = 420",
    )
    with pytest.raises(ConfigError, match="startup_eta_min_seconds"):
        validate_config(doc)


def test_startup_eta_min_exceeding_max_rejected() -> None:
    doc = patched(
        "startup_eta_min_seconds = 300\nstartup_eta_seconds = 420",
        "startup_eta_min_seconds = 421\nstartup_eta_seconds = 420",
    )
    with pytest.raises(ConfigError, match="must not exceed"):
        validate_config(doc)


@pytest.mark.parametrize("value", [0, 3601, -5])
def test_startup_eta_min_outside_1_to_3600_rejected(value: int) -> None:
    doc = patched(
        "startup_eta_min_seconds = 300\nstartup_eta_seconds = 420",
        f"startup_eta_min_seconds = {value}\nstartup_eta_seconds = 420",
    )
    with pytest.raises(ConfigError, match="between 1 and 3600"):
        validate_config(doc)


@pytest.mark.parametrize("value", [0, 3601, -5])
def test_startup_eta_max_outside_1_to_3600_rejected(value: int) -> None:
    doc = patched(
        "startup_eta_min_seconds = 300\nstartup_eta_seconds = 420",
        f"startup_eta_min_seconds = 300\nstartup_eta_seconds = {value}",
    )
    with pytest.raises(ConfigError, match="between 1 and 3600"):
        validate_config(doc)


def test_startup_eta_min_equal_max_accepted() -> None:
    doc = patched(
        "startup_eta_min_seconds = 300\nstartup_eta_seconds = 420",
        "startup_eta_min_seconds = 420\nstartup_eta_seconds = 420",
    )
    config = validate_config(doc)
    vllm = config.model_by_id("vllm-dsv4-flash-vision")
    assert vllm is not None
    assert vllm.startup_eta_min_seconds == 420
    assert vllm.startup_eta_seconds == 420


def test_startup_timeout_seconds_is_required() -> None:
    doc = patched(
        "startup_eta_seconds = 420\nstartup_timeout_seconds = 900",
        "startup_eta_seconds = 420",
    )
    with pytest.raises(ConfigError, match="startup_timeout_seconds"):
        validate_config(doc)


def test_startup_timeout_below_max_eta_rejected() -> None:
    doc = patched(
        "startup_eta_seconds = 420\nstartup_timeout_seconds = 900",
        "startup_eta_seconds = 420\nstartup_timeout_seconds = 100",
    )
    with pytest.raises(ConfigError, match="must be at least"):
        validate_config(doc)


@pytest.mark.parametrize("value", [0, 1801, -5])
def test_startup_timeout_outside_1_to_1800_rejected(value: int) -> None:
    doc = patched(
        "startup_eta_seconds = 420\nstartup_timeout_seconds = 900",
        f"startup_eta_seconds = 420\nstartup_timeout_seconds = {value}",
    )
    with pytest.raises(ConfigError, match="between 1 and 1800"):
        validate_config(doc)


def test_startup_timeout_equal_max_eta_accepted() -> None:
    doc = patched(
        "startup_eta_seconds = 420\nstartup_timeout_seconds = 900",
        "startup_eta_seconds = 420\nstartup_timeout_seconds = 420",
    )
    config = validate_config(doc)
    vllm = config.model_by_id("vllm-dsv4-flash-vision")
    assert vllm is not None
    assert vllm.startup_timeout_seconds == 420


def test_startup_timeout_900_accepted() -> None:
    config = validate_config(parse(REPO_TOML))
    for model in config.models:
        assert model.startup_timeout_seconds == 900


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")
