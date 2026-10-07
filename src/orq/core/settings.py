"""Layered settings (Phase 6). Effective value = task > project > global overrides (dashboard) > config.toml > defaults.

Models and reasoning efforts are resolved before every agent call, so a change applies at the next call of a live run.
Guard settings are read when the run checks a diff and when it starts (the hook's file).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orq.config import Config

EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_KEYS = ("implementer.default_model", "implementer.mechanical_model", "reviewer.codex_model", "reviewer.claude_model")
EFFORT_KEYS = ("reviewer.routine_effort", "reviewer.final_effort")
LIST_KEYS = ("git.protected_paths", "guard.source_globs")
INT_KEYS = ("guard.max_net_deleted_lines",)
KEYS = MODEL_KEYS + EFFORT_KEYS + LIST_KEYS + INT_KEYS
LIVE_KEYS = MODEL_KEYS + EFFORT_KEYS
# Short names for the optional `## Models` section of TASK.md (models and efforts only).
TASK_ALIASES = {"implementer": "implementer.default_model", "mechanical": "implementer.mechanical_model",
                "reviewer": "reviewer.codex_model", "claude_reviewer": "reviewer.claude_model",
                "routine_effort": "reviewer.routine_effort", "final_effort": "reviewer.final_effort"}
LAYERS = ("task", "project", "global")


class SettingsError(ValueError):
    pass


def default_cache_path() -> Path:
    return Path.home() / ".codex" / "models_cache.json"


@dataclass(frozen=True)
class Effective:
    values: dict[str, Any]
    sources: dict[str, str]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def source(self, key: str) -> str:
        return self.sources[key]


def _from_config(config: Config, key: str) -> Any:
    section, name = key.split(".")
    value = getattr(getattr(config, section), name)
    return list(value) if isinstance(value, list) else value


def resolve(config: Config, global_overrides: Mapping[str, Any], project_overrides: Mapping[str, Any],
            task_overrides: Mapping[str, Any]) -> Effective:
    """`task_overrides` uses the TASK.md aliases; the other layers use full keys. None or missing means inherit."""
    task = {TASK_ALIASES.get(k, k): v for k, v in task_overrides.items()}
    layers = (("task", task), ("project", project_overrides), ("global", global_overrides))
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key in KEYS:
        for name, layer in layers:
            if layer.get(key) not in (None, ""):
                values[key], sources[key] = layer[key], name
                break
        else:
            values[key], sources[key] = _from_config(config, key), "config"
    if not values["reviewer.claude_model"]:
        # No explicit Claude reviewer model: follow the implementer's default model, from whatever layer it came.
        values["reviewer.claude_model"] = values["implementer.default_model"]
        sources["reviewer.claude_model"] = sources["implementer.default_model"]
    return Effective(values, sources)


def codex_models(cache_path: Path | None = None) -> list[dict]:
    """Models the Codex desktop cache lists for the picker, with the efforts each supports. Empty when unreadable."""
    path = cache_path or default_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("models", []) if isinstance(data, dict) else data
    models = []
    for item in items:
        if not isinstance(item, dict) or item.get("visibility") != "list" or not item.get("slug"):
            continue
        efforts = [str(level.get("effort")) for level in item.get("supported_reasoning_levels") or [] if isinstance(level, dict)]
        models.append({"slug": item["slug"], "display_name": item.get("display_name") or item["slug"], "efforts": efforts})
    return models


def validate_overrides(overrides: Mapping[str, Any], *, cache_path: Path | None = None,
                       codex_model: str | None = None) -> dict[str, Any]:
    """Check keys, types and efforts; None clears a key. `codex_model` is the model the efforts will run on."""
    clean: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in KEYS:
            raise SettingsError(f"unknown setting {key}")
        if value is None:
            clean[key] = None
        elif key in LIST_KEYS:
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise SettingsError(f"{key} must be a list of strings")
            clean[key] = [v.strip() for v in value]
        elif key in INT_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SettingsError(f"{key} must be a positive integer")
            clean[key] = value
        else:
            if not isinstance(value, str) or not value.strip():
                raise SettingsError(f"{key} must not be empty")
            clean[key] = value.strip()
            if key in EFFORT_KEYS and clean[key] not in EFFORTS:
                raise SettingsError(f"{key}: unknown effort {clean[key]!r} (one of {', '.join(EFFORTS)})")
    model = clean.get("reviewer.codex_model") or codex_model
    known = {m["slug"]: m["efforts"] for m in codex_models(cache_path)}
    if model in known and known[model]:
        for key in EFFORT_KEYS:
            if clean.get(key) and clean[key] not in known[model]:
                raise SettingsError(f"{model} does not support {clean[key]} (supports {', '.join(known[model])})")
    return clean
