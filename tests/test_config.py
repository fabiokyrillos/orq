from pathlib import Path

import pytest

from orq.config import Config, load_config


def test_defaults_when_file_is_missing(tmp_path: Path) -> None:
    config = load_config(tmp_path / "config.toml")

    assert config.limits.max_iterations == 15
    assert config.limits.max_wall_hours == 6
    assert config.limits.max_concurrent_runs == 2
    assert config.implementer.default_model == "opus"
    assert config.implementer.mechanical_model == "sonnet"
    assert config.reviewer.primary == "codex"
    assert config.reviewer.codex_model == "gpt-5.5"
    assert config.reviewer.fallback == "claude"
    assert config.reviewer.routine_effort == "low"
    assert config.reviewer.final_effort == "high"
    assert config.reviewer.switch_at_used_percent == 90
    assert config.git.merge_strategy == "squash"
    assert config.git.worktree_root == Path("C:/orq-wt")
    assert config.git.protected_paths == [".github/**", "migrations/**", "**/.env*"]
    assert config.git.sandbox_repos == []


def test_toml_values_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[limits]\nmax_iterations = 3\n\n[reviewer]\ncodex_model = \"gpt-5.5-mini\"\n\n"
        "[git]\nworktree_root = \"D:/wt\"\nsandbox_repos = [\"owner/sandbox\"]\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.limits.max_iterations == 3
    assert config.limits.max_wall_hours == 6
    assert config.reviewer.codex_model == "gpt-5.5-mini"
    assert config.reviewer.primary == "codex"
    assert config.git.worktree_root == Path("D:/wt")
    assert config.git.sandbox_repos == ["owner/sandbox"]


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[limits]\nmax_iteration = 3\n", encoding="utf-8")

    with pytest.raises(ValueError, match="limits.max_iteration"):
        load_config(path)


def test_worktree_root_expands_user_home(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[git]\nworktree_root = \"~/wt\"\n", encoding="utf-8")

    config = load_config(path)

    assert config.git.worktree_root == Path.home() / "wt"
    assert isinstance(config, Config)
