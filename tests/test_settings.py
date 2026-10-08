"""Layered settings (Phase 6): task > project > global overrides > config.toml > defaults."""

import json
from pathlib import Path

import pytest

from orq.config import Config
from orq.core.settings import SettingsError, codex_models, resolve, validate_overrides


def test_defaults_come_from_config_and_code() -> None:
    config = Config()
    config.reviewer.codex_model = "gpt-5.5"

    eff = resolve(config, {}, {}, {})

    assert eff["implementer.default_model"] == "opus" and eff.source("implementer.default_model") == "config"
    assert eff["reviewer.codex_model"] == "gpt-5.5"
    assert eff["reviewer.claude_model"] == "opus"  # empty claude_model follows the implementer's default model
    assert eff["git.protected_paths"] == [".github/**", "migrations/**", "**/.env*"]


def test_layers_override_in_order_and_report_their_source() -> None:
    eff = resolve(Config(),
                  {"reviewer.codex_model": "gpt-6-sol", "reviewer.routine_effort": "medium"},
                  {"reviewer.codex_model": "gpt-6.1-sol", "git.protected_paths": ["db/**"]},
                  {"routine_effort": "high"})

    assert eff["reviewer.codex_model"] == "gpt-6.1-sol" and eff.source("reviewer.codex_model") == "project"
    assert eff["reviewer.routine_effort"] == "high" and eff.source("reviewer.routine_effort") == "task"
    assert eff["git.protected_paths"] == ["db/**"]
    assert eff.source("implementer.mechanical_model") == "config"


def test_claude_reviewer_model_follows_the_effective_implementer_model() -> None:
    eff = resolve(Config(), {"implementer.default_model": "sonnet"}, {}, {})
    assert eff["reviewer.claude_model"] == "sonnet"
    eff = resolve(Config(), {"implementer.default_model": "sonnet", "reviewer.claude_model": "opus"}, {}, {})
    assert eff["reviewer.claude_model"] == "opus"


def write_cache(path: Path) -> Path:
    path.write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
        {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide", "supported_reasoning_levels": []},
    ]}), encoding="utf-8")
    return path


def test_codex_models_come_from_the_desktop_cache(tmp_path: Path) -> None:
    models = codex_models(write_cache(tmp_path / "models_cache.json"))

    assert [m["slug"] for m in models] == ["gpt-6.1-sol", "gpt-5.5"]
    assert models[1]["efforts"] == ["low", "medium", "high", "xhigh"]
    assert codex_models(tmp_path / "missing.json") == []


def test_validation_types_keys_and_efforts(tmp_path: Path) -> None:
    cache = write_cache(tmp_path / "models_cache.json")
    ok = validate_overrides({"reviewer.codex_model": "gpt-5.5", "reviewer.final_effort": "xhigh", "git.protected_paths": ["a/**"],
                             "guard.max_net_deleted_lines": 200, "implementer.default_model": None}, cache_path=cache)
    assert ok["guard.max_net_deleted_lines"] == 200 and ok["implementer.default_model"] is None

    with pytest.raises(SettingsError, match="unknown"):
        validate_overrides({"reviewer.primary": "claude"}, cache_path=cache)
    with pytest.raises(SettingsError, match="list of strings"):
        validate_overrides({"git.protected_paths": "a/**"}, cache_path=cache)
    with pytest.raises(SettingsError, match="effort"):
        validate_overrides({"reviewer.routine_effort": "turbo"}, cache_path=cache)
    with pytest.raises(SettingsError, match="gpt-5.5 does not support max"):
        validate_overrides({"reviewer.codex_model": "gpt-5.5", "reviewer.final_effort": "max"}, cache_path=cache)
    with pytest.raises(SettingsError, match="empty"):
        validate_overrides({"implementer.default_model": " "}, cache_path=cache)


def fake_run(stdout: str, record: list):
    import subprocess

    def run(argv, **kwargs):
        record.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout, "")
    return run


def test_probe_codex_ok_and_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from orq.adapters.probe import probe_model
    monkeypatch.setenv("ORQ_CODEX_CMD", "codex")
    calls: list = []
    ok_out = "\n".join(json.dumps(e) for e in [{"type": "thread.started"},
                                               {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}])
    assert probe_model("codex", "gpt-6.1-sol", run=fake_run(ok_out, calls)) == (True, "OK")
    argv = calls[0]
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol" and 'windows.sandbox="unelevated"' in argv and "--ignore-user-config" in argv

    refused = json.dumps({"type": "turn.failed", "error": {"message": json.dumps(
        {"type": "error", "status": 400, "error": {"message": "The 'gpt-x' model is not supported when using Codex with a ChatGPT account."}})}})
    ok, message = probe_model("codex", "gpt-x", run=fake_run(refused, []))
    assert not ok and message.startswith("The 'gpt-x' model is not supported")


def test_probe_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    from orq.adapters.probe import probe_model
    monkeypatch.setenv("ORQ_CLAUDE_EXE", "claude")
    calls: list = []
    assert probe_model("claude", "sonnet", run=fake_run(json.dumps({"is_error": False, "result": "OK"}), calls)) == (True, "OK")
    assert calls[0][calls[0].index("--model") + 1] == "sonnet"
    assert probe_model("claude", "nope", run=fake_run(json.dumps({"is_error": True, "result": "model not found"}), [])) == (False, "model not found")


def test_scan_roots_are_global_settings_only() -> None:
    config = Config()
    config.projects.scan_roots = ["D:/a"]
    eff = resolve(config, {"projects.scan_depth": 2}, {}, {})
    assert (eff["projects.scan_roots"], eff.source("projects.scan_roots")) == (["D:/a"], "config")
    assert eff["projects.scan_depth"] == 2

    assert validate_overrides({"projects.scan_roots": [" D:/Projetos/GitHub "]}) == {"projects.scan_roots": ["D:/Projetos/GitHub"]}
    with pytest.raises(SettingsError, match="global"):
        validate_overrides({"projects.scan_roots": ["D:/x"]}, layer="project")
    with pytest.raises(SettingsError):
        validate_overrides({"projects.scan_depth": 0})


def test_slot_limits_come_from_global_settings(tmp_path: Path) -> None:
    from orq.core.settings import slot_limits
    from orq.store.db import Store

    store = Store(tmp_path / "orq.db")
    config = Config()
    assert slot_limits(config, store) == (2, 1)
    store.set_settings({"limits.max_concurrent_runs": 3, "queue.project_concurrency": 2})
    assert slot_limits(config, store) == (3, 2)

    for bad in (0, 7):
        with pytest.raises(SettingsError, match="1 and 6"):
            validate_overrides({"limits.max_concurrent_runs": bad})
    with pytest.raises(SettingsError, match="global"):
        validate_overrides({"queue.project_concurrency": 2}, layer="project")
