from pathlib import Path

import pytest

from orq.config import GuardConfig
from orq.git.manager import GitManager
from orq.guard.diff_rules import evaluate_staged
from tests.conftest import git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    git("init", "-q", "-b", "main", str(path), cwd=tmp_path)
    git("config", "user.name", "t", cwd=path)
    git("config", "user.email", "t@example.invalid", cwd=path)
    (path / "app.py").write_text("def public():\n    return 1\n\ndef _private():\n    return 2\n", encoding="utf-8")
    (path / "routes.py").write_text("@app.get('/health')\ndef health():\n    return 'ok'\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_app.py").write_text("def test_public():\n    assert public() == 1\n", encoding="utf-8")
    (path / "pyproject.toml").write_text('[project]\ndependencies = [\n  "httpx>=0.27",\n  "typer>=0.12",\n]\n', encoding="utf-8")
    (path / "package.json").write_text('{\n  "dependencies": {\n    "lodash": "^4.17.21",\n    "react": "^18.0.0"\n  }\n}\n', encoding="utf-8")
    (path / ".github").mkdir()
    (path / ".github" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "seed", cwd=path)
    return path


def rules(repo: Path, **overrides) -> list[str]:
    config = GuardConfig(**overrides)
    gm = GitManager()
    gm.stage_all(repo)
    return sorted({v.rule for v in evaluate_staged(gm, repo, protected_paths=[".github/**"], guard=config)})


def test_clean_change_has_no_violations(repo: Path) -> None:
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert rules(repo) == []


def test_nothing_staged_has_no_violations(repo: Path) -> None:
    assert rules(repo) == []


def test_deleted_file(repo: Path) -> None:
    (repo / "routes.py").unlink()
    found = rules(repo)
    assert "deleted_file" in found


def test_removed_test(repo: Path) -> None:
    (repo / "tests" / "test_app.py").write_text("x = 1\n", encoding="utf-8")
    assert "removed_test" in rules(repo)


def test_renamed_or_added_test_is_not_a_removal(repo: Path) -> None:
    (repo / "tests" / "test_app.py").write_text("def test_public():\n    assert public() == 1\n\ndef test_more():\n    pass\n", encoding="utf-8")
    assert rules(repo) == []


def test_removed_private_function_is_fine_but_public_is_flagged(repo: Path) -> None:
    (repo / "app.py").write_text("def public():\n    return 1\n", encoding="utf-8")
    assert rules(repo) == []
    (repo / "app.py").write_text("def public():\n    return 1\n\ndef _private():\n    return 2\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    (repo / "app.py").write_text("def _private():\n    return 2\n", encoding="utf-8")
    assert "removed_export" in rules(repo)


def test_moved_export_is_not_a_removal(repo: Path) -> None:
    (repo / "app.py").write_text("def _private():\n    return 2\n", encoding="utf-8")
    (repo / "other.py").write_text("def public():\n    return 1\n", encoding="utf-8")
    assert "removed_export" not in rules(repo)


def test_removed_route(repo: Path) -> None:
    (repo / "routes.py").write_text("def health():\n    return 'ok'\n", encoding="utf-8")
    assert "removed_route" in rules(repo)


def test_protected_path(repo: Path) -> None:
    (repo / ".github" / "ci.yml").write_text("on: pull_request\n", encoding="utf-8")
    assert rules(repo) == ["protected_path"]


def test_dependency_removed_from_pyproject(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[project]\ndependencies = [\n  "typer>=0.12",\n]\n', encoding="utf-8")
    assert rules(repo) == ["dependency_removed"]


def test_dependency_removed_from_package_json(repo: Path) -> None:
    (repo / "package.json").write_text('{\n  "dependencies": {\n    "react": "^18.0.0"\n  }\n}\n', encoding="utf-8")
    assert rules(repo) == ["dependency_removed"]


def test_dependency_version_bump_is_fine(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[project]\ndependencies = [\n  "httpx>=0.28",\n  "typer>=0.12",\n]\n', encoding="utf-8")
    assert rules(repo) == []


def test_negative_balance(repo: Path) -> None:
    (repo / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(50)), encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "big", cwd=repo)
    (repo / "big.py").write_text("x = 1\n", encoding="utf-8")
    assert "negative_balance" in rules(repo, max_net_deleted_lines=40)
    assert "negative_balance" not in rules(repo, max_net_deleted_lines=100)


def test_negative_balance_ignores_non_source_files(repo: Path) -> None:
    (repo / "data.csv").write_text("".join(f"{i},{i}\n" for i in range(50)), encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "data", cwd=repo)
    (repo / "data.csv").write_text("1,1\n", encoding="utf-8")
    assert rules(repo, max_net_deleted_lines=10) == []


def test_violation_str_is_readable(repo: Path) -> None:
    (repo / "routes.py").unlink()
    gm = GitManager()
    gm.stage_all(repo)
    text = [str(v) for v in evaluate_staged(gm, repo, protected_paths=[], guard=GuardConfig())]
    assert "[deleted_file] routes.py: file deleted" in text


# Phase 6: only removals of what the base branch has count

def test_removing_what_this_run_added_is_not_a_violation(repo: Path) -> None:
    gm = GitManager()
    base = gm.head(repo)
    (repo / "tests" / "test_new.py").write_text("def test_added_by_the_run():\n    assert True\n", encoding="utf-8")
    (repo / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    gm.commit_all(repo, "iteration 1")
    (repo / "tests" / "test_new.py").write_text("def test_renamed_later():\n    assert True\n", encoding="utf-8")
    (repo / "helper.py").unlink()
    (repo / "tests" / "test_app.py").write_text("", encoding="utf-8")   # this one exists on the base: still flagged
    gm.stage_all(repo)

    found = {(v.rule, v.path) for v in evaluate_staged(gm, repo, protected_paths=[], guard=GuardConfig(), base=base)}

    assert found == {("removed_test", "tests/test_app.py")}
    without_base = {(v.rule, v.path) for v in evaluate_staged(gm, repo, protected_paths=[], guard=GuardConfig())}
    assert ("deleted_file", "helper.py") in without_base and ("removed_test", "tests/test_new.py") in without_base
