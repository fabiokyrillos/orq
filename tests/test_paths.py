from pathlib import Path

import pytest

from orq.paths import OrqPaths


def test_default_root_is_dot_orq_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORQ_HOME", raising=False)

    paths = OrqPaths.from_env()

    assert paths.root == Path.home() / ".orq"


def test_orq_home_env_overrides_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ORQ_HOME", str(tmp_path / "custom"))

    paths = OrqPaths.from_env()

    assert paths.root == tmp_path / "custom"


def test_layout_follows_spec_section_11(tmp_path: Path) -> None:
    paths = OrqPaths(tmp_path)

    assert paths.db == tmp_path / "orq.db"
    assert paths.config == tmp_path / "config.toml"
    assert paths.repos == tmp_path / "repos"
    assert paths.run_dir("R1") == tmp_path / "runs" / "R1"
    assert paths.iteration_dir("R1", 3) == tmp_path / "runs" / "R1" / "iterations" / "3"
    assert paths.allow_tokens("R1") == tmp_path / "runs" / "R1" / "allow_tokens"
