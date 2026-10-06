from pathlib import Path

import pytest

from orq.guard.rules import action_key, classify, describe_action, glob_match

WT = Path("C:/orq-wt/sandbox/R1")


@pytest.mark.parametrize("command,rule", [
    ("git push --force origin main", "git_force_push"),
    ("git push -f", "git_force_push"),
    ("git push --force-with-lease", "git_force_push"),
    ("git reset --hard HEAD~1", "git_reset_hard"),
    ("cd src && git reset --hard", "git_reset_hard"),
    ("git branch -D feature", "git_branch_delete"),
    ("git branch --delete feature", "git_branch_delete"),
    ("git push origin --delete feature", "git_branch_delete"),
    ("git push origin :feature", "git_branch_delete"),
    ("git clean -fdx", "git_clean"),
    ("rm -rf build", "recursive_delete"),
    ("rm -r build", "recursive_delete"),
    ("rm -fR build", "recursive_delete"),
    ("Remove-Item -Recurse -Force build", "recursive_delete"),
    ("rmdir /s /q build", "recursive_delete"),
    ("del /s *.pyc", "recursive_delete"),
    ("psql -c 'DROP TABLE users'", "sql_destructive"),
    ("sqlite3 db.sqlite 'TRUNCATE TABLE x'", "sql_destructive"),
    ("uv remove httpx", "dependency_removal"),
    ("pip uninstall -y httpx", "dependency_removal"),
    ("python -m pip uninstall httpx", "dependency_removal"),
    ("npm uninstall lodash", "dependency_removal"),
    ("npm rm lodash", "dependency_removal"),
    ("yarn remove lodash", "dependency_removal"),
    ("pnpm remove lodash", "dependency_removal"),
    ("echo hi > C:/Users/me/notes.txt", "write_outside_worktree"),
    ("echo hi >> /c/Users/me/notes.txt", "write_outside_worktree"),
    ("cat x | tee C:/tmp/out.txt", "write_outside_worktree"),
])
def test_bash_destructive_commands_are_classified(command: str, rule: str) -> None:
    violation = classify("Bash", {"command": command}, worktree=WT, protected_paths=[])
    assert violation is not None and violation.rule == rule


@pytest.mark.parametrize("command", [
    "git status", "git push origin main", "git reset HEAD~1", "git reset --soft HEAD~1",
    "git branch feature", "git clean -n", "rm build/out.txt", "uv add httpx", "pip install httpx",
    "npm install lodash", "echo hi > notes.txt", "echo hi > C:/orq-wt/sandbox/R1/notes.txt",
    "python -m pytest -q", "echo 'drop by the office'", "grep -r TRUNCATE docs/",
])
def test_bash_safe_commands_are_allowed(command: str) -> None:
    assert classify("Bash", {"command": command}, worktree=WT, protected_paths=[]) is None


def test_unbalanced_quotes_fall_back_to_whitespace_split() -> None:
    assert classify("Bash", {"command": 'rm -rf "build'}, worktree=WT, protected_paths=[]).rule == "recursive_delete"


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_file_tools_outside_worktree_are_denied(tool: str, key: str) -> None:
    violation = classify(tool, {key: "C:/Users/me/.bashrc"}, worktree=WT, protected_paths=[])
    assert violation is not None and violation.rule == "write_outside_worktree"


def test_file_tools_inside_worktree_are_allowed() -> None:
    assert classify("Write", {"file_path": str(WT / "src" / "x.py")}, worktree=WT, protected_paths=[]) is None
    assert classify("Write", {"file_path": "src/x.py"}, worktree=WT, protected_paths=[]) is None


def test_protected_path_is_denied() -> None:
    violation = classify("Edit", {"file_path": str(WT / ".github" / "workflows" / "ci.yml")}, worktree=WT,
                         protected_paths=[".github/**", "**/.env*"])
    assert violation is not None and violation.rule == "protected_path"
    assert classify("Edit", {"file_path": str(WT / "app" / ".env.local")}, worktree=WT, protected_paths=["**/.env*"]).rule == "protected_path"
    assert classify("Edit", {"file_path": ".env"}, worktree=WT, protected_paths=["**/.env*"]).rule == "protected_path"


def test_other_tools_are_allowed() -> None:
    assert classify("Read", {"file_path": "C:/anything"}, worktree=WT, protected_paths=[]) is None
    assert classify("Bash", {}, worktree=WT, protected_paths=[]) is None


def test_action_key_is_stable_and_input_sensitive() -> None:
    a = action_key("Bash", {"command": "git reset --hard", "description": "Reset working tree"})
    b = action_key("Bash", {"description": "Discard changes", "command": "git reset --hard"})
    c = action_key("Bash", {"command": "git reset --hard HEAD~2"})
    assert a == b and a != c and len(a) == 24  # the description Claude adds does not change the key
    assert action_key("Write", {"file_path": "x.py", "content": "a"}) == action_key("Write", {"file_path": "x.py", "content": "b"})
    assert action_key("Write", {"file_path": "x.py"}) != action_key("Edit", {"file_path": "x.py"})


def test_describe_action_quotes_command_or_path() -> None:
    assert describe_action("Bash", {"command": "rm -rf build"}) == "Bash: rm -rf build"
    assert describe_action("Write", {"file_path": "C:/x.txt"}) == "Write: C:/x.txt"


@pytest.mark.parametrize("pattern,path,expected", [
    (".github/**", ".github/workflows/ci.yml", True),
    (".github/**", "src/.github/x", False),
    ("**/.env*", ".env", True),
    ("**/.env*", "app/.env.local", True),
    ("**/.env*", "app/environment.py", False),
    ("migrations/**", "migrations/0001.sql", True),
    ("*.md", "README.md", True),
    ("*.md", "docs/README.md", False),
])
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected
