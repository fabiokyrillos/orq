# Phase 2 Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Phase 2 safety layer to orq: pre-execution guard hook with one-time tokens, post-execution diff rules, no-progress detection with rollback, checkpointed crash resume, and rate-limit handling with the Claude reviewer fallback.

**Architecture:** Pure rule modules (`guard/rules.py`, `guard/diff_rules.py`, `core/progress.py`, `core/ratelimit.py`) are unit tested in isolation. The `Runner` is refactored into a phase loop driven by a `Checkpoint` persisted in `state.json`, so every pause and crash resumes from the same code path. A `ReviewerRouter` wraps the Codex and Claude reviewers behind the existing `Agent` protocol.

**Tech Stack:** Python 3.12, `typer`, `sqlite3`, `asyncio` subprocesses, `pytest`, `zoneinfo`. Windows native, `pathlib` everywhere. Tests run with `uv run pytest -q`.

Design: `docs/superpowers/specs/2026-10-06-phase2-safety-design.md`. Spec: `docs/SPEC.md`.

---

## File structure

Create:

* `src/orq/guard/__init__.py` (empty)
* `src/orq/guard/rules.py`: pure classification of a tool call, action key, description.
* `src/orq/guard/hook.py`: the `PreToolUse` script run by Claude Code.
* `src/orq/guard/settings.py`: writes `claude-settings.json` and `guard.json` into the run dir.
* `src/orq/guard/diff_rules.py`: post-execution rules over the staged index.
* `src/orq/core/progress.py`: no-progress detection.
* `src/orq/core/checkpoint.py`: `Checkpoint` dataclass, `state.json` load and save.
* `src/orq/core/procs.py`: `pid_alive`, `kill_tree`.
* `src/orq/core/ratelimit.py`: Claude reset-time parsing.
* `src/orq/adapters/router.py`: `ReviewerRouter`.
* `src/orq/__main__.py`: `python -m orq` entry for subprocess tests.
* `tests/test_guard_rules.py`, `tests/test_guard_hook.py`, `tests/test_diff_rules.py`, `tests/test_progress.py`, `tests/test_checkpoint.py`, `tests/test_ratelimit.py`, `tests/test_router.py`, `tests/test_resume.py`.

Modify:

* `src/orq/adapters/base.py`: `stream_process` gets `pid_file`.
* `src/orq/adapters/claude.py`: `--settings` when the run dir has hook settings; pid file.
* `src/orq/adapters/codex.py`: error classification, rate-limit snapshot.
* `src/orq/config.py`: `[guard]`, `[limits].rate_limit_retries`, optional sandbox list.
* `src/orq/core/models.py`: `RunState.PAUSED`.
* `src/orq/core/prompts.py`: guard line in the standing rules, interrupted-turn note, discarded-iterations note.
* `src/orq/core/loop.py`: phase loop over a checkpoint.
* `src/orq/git/manager.py`: reset, clean, staged diff helpers, `pr_url`.
* `src/orq/cli.py`: `answer`, `resume`, `rollback`, `pause`, `abort`.
* `tests/fakes/fake_claude.py`: `hang` scenario and denial-with-work scenario.
* `tests/test_loop.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_adapters.py`.
* `docs/SPEC.md`, `docs/phase2-findings.md` (new).

---

## Milestone 1: pre-execution guard

### Task 1: guard rules

**Files:**
- Create: `src/orq/guard/__init__.py`, `src/orq/guard/rules.py`
- Test: `tests/test_guard_rules.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guard_rules.py
from pathlib import Path

import pytest

from orq.guard.rules import action_key, classify, describe_action

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


def test_other_tools_are_allowed() -> None:
    assert classify("Read", {"file_path": "C:/anything"}, worktree=WT, protected_paths=[]) is None
    assert classify("Bash", {}, worktree=WT, protected_paths=[]) is None


def test_action_key_is_stable_and_input_sensitive() -> None:
    a = action_key("Bash", {"command": "git reset --hard", "description": "x"})
    b = action_key("Bash", {"description": "x", "command": "git reset --hard"})
    c = action_key("Bash", {"command": "git reset --hard HEAD~2"})
    assert a == b and a != c and len(a) == 24


def test_describe_action_quotes_command_or_path() -> None:
    assert describe_action("Bash", {"command": "rm -rf build"}) == "Bash: rm -rf build"
    assert describe_action("Write", {"file_path": "C:/x.txt"}) == "Write: C:/x.txt"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guard_rules.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'orq.guard'`

- [ ] **Step 3: Implement the rules**

```python
# src/orq/guard/__init__.py
```

```python
# src/orq/guard/rules.py
"""Pure classification of a Claude Code tool call against the guard rules (SPEC 10.3).

No I/O here: the hook, the runner and the tests all call `classify`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_SQL_RE = re.compile(r"\b(?:drop\s+(?:table|database|schema|index|view)|truncate(?:\s+table)?)\s+\S", re.IGNORECASE)
_PS_RECURSE_RE = re.compile(r"\b(?:remove-item|ri|rm|del|erase|rd|rmdir)\b[^|;&]*\s-recurse\b", re.IGNORECASE)
_CMD_RECURSE_RE = re.compile(r"\b(?:rmdir|rd|del|erase)\b[^|;&]*\s/s\b", re.IGNORECASE)
_REDIRECT_RE = re.compile(r">{1,2}\s*\"?((?:[A-Za-z]:[\\/]|/)[^\s\"'|;&]+)")
_TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?\"?((?:[A-Za-z]:[\\/]|/)[^\s\"'|;&]+)")
_DEP_REMOVAL = {("uv", "remove"), ("pip", "uninstall"), ("pip3", "uninstall"), ("poetry", "remove"),
                ("npm", "uninstall"), ("npm", "remove"), ("npm", "rm"), ("npm", "un"), ("npm", "r"),
                ("yarn", "remove"), ("pnpm", "remove"), ("pnpm", "rm"), ("cargo", "remove")}


@dataclass(frozen=True)
class Violation:
    rule: str
    description: str


def action_key(tool_name: str, tool_input: dict) -> str:
    payload = tool_name + "\n" + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def describe_action(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return f"Bash: {str(tool_input.get('command', '')).strip()}"
    key = FILE_TOOLS.get(tool_name)
    if key:
        return f"{tool_name}: {tool_input.get(key, '')}"
    return f"{tool_name}: {json.dumps(tool_input, ensure_ascii=False)[:200]}"


def classify(tool_name: str, tool_input: dict, *, worktree: Path | None, protected_paths: list[str]) -> Violation | None:
    if tool_name == "Bash":
        return _classify_bash(str(tool_input.get("command", "")), worktree)
    key = FILE_TOOLS.get(tool_name)
    if key and tool_input.get(key):
        return _classify_path(str(tool_input[key]), worktree, protected_paths)
    return None


# Bash

def _classify_bash(command: str, worktree: Path | None) -> Violation | None:
    if not command.strip():
        return None
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = _tokens(segment)
        tokens = [t for t in tokens if "=" not in t or not re.match(r"^\w+=", t)]  # drop VAR=x prefixes
        if not tokens:
            continue
        head = tokens[0].lower()
        if head in ("sudo", "cmd", "powershell", "pwsh") and len(tokens) > 1 and head == "sudo":
            tokens, head = tokens[1:], tokens[1].lower()
        if head == "git" and len(tokens) > 1:
            hit = _classify_git(tokens[1:])
            if hit:
                return hit
        if head == "rm" and any(t.startswith("-") and not t.startswith("--") and ("r" in t or "R" in t) or t == "--recursive" for t in tokens[1:]):
            return Violation("recursive_delete", "recursive delete")
        if head == "python" and tokens[1:3] == ["-m", "pip"] and len(tokens) > 3 and tokens[3] == "uninstall":
            return Violation("dependency_removal", "dependency removal")
        if (head, tokens[1].lower() if len(tokens) > 1 else "") in _DEP_REMOVAL:
            return Violation("dependency_removal", "dependency removal")
    text = command
    if _PS_RECURSE_RE.search(text) or _CMD_RECURSE_RE.search(text):
        return Violation("recursive_delete", "recursive delete")
    if _SQL_RE.search(text):
        return Violation("sql_destructive", "destructive SQL")
    if worktree is not None:
        for match in (*_REDIRECT_RE.finditer(text), *_TEE_RE.finditer(text)):
            if not _inside(_normalize(match.group(1)), worktree):
                return Violation("write_outside_worktree", f"write to {match.group(1)} outside the worktree")
    return None


def _classify_git(args: list[str]) -> Violation | None:
    sub = args[0].lower()
    flags = {a.lower() for a in args[1:]}
    if sub == "push":
        if flags & {"--force", "-f", "--force-with-lease"}:
            return Violation("git_force_push", "git force push")
        if "--delete" in flags or "-d" in flags or any(a.startswith(":") and len(a) > 1 for a in args[1:]):
            return Violation("git_branch_delete", "remote branch deletion")
    if sub == "reset" and "--hard" in flags:
        return Violation("git_reset_hard", "git reset --hard")
    if sub == "branch" and flags & {"-d", "-D", "--delete"}:
        return Violation("git_branch_delete", "branch deletion")
    if sub == "clean" and any(a.startswith("-") and not a.startswith("--") and ("f" in a or "x" in a) for a in args[1:]):
        return Violation("git_clean", "git clean")
    return None


def _tokens(segment: str) -> list[str]:
    try:
        return [t.strip('"') for t in shlex.split(segment, posix=False)]
    except ValueError:
        return segment.split()


# paths

def _classify_path(raw: str, worktree: Path | None, protected_paths: list[str]) -> Violation | None:
    path = _normalize(raw)
    if worktree is not None:
        if path.is_absolute() and not _inside(path, worktree):
            return Violation("write_outside_worktree", f"{raw} is outside the worktree")
        relative = path.relative_to(worktree.resolve()).as_posix() if path.is_absolute() else Path(raw).as_posix()
    else:
        relative = path.as_posix()
    for pattern in protected_paths:
        if glob_match(pattern, relative):
            return Violation("protected_path", f"{relative} matches protected path {pattern}")
    return None


def _normalize(raw: str) -> Path:
    text = raw.strip().strip('"').strip("'")
    match = re.match(r"^/([A-Za-z])/(.*)$", text)  # Git Bash style /c/dev/x
    if match:
        text = f"{match.group(1).upper()}:/{match.group(2)}"
    path = Path(text)
    return path.resolve() if path.is_absolute() else path


def _inside(path: Path, root: Path) -> bool:
    try:
        PureWindowsPath(str(path.resolve())).relative_to(PureWindowsPath(str(root.resolve())))
        return True
    except ValueError:
        return False


def glob_match(pattern: str, relative_posix: str) -> bool:
    """fnmatch with `**` crossing directories. `.github/**` matches `.github/workflows/ci.yml`."""
    regex = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif ch == "*":
            regex += "[^/]*"
            i += 1
        elif ch == "?":
            regex += "[^/]"
            i += 1
        else:
            regex += re.escape(ch)
            i += 1
    return re.fullmatch(regex, relative_posix) is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_guard_rules.py -q`
Expected: all pass. If `_inside` fails on non-existent paths, note that `Path.resolve()` on Windows does not require the path to exist (strict=False); keep it.

- [ ] **Step 5: Commit**

```bash
git add src/orq/guard tests/test_guard_rules.py
git commit -m "feat(guard): pure classification rules for destructive tool calls"
```

### Task 2: hook script and settings writer

**Files:**
- Create: `src/orq/guard/hook.py`, `src/orq/guard/settings.py`
- Test: `tests/test_guard_hook.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guard_hook.py
import json
import subprocess
import sys
from pathlib import Path

from orq.guard.rules import action_key
from orq.guard.settings import write_hook_settings

HOOK = Path("src/orq/guard/hook.py").resolve()


def run_hook(run_dir: Path, payload: dict) -> subprocess.CompletedProcess:
    env = {"ORQ_RUN_DIR": str(run_dir), "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload).encode("utf-8"), capture_output=True, env=env)


def payload(tool: str, tool_input: dict) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": "C:/wt", "session_id": "s"}


def prepare(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "allow_tokens").mkdir(parents=True)
    write_hook_settings(run_dir, worktree=tmp_path / "wt", protected_paths=[".github/**"])
    return run_dir


def test_settings_file_points_at_this_python_and_hook(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    settings = json.loads((run_dir / "claude-settings.json").read_text(encoding="utf-8"))
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash|Write|Edit|MultiEdit|NotebookEdit"
    command = hook["hooks"][0]["command"]
    assert "\\" not in command and sys.executable.replace("\\", "/") in command and "hook.py" in command
    guard = json.loads((run_dir / "guard.json").read_text(encoding="utf-8"))
    assert guard["protected_paths"] == [".github/**"] and guard["worktree"].endswith("wt")


def test_destructive_call_is_denied_with_json(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, payload("Bash", {"command": "git reset --hard HEAD~1"}))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "git_reset_hard" in out["permissionDecisionReason"]
    assert "Do not work around it" in out["permissionDecisionReason"]
    log = [json.loads(l) for l in (run_dir / "guard.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["decision"] == "deny" and log[-1]["action_key"] == action_key("Bash", {"command": "git reset --hard HEAD~1"})


def test_token_allows_once(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    tool_input = {"command": "git reset --hard HEAD~1"}
    token = run_dir / "allow_tokens" / action_key("Bash", tool_input)
    token.write_text("approved", encoding="utf-8")
    first = run_hook(run_dir, payload("Bash", tool_input))
    assert first.returncode == 0 and first.stdout.strip() == b"" and not token.exists()
    second = run_hook(run_dir, payload("Bash", tool_input))
    assert json.loads(second.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_safe_call_prints_nothing(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, payload("Bash", {"command": "git status"}))
    assert proc.returncode == 0 and proc.stdout.strip() == b""
    log = [json.loads(l) for l in (run_dir / "guard.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["decision"] == "allow"


def test_write_outside_worktree_uses_guard_json(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, payload("Write", {"file_path": str(tmp_path / "elsewhere.txt")}))
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    proc = run_hook(run_dir, payload("Write", {"file_path": str(tmp_path / "wt" / "ok.txt")}))
    assert proc.stdout.strip() == b""


def test_malformed_payload_allows_and_logs(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = subprocess.run([sys.executable, str(HOOK)], input=b"not json", capture_output=True, env={"ORQ_RUN_DIR": str(run_dir)})
    assert proc.returncode == 0 and proc.stdout.strip() == b""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guard_hook.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'orq.guard.settings'`

- [ ] **Step 3: Implement settings writer and hook**

```python
# src/orq/guard/settings.py
"""Writes the per-run Claude Code settings that attach the guard hook (SPEC 10.3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOK_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"
SETTINGS_NAME = "claude-settings.json"
GUARD_CONFIG_NAME = "guard.json"


def hook_script() -> Path:
    return Path(__file__).with_name("hook.py").resolve()


def write_hook_settings(run_dir: Path, *, worktree: Path, protected_paths: list[str]) -> Path:
    """Write claude-settings.json and guard.json into run_dir; return the settings path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    command = f'"{Path(sys.executable).as_posix()}" "{hook_script().as_posix()}"'
    settings = {"hooks": {"PreToolUse": [{"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]}]}}
    path = run_dir / SETTINGS_NAME
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    (run_dir / GUARD_CONFIG_NAME).write_text(
        json.dumps({"worktree": str(worktree), "protected_paths": protected_paths}, indent=2), encoding="utf-8"
    )
    return path
```

```python
# src/orq/guard/hook.py
"""PreToolUse hook executed by Claude Code for every Bash/Write/Edit call (SPEC 10.3).

Reads the payload on stdin, denies destructive actions unless a one-time token exists,
logs every decision to <ORQ_RUN_DIR>/guard.jsonl. Any internal failure allows the call and logs it:
the post-execution guard and the reviewer are the next layers.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/, so `orq.guard.rules` imports without install

from orq.guard.rules import Violation, action_key, classify, describe_action  # noqa: E402

REASON = ("orq guard: {description} is destructive and needs owner approval (rule {rule}). "
          "Do not work around it with another command. Finish any independent work, then end your turn "
          "and state the exact command you need and why.")


def main() -> int:
    run_dir = Path(os.environ.get("ORQ_RUN_DIR", str(Path.home() / ".orq" / "guard-orphan")))
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        _log(run_dir, {"decision": "allow", "error": f"bad payload: {exc}"})
        return 0
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    config = _load_config(run_dir)
    worktree = Path(config["worktree"]) if config.get("worktree") else None
    try:
        violation = classify(tool_name, tool_input, worktree=worktree, protected_paths=config.get("protected_paths", []))
    except Exception as exc:  # noqa: BLE001 - a hook crash must never block the implementer
        _log(run_dir, {"decision": "allow", "tool_name": tool_name, "error": repr(exc)})
        return 0
    key = action_key(tool_name, tool_input)
    record = {"tool_name": tool_name, "tool_input": tool_input, "action_key": key, "session_id": payload.get("session_id")}
    if violation is None:
        _log(run_dir, {"decision": "allow", **record})
        return 0
    token = run_dir / "allow_tokens" / key
    if token.exists():
        token.unlink()
        _log(run_dir, {"decision": "allow-by-token", "rule": violation.rule, **record})
        return 0
    _log(run_dir, {"decision": "deny", "rule": violation.rule, **record})
    _deny(violation, tool_name, tool_input)
    return 0


def _deny(violation: Violation, tool_name: str, tool_input: dict) -> None:
    reason = REASON.format(description=f"`{describe_action(tool_name, tool_input)}`", rule=violation.rule)
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    sys.stdout.flush()


def _load_config(run_dir: Path) -> dict:
    path = run_dir / "guard.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _log(run_dir: Path, record: dict) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "guard.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_guard_hook.py -q`
Expected: all pass. The hook runs under a bare environment (`PATH=""`), proving it only needs `python.exe` and the repo checkout.

- [ ] **Step 5: Commit**

```bash
git add src/orq/guard tests/test_guard_hook.py
git commit -m "feat(guard): PreToolUse hook with one-time allow tokens and per-run settings"
```

### Task 3: implementer adapter attaches the hook

**Files:**
- Modify: `src/orq/adapters/base.py` (`stream_process`), `src/orq/adapters/claude.py` (`ClaudeImplementer.run`)
- Modify: `src/orq/core/prompts.py` (`IMPLEMENTER_RULES`)
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_adapters.py` (reuse that file's existing fixtures for running `ClaudeImplementer` against `fake_claude.py`; they record argv to `FAKE_RECORD`):

```python
def test_implementer_adds_settings_when_run_dir_has_hook(tmp_path: Path, fake_claude_prefix, record) -> None:
    from orq.guard.settings import write_hook_settings
    run_dir = tmp_path / "run"
    write_hook_settings(run_dir, worktree=tmp_path / "wt", protected_paths=[])
    impl = ClaudeImplementer(model="sonnet", system_prompt="rules", argv_prefix=fake_claude_prefix)
    asyncio.run(impl.run("hi", cwd=tmp_path, log_path=tmp_path / "log.jsonl", run_dir=run_dir))
    argv = record()["argv"]
    assert argv[argv.index("--settings") + 1] == str(run_dir / "claude-settings.json")
    assert (run_dir / "child.pid").read_text(encoding="utf-8").strip().isdigit()


def test_implementer_without_hook_settings_has_no_settings_flag(tmp_path: Path, fake_claude_prefix, record) -> None:
    impl = ClaudeImplementer(model="sonnet", system_prompt="rules", argv_prefix=fake_claude_prefix)
    asyncio.run(impl.run("hi", cwd=tmp_path, log_path=tmp_path / "log.jsonl", run_dir=tmp_path / "run"))
    assert "--settings" not in record()["argv"]
```

Adapt fixture names to what `tests/test_adapters.py` already defines (read the file first; it has a helper that builds the fake argv prefix and reads the record JSON).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_adapters.py -q -k settings`
Expected: FAIL on `--settings` not in argv.

- [ ] **Step 3: Implement**

In `src/orq/adapters/base.py`, add `pid_file: Path | None = None` to `stream_process` and write the pid right after the process starts:

```python
async def stream_process(argv, *, cwd, stdin_text, log_path, env, on_event=None, pid_file: Path | None = None) -> Completed:
    ...
    proc = await asyncio.create_subprocess_exec(...)
    if pid_file is not None:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    ...
    exit_code = await proc.wait()
    if pid_file is not None:
        pid_file.unlink(missing_ok=True)
    return Completed(...)
```

In `src/orq/adapters/claude.py`, `ClaudeImplementer.run`:

```python
from orq.guard.settings import SETTINGS_NAME

        argv = [...existing..., *session_flag]
        pid_file = None
        if run_dir is not None:
            settings = run_dir / SETTINGS_NAME
            if settings.exists():
                argv += ["--settings", str(settings)]
            pid_file = run_dir / "child.pid"
        argv += self._extra
        env = clean_env({"ORQ_RUN_DIR": str(run_dir)} if run_dir else None)
        completed = await stream_process(argv, cwd=cwd, stdin_text=prompt, log_path=log_path, env=env, on_event=on_event, pid_file=pid_file)
```

In `src/orq/core/prompts.py`, add to `IMPLEMENTER_RULES` after the "Never add secrets" line:

```
- A tool call may be denied by the orq guard. Follow the denial text exactly: never work around a denied action with another command or tool.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_adapters.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/adapters/base.py src/orq/adapters/claude.py src/orq/core/prompts.py tests/test_adapters.py
git commit -m "feat(guard): implementer runs with the per-run hook settings and records its pid"
```

### Task 4: runner turns denials into decisions (interactive path)

This task adds the denial decision flow to the *current* `Runner` shape (decisions asked through the `human` callback). Milestone 4 moves the same logic onto the checkpoint. Keep the code in small methods so it transfers.

**Files:**
- Modify: `src/orq/core/loop.py`, `src/orq/core/prompts.py`
- Test: `tests/test_loop.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_loop.py`:

```python
def denied(command: str, text: str = "I need to run that command.") -> AgentResult:
    return AgentResult(ok=True, text=text, permission_denials=[{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": command}}])


def test_guard_denial_becomes_destructive_decision_and_approve_writes_token(env) -> None:
    make, paths, _ = env
    answers: list[Decision] = []

    def human(decision: Decision) -> str:
        answers.append(decision)
        return "approve"

    implementer = FakeImplementer([denied("git reset --hard HEAD~1"), ok()])
    reviewer = FakeReviewer([review("continue", "carry on"), review("done", None)])
    runner = make(implementer, reviewer, human=human)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    guard = [d for d in answers if d.source == "guard"]
    assert len(guard) == 1 and guard[0].destructive and guard[0].decision_type == "risk"
    assert "git reset --hard HEAD~1" in guard[0].question
    from orq.guard.rules import action_key
    assert (paths.allow_tokens(runner.run_id) / action_key("Bash", {"command": "git reset --hard HEAD~1"})).exists()
    assert "approved this action" in implementer.prompts[1] and "git reset --hard HEAD~1" in implementer.prompts[1]
    assert "carry on" in implementer.prompts[1]


def test_guard_denial_denied_by_owner_tells_implementer_to_proceed(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([denied("rm -rf build"), ok()])
    reviewer = FakeReviewer([review("continue", "carry on"), review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: "deny")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert "denied this action" in implementer.prompts[1] and "rm -rf build" in implementer.prompts[1]
    assert not any(paths.allow_tokens(runner.run_id).iterdir())


def test_same_denied_action_twice_in_one_turn_asks_once(env) -> None:
    make, paths, _ = env
    result = AgentResult(ok=True, text="x", permission_denials=[
        {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git clean -fdx"}},
        {"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "git clean -fdx"}},
    ])
    asked: list[Decision] = []
    runner = make(FakeImplementer([result, ok()]), FakeReviewer([review("continue"), review("done", None)]),
                  human=lambda d: asked.append(d) or "deny")
    asyncio.run(runner.execute())
    assert len([d for d in asked if d.source == "guard"]) == 1


def test_denial_without_other_work_skips_review(env) -> None:
    make, paths, _ = env
    implementer = NoWriteImplementer([denied("git reset --hard"), ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: "approve")
    final = asyncio.run(runner.execute())
    assert final is RunState.DONE
    assert len(reviewer.prompts) == 1  # the empty first iteration was not reviewed
```

Add `NoWriteImplementer` next to `FakeImplementer`: same class but writes a file only when `self.calls > 1`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_loop.py -q -k "guard or denial"`
Expected: FAIL (no guard decision asked; `approved this action` missing).

- [ ] **Step 3: Implement**

In `src/orq/core/prompts.py` add:

```python
def guard_outcome_lines(approved: list[str], denied: list[str]) -> str:
    lines = [f"- The owner approved this action; run exactly: `{a}`" for a in approved]
    lines += [f"- The owner denied this action: `{d}`. Proceed without it." for d in denied]
    return "## Guard decisions\n" + "\n".join(lines) if lines else ""
```

In `src/orq/core/loop.py`:

1. In `__init__`, after `self.rundir.create(task_text)`, write the hook settings:

```python
from orq.guard.settings import write_hook_settings
from orq.guard.rules import action_key, describe_action
        write_hook_settings(self.rundir.path, worktree=self.worktree, protected_paths=config.git.protected_paths)
```

2. New helper collecting unique denied actions from an `AgentResult`:

```python
    def _denied_actions(self, result: AgentResult) -> list[dict]:
        seen: dict[str, dict] = {}
        for denial in result.permission_denials:
            tool, tool_input = str(denial.get("tool_name", "")), denial.get("tool_input") or {}
            key = action_key(tool, tool_input)
            seen.setdefault(key, {"key": key, "tool_name": tool, "tool_input": tool_input, "description": describe_action(tool, tool_input)})
        return list(seen.values())
```

3. New helper asking the owner about each denied action and returning the prompt block:

```python
    def _ask_guard_actions(self, actions: list[dict]) -> str:
        approved, denied = [], []
        for action in actions:
            answer = self._ask(Decision(decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                                        destructive=True, options=["approve", "deny"], recommendation=1,
                                        question=f"The implementer tried a destructive action: {action['description']}. Allow it once?"))
            if answer.lower() == "approve":
                (self.paths.allow_tokens(self.run_id) / action["key"]).write_text("approved", encoding="utf-8")
                self.rundir.event("guard_token", action_key=action["key"], description=action["description"])
                approved.append(action["description"])
            else:
                denied.append(action["description"])
        return guard_outcome_lines(approved, denied)
```

4. In `_iterate`, after the decision-marker check and before `VERIFYING`: collect `actions = self._denied_actions(result)`. After staging (`self._scan_staged(itdir)` already stages), if `actions` and `not self.git.staged_files(self.worktree)`: skip the check/commit/review, ask the guard decisions and return `_IterationOutcome(next_prompt=block, milestone=previous.milestone, done=False), previous_check`. Otherwise run the iteration as today and, right before each `return _IterationOutcome(...)` at the end of `_iterate`, if `actions`: `block = self._ask_guard_actions(actions)` and append `"\n\n" + block` to the outcome's `next_prompt` (or use the block alone when `next_prompt` is None). Also strip `done` to `False` when actions exist (an approved destructive action still has to run).

5. Add `GitManager.staged_files(worktree) -> list[str]` returning `git diff --cached --name-only` lines.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all pass, including the existing loop tests.

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/loop.py src/orq/core/prompts.py src/orq/git/manager.py tests/test_loop.py
git commit -m "feat(guard): denied tool calls become destructive decisions with one-time tokens"
```

---

## Milestone 2: post-execution diff rules

### Task 5: diff rules module

**Files:**
- Create: `src/orq/guard/diff_rules.py`
- Modify: `src/orq/git/manager.py` (staged diff helpers), `src/orq/config.py` (`[guard]`)
- Test: `tests/test_diff_rules.py`, `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_diff_rules.py
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


def test_deleted_file(repo: Path) -> None:
    (repo / "routes.py").unlink()
    assert "deleted_file" in rules(repo)


def test_removed_test(repo: Path) -> None:
    (repo / "tests" / "test_app.py").write_text("x = 1\n", encoding="utf-8")
    assert "removed_test" in rules(repo)


def test_renamed_test_is_not_a_removal(repo: Path) -> None:
    (repo / "tests" / "test_app.py").write_text("def test_public():\n    assert public() == 1\n\ndef test_more():\n    pass\n", encoding="utf-8")
    assert rules(repo) == []


def test_removed_export_but_private_ok(repo: Path) -> None:
    (repo / "app.py").write_text("def public():\n    return 1\n", encoding="utf-8")
    assert rules(repo) == []  # only _private removed
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


def test_dependency_removed(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[project]\ndependencies = [\n  "typer>=0.12",\n]\n', encoding="utf-8")
    assert "dependency_removed" in rules(repo)


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
```

Add to `tests/test_config.py`:

```python
def test_guard_section_and_optional_sandbox(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[guard]\nmax_net_deleted_lines = 10\n[limits]\nrate_limit_retries = 1\n', encoding="utf-8")
    config = load_config(path)
    assert config.guard.max_net_deleted_lines == 10 and config.limits.rate_limit_retries == 1
    assert config.git.sandbox_repos == [] and "**/*.py" in config.guard.source_globs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_diff_rules.py tests/test_config.py -q`
Expected: FAIL with import errors for `GuardConfig` and `orq.guard.diff_rules`.

- [ ] **Step 3: Implement**

`src/orq/config.py`: add

```python
@dataclass
class GuardConfig:
    max_net_deleted_lines: int = 300
    source_globs: list[str] = field(default_factory=lambda: ["**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.rs", "**/*.go", "**/*.java", "**/*.cs"])
```

`LimitsConfig.rate_limit_retries: int = 3`. `Config.guard: GuardConfig = field(default_factory=GuardConfig)`. Change the `sandbox_repos` comment to "Optional allowlist; empty allows any repo."

`src/orq/git/manager.py`: add

```python
    def staged_files(self, worktree: Path) -> list[str]:
        return [l for l in self.git("diff", "--cached", "--name-only", cwd=worktree).splitlines() if l.strip()]

    def staged_name_status(self, worktree: Path) -> list[tuple[str, str]]:
        """[(status, path)] for the index vs HEAD; renames report as ('R', new_path')."""
        out = []
        for line in self.git("diff", "--cached", "--name-status", "-M", cwd=worktree).splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append((parts[0][0], parts[-1]))
        return out

    def staged_numstat(self, worktree: Path) -> list[tuple[int, int, str]]:
        out = []
        for line in self.git("diff", "--cached", "--numstat", cwd=worktree).splitlines():
            added, deleted, path = line.split("\t", 2)
            if added != "-":
                out.append((int(added), int(deleted), path))
        return out

    def staged_patch(self, worktree: Path) -> str:
        return self.git("diff", "--cached", "--no-color", "-M", cwd=worktree)

    def reset_hard(self, worktree: Path, sha: str) -> None:
        self.git("reset", "-q", "--hard", sha, cwd=worktree)
        self.git("clean", "-fdq", cwd=worktree)
```

`src/orq/guard/diff_rules.py`:

```python
"""Post-execution guard: deterministic rules over the staged index (SPEC 10.4 and 9.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orq.config import GuardConfig
from orq.git.manager import GitManager
from orq.guard.rules import glob_match

_TEST_DEFS = [re.compile(p) for p in (
    r"^\s*(?:async\s+)?def\s+(test_\w+)", r"^\s*(?:it|test|describe)\(\s*['\"`]([^'\"`]+)", r"^\s*fn\s+(test_\w+)", r"^\s*func\s+(Test\w+)")]
_EXPORT_DEFS = [re.compile(p) for p in (
    r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(", r"^class\s+([A-Za-z]\w*)",
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+(\w+)",
    r"^\s*pub\s+(?:async\s+)?fn\s+(\w+)", r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)")]
_ROUTE_RE = re.compile(r"^\s*(?:@\w+\.(?:get|post|put|delete|patch|route)\(|\b(?:app|router)\.(?:get|post|put|delete|patch)\()")
_MANIFESTS = re.compile(r"(^|/)(pyproject\.toml|requirements[^/]*\.txt|package\.json|Cargo\.toml|go\.mod)$")
_DEP_LINE = re.compile(r"""^\s*["']?([A-Za-z0-9_.@/-]+)["']?\s*(?:[><=~!^\[]|:\s*["'][\^~]?\d|\s+v\d|=\s*["{])""")
_HUNK_FILE = re.compile(r"^diff --git a/(.*?) b/(.*)$")


@dataclass(frozen=True)
class DiffViolation:
    rule: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}: {self.detail}"


def evaluate_staged(git: GitManager, worktree: Path, *, protected_paths: list[str], guard: GuardConfig) -> list[DiffViolation]:
    violations: list[DiffViolation] = []
    for status, path in git.staged_name_status(worktree):
        if status == "D":
            violations.append(DiffViolation("deleted_file", path, "file deleted"))
        for pattern in protected_paths:
            if glob_match(pattern, path):
                violations.append(DiffViolation("protected_path", path, f"matches {pattern}"))
                break
    removed, added = _split_patch(git.staged_patch(worktree))
    added_text = "\n".join(line for lines in added.values() for line in lines)
    for path, lines in removed.items():
        for line in lines:
            name = _first_match(_TEST_DEFS, line)
            if name and name not in added_text:
                violations.append(DiffViolation("removed_test", path, f"test {name} removed"))
                continue
            name = _first_match(_EXPORT_DEFS, line)
            if name and not name.startswith("_") and not re.search(rf"\b{re.escape(name)}\b", added_text):
                violations.append(DiffViolation("removed_export", path, f"{name} removed"))
                continue
            if _ROUTE_RE.search(line) and line.strip() not in added_text:
                violations.append(DiffViolation("removed_route", path, line.strip()))
                continue
            if _MANIFESTS.search(path):
                dep = _DEP_LINE.match(line)
                if dep and not any(_DEP_LINE.match(a) and _DEP_LINE.match(a).group(1) == dep.group(1) for a in added.get(path, [])):
                    violations.append(DiffViolation("dependency_removed", path, f"{dep.group(1)} removed"))
    net = 0
    for plus, minus, path in git.staged_numstat(worktree):
        if any(glob_match(g, path) for g in guard.source_globs):
            net += minus - plus
    if net > guard.max_net_deleted_lines:
        violations.append(DiffViolation("negative_balance", "*", f"{net} more source lines deleted than added (limit {guard.max_net_deleted_lines})"))
    return violations


def _split_patch(patch: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    removed: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}
    current = ""
    for line in patch.splitlines():
        header = _HUNK_FILE.match(line)
        if header:
            current = header.group(2)
            continue
        if line.startswith(("---", "+++", "@@", "diff ", "index ", "similarity", "rename", "new file", "deleted file")):
            continue
        if line.startswith("-"):
            removed.setdefault(current, []).append(line[1:])
        elif line.startswith("+"):
            added.setdefault(current, []).append(line[1:])
    return removed, added


def _first_match(patterns: list[re.Pattern], line: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_diff_rules.py tests/test_config.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/guard/diff_rules.py src/orq/git/manager.py src/orq/config.py tests/test_diff_rules.py tests/test_config.py
git commit -m "feat(guard): post-execution diff rules over the staged index"
```

### Task 6: runner applies diff rules

**Files:**
- Modify: `src/orq/core/loop.py`
- Test: `tests/test_loop.py`

- [ ] **Step 1: Write the failing tests**

```python
class DeletingImplementer(FakeImplementer):
    """First call deletes README.md (a deleted_file violation), later calls behave like FakeImplementer."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        if self.calls == 0:
            (cwd / "README.md").unlink()
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event)


def test_diff_rule_violation_denied_resets_worktree(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = DeletingImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: asked.append(d) or "deny")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    guard = [d for d in asked if d.source == "guard"]
    assert guard and "deleted_file" in guard[0].question and guard[0].destructive
    assert (runner.worktree / "README.md").exists()            # reset restored it
    assert "rejected these changes" in implementer.prompts[1]
    assert len(reviewer.prompts) == 1                           # iteration 1 was not reviewed
    log = git("log", "--format=%s", cwd=runner.worktree).splitlines()
    assert len([l for l in log if l.startswith("orq(")]) == 1


def test_diff_rule_violation_approved_commits(env) -> None:
    make, paths, _ = env
    implementer = DeletingImplementer([ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: "approve")

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    assert not (runner.worktree / "README.md").exists()
    assert "deleted_file" in runner.rundir.decisions_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_loop.py -q -k diff_rule`
Expected: FAIL (no guard decision; README still deleted in the deny test).

- [ ] **Step 3: Implement**

In `_iterate`, after `self._scan_staged(itdir)` (which stages and scans) and before the check:

```python
        violations = evaluate_staged(self.git, self.worktree, protected_paths=self.config.git.protected_paths, guard=self.config.guard)
        if violations:
            self.rundir.event("diff_rules", iteration=iteration, violations=[str(v) for v in violations])
            listing = "\n".join(f"- {v}" for v in violations)
            answer = self._ask(Decision(decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                                        destructive=True, options=["approve", "deny"], recommendation=1,
                                        question=f"The diff guard flagged these changes:\n{listing}\nAllow them?"))
            if answer.lower() != "approve":
                self.git.reset_hard(self.worktree, self._last_commit or since)
                self.rundir.event("rollback_iteration", iteration=iteration, to=self._last_commit)
                block = "The owner rejected these changes:\n" + listing + "\nThe worktree was reset to the previous iteration. Proceed without them."
                return _IterationOutcome(next_prompt=block, milestone=previous.milestone, done=False), previous_check
```

Move `since = self._last_commit or self.git.head(self.worktree)` above this block. The approve path falls through; `_ask` already appends the question (with the violation list) to `DECISIONS.md`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/loop.py tests/test_loop.py
git commit -m "feat(guard): diff rule violations pause the run; deny resets the worktree"
```

---

## Milestone 3: no-progress detection and rollback

### Task 7: progress tracker

**Files:**
- Create: `src/orq/core/progress.py`
- Test: `tests/test_progress.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_progress.py
from orq.core.progress import ProgressTracker


def test_same_diff_twice_fires_and_targets_iteration_before_streak() -> None:
    t = ProgressTracker()
    assert t.record(1, diff_hash="a", failure_signature=None, next_prompt="do x") is None
    assert t.record(2, diff_hash="b", failure_signature=None, next_prompt="do y") is None
    assert t.record(3, diff_hash="b", failure_signature=None, next_prompt="do z") is not None
    rule = t.record(3, diff_hash="b", failure_signature=None, next_prompt="do z")  # idempotent re-record is not required; new object instead
    

def test_same_diff_rule_details() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="p1")
    rule = t.record(2, diff_hash="a", failure_signature=None, next_prompt="p2")
    assert rule.rule == "same_diff" and rule.rollback_to == 0


def test_same_failure_three_times() -> None:
    t = ProgressTracker()
    assert t.record(1, diff_hash="a", failure_signature="f", next_prompt="p1") is None
    assert t.record(2, diff_hash="b", failure_signature="f", next_prompt="p2") is None
    rule = t.record(3, diff_hash="c", failure_signature="f", next_prompt="p3")
    assert rule.rule == "same_failure" and rule.rollback_to == 0


def test_passing_checks_do_not_count_as_same_failure() -> None:
    t = ProgressTracker()
    for i in range(1, 4):
        assert t.record(i, diff_hash=str(i), failure_signature=None, next_prompt=f"p{i}") is None


def test_near_identical_prompt_fires() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="Please add tests for the parser module and fix lint")
    rule = t.record(2, diff_hash="b", failure_signature=None, next_prompt="Please add tests for the parser module and fix lint.")
    assert rule.rule == "same_prompt" and rule.rollback_to == 0


def test_reset_clears_streak_and_history_roundtrips() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="p")
    t.record(2, diff_hash="a", failure_signature=None, next_prompt="q")
    t.reset()
    assert t.record(3, diff_hash="a", failure_signature=None, next_prompt="r") is None
    again = ProgressTracker(t.history)
    assert again.record(4, diff_hash="a", failure_signature=None, next_prompt="s").rule == "same_diff"
```

Remove the stray first test body lines after `is not None` (keep the test as three asserts).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_progress.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/orq/core/progress.py
"""No-progress detection (SPEC 10.2). History is plain dicts so it serializes into state.json."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

PROMPT_SIMILARITY = 0.9


@dataclass(frozen=True)
class ProgressRule:
    rule: str          # same_diff | same_failure | same_prompt
    rollback_to: int   # last iteration before the streak began (0 = base commit)
    detail: str


class ProgressTracker:
    def __init__(self, history: list[dict] | None = None) -> None:
        self.history: list[dict] = list(history or [])

    def record(self, iteration: int, *, diff_hash: str, failure_signature: str | None, next_prompt: str | None) -> ProgressRule | None:
        self.history.append({"iteration": iteration, "diff_hash": diff_hash, "failure_signature": failure_signature,
                             "next_prompt": next_prompt or ""})
        h = self.history
        if len(h) >= 2 and h[-1]["diff_hash"] == h[-2]["diff_hash"]:
            return ProgressRule("same_diff", h[-2]["iteration"] - 1, "the last two iterations produced the same diff")
        if len(h) >= 3 and h[-1]["failure_signature"] and h[-1]["failure_signature"] == h[-2]["failure_signature"] == h[-3]["failure_signature"]:
            return ProgressRule("same_failure", h[-3]["iteration"] - 1, "the check command failed the same way three times")
        if len(h) >= 2 and h[-1]["next_prompt"] and h[-2]["next_prompt"]:
            ratio = SequenceMatcher(None, h[-1]["next_prompt"], h[-2]["next_prompt"]).ratio()
            if ratio >= PROMPT_SIMILARITY:
                return ProgressRule("same_prompt", h[-2]["iteration"] - 1, f"the reviewer repeated its instructions (similarity {ratio:.2f})")
        return None

    def reset(self) -> None:
        self.history.clear()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_progress.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/progress.py tests/test_progress.py
git commit -m "feat(core): no-progress detection over diff, failure and prompt history"
```

### Task 8: runner uses the tracker and can roll back

**Files:**
- Modify: `src/orq/core/loop.py`, `src/orq/core/prompts.py`
- Test: `tests/test_loop.py`

- [ ] **Step 1: Write the failing tests**

```python
class SameDiffImplementer(FakeImplementer):
    """Always rewrites the same file with the same content, so every iteration's diff hash repeats."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        self.prompts.append(prompt)
        self.calls += 1
        (cwd / "same.txt").write_text(f"version {self.calls}\n", encoding="utf-8")
        log_path.write_text("{}\n", encoding="utf-8")
        return self.results.pop(0)


def test_no_progress_asks_and_rollback_discards_iterations(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []

    def human(decision: Decision) -> str:
        asked.append(decision)
        return decision.options[1] if decision.source == "orq" else "0"

    implementer = SameDiffImplementer([ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "p1"), review("continue", "p2"), review("done", None)])
    runner = make(implementer, reviewer, human=human)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE
    blocked = [d for d in asked if d.decision_type == "blocked"]
    assert blocked and blocked[0].options[1] == "rollback to iteration 1"
    assert "same_diff" in blocked[0].question
    log = git("log", "--format=%s", cwd=runner.worktree).splitlines()
    assert log[0].startswith(f"orq({runner.run_id}) iter 3:") and log[1].startswith(f"orq({runner.run_id}) iter 1:")
    assert "discarded" in implementer.prompts[2].lower() and "discarded" in reviewer.prompts[2].lower()


def test_no_progress_continue_resets_streak(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = SameDiffImplementer([ok(), ok(), ok()])
    reviewer = FakeReviewer([review("continue", "p1"), review("continue", "p2"), review("done", None)])
    runner = make(implementer, reviewer, human=lambda d: asked.append(d) or "continue")
    asyncio.run(runner.execute())
    assert len([d for d in asked if d.decision_type == "blocked"]) == 1


def test_no_progress_abort(env) -> None:
    make, paths, _ = env
    implementer = SameDiffImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("continue", "p1"), review("continue", "p2")])
    runner = make(implementer, reviewer, human=lambda d: "abort")
    assert asyncio.run(runner.execute()) is RunState.ABORTED
```

Note: the diff hash is computed over the patch text with the `version N` line, so these diffs differ. Make `SameDiffImplementer` write the constant string `"same\n"` instead; a second identical write gives an empty diff versus the previous commit, and two empty diffs in a row fire `same_diff`. Adjust: the first iteration creates `same.txt` (non-empty diff), the second and third rewrite the identical content (empty diffs, `commit_staged` returns `None`). Expected log then has only `iter 1` and `iter 3`? With empty diffs no commit happens, so after rollback the log has `iter 1` only and iteration 3 also commits nothing. Change the final assertions to: `assert [l for l in log if l.startswith("orq(")] == [f"orq({runner.run_id}) iter 1: I changed things"]` and keep the `discarded` assertions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_loop.py -q -k no_progress`
Expected: FAIL (no blocked decision).

- [ ] **Step 3: Implement**

`src/orq/core/prompts.py`: both prompt builders accept `discarded: str | None = None` and, when set, append `"## Discarded iterations\n" + discarded`.

`src/orq/core/loop.py`:

* `self.progress = ProgressTracker()`, `self._commits: dict[int, str] = {}`, `self._discarded: str | None = None`, `self._summaries: dict[int, str] = {}` in `__init__`.
* After each commit in `_iterate`: `self._commits[iteration] = commit or self._last_commit`. After each review: `self._summaries[iteration] = review.get("summary", "")`.
* After computing the outcome in `_iterate` (every non-decision return path that reached the review), call:

```python
    def _check_progress(self, iteration: int, patch: str, check: CheckResult, next_prompt: str | None) -> str | None:
        """Returns a replacement next_prompt when the owner rolled back, else None. May raise _Stop."""
        rule = self.progress.record(iteration, diff_hash=hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16],
                                    failure_signature=None if check.ok else check.signature, next_prompt=next_prompt)
        if rule is None:
            return None
        self.rundir.event("no_progress", iteration=iteration, rule=rule.rule, rollback_to=rule.rollback_to)
        target = f"rollback to iteration {rule.rollback_to}"
        answer = self._ask(Decision(decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                                    question=f"No progress ({rule.rule}): {rule.detail}. What now?", options=["continue", target, "abort"], recommendation=1))
        if answer == "abort":
            raise _Stop(RunState.ABORTED, f"owner aborted after {rule.rule}")
        self.progress.reset()
        if answer == target:
            return self.rollback_to(rule.rollback_to, upto=iteration)
        return None

    def rollback_to(self, target: int, *, upto: int) -> str:
        sha = self._commits.get(target) or self._base_commit
        self.git.reset_hard(self.worktree, sha)
        self._last_commit = sha
        discarded = [f"- iteration {i}: {self._summaries.get(i, '(no review)')}" for i in range(target + 1, upto + 1)]
        for i in range(target + 1, upto + 1):
            self._commits.pop(i, None)
        self._discarded = f"Iterations {target + 1}..{upto} were discarded by the owner:\n" + "\n".join(discarded)
        self.rundir.event("rollback", to_iteration=target, sha=sha, discarded=list(range(target + 1, upto + 1)))
        return f"The owner rolled the worktree back to iteration {target}. Start again from there.\n" + self._discarded
```

`self._base_commit` is recorded in `_setup` (the worktree HEAD right after creation). The prompt builders receive `discarded=self._discarded`; it is cleared (`self._discarded = None`) after the next review has seen it.

Wire it: in `_iterate`, right before the final `return _IterationOutcome(...)` for `continue`/`done`, `replacement = self._check_progress(iteration, patch, check, next_prompt)`; if `replacement`: return `_IterationOutcome(next_prompt=replacement, milestone=review.get("milestone"), done=False), check`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/loop.py src/orq/core/prompts.py tests/test_loop.py
git commit -m "feat(core): no-progress decisions with rollback to an earlier iteration"
```

---

## Milestone 4: checkpoints and crash resume

### Task 9: checkpoint and process helpers

**Files:**
- Create: `src/orq/core/checkpoint.py`, `src/orq/core/procs.py`
- Modify: `src/orq/core/models.py` (`RunState.PAUSED`)
- Test: `tests/test_checkpoint.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checkpoint.py
import os
import subprocess
import sys
from pathlib import Path

from orq.core.checkpoint import Checkpoint
from orq.core.procs import kill_tree, pid_alive


def test_checkpoint_roundtrip_and_atomic_write(tmp_path: Path) -> None:
    cp = Checkpoint(run_id="R1", state="IMPLEMENTING", phase="implement", iteration=2, branch="orq/x",
                    worktree="C:/wt", repo_path="C:/repo", base_commit="b", last_commit="c", commits={"1": "c"})
    cp.outcome = {"next_prompt": "go", "milestone": "m", "done": False}
    cp.pending_decision = {"decision_id": "D1", "kind": "guard_pre", "payload": {"remaining": []}}
    path = tmp_path / "state.json"
    cp.save(path)
    assert not (tmp_path / "state.json.tmp").exists()
    loaded = Checkpoint.load(path)
    assert loaded == cp and loaded.version == 1 and loaded.pid == os.getpid()


def test_missing_checkpoint_returns_none(tmp_path: Path) -> None:
    assert Checkpoint.load(tmp_path / "nope.json") is None


def test_pid_alive_and_kill_tree() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert pid_alive(proc.pid)
        kill_tree(proc.pid)
        proc.wait(timeout=10)
        assert not pid_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not pid_alive(999999)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_checkpoint.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/orq/core/checkpoint.py
"""state.json: everything the runner needs to continue after a pause or a crash (SPEC 6 and 11)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = 1


@dataclass
class Checkpoint:
    run_id: str
    state: str
    phase: str                       # setup | implement | verify | review | await | finalize | done
    branch: str
    worktree: str
    repo_path: str | None = None
    base_commit: str | None = None
    last_commit: str | None = None
    iteration: int = 0
    version: int = VERSION
    pid: int = field(default_factory=os.getpid)
    commits: dict[str, str] = field(default_factory=dict)
    summaries: dict[str, str] = field(default_factory=dict)
    implementer_session: str | None = None
    reviewer_session: str | None = None
    outcome: dict = field(default_factory=lambda: {"next_prompt": None, "milestone": None, "done": False})
    previous_check: dict | None = None
    report: str = ""
    diff_hash: str | None = None
    denied_actions: list[dict] = field(default_factory=list)
    guard_approved: list[str] = field(default_factory=list)
    guard_denied: list[str] = field(default_factory=list)
    diff_approved: bool = False
    pending_decision: dict | None = None
    progress_history: list[dict] = field(default_factory=list)
    discarded: str | None = None
    reviewer_fallback_until: float | None = None
    rate_limit_until: float | None = None
    rate_limit_retries: int = 0
    interrupted: bool = False
    started_at: str | None = None
    elapsed_seconds: float = 0.0

    def save(self, path: Path) -> None:
        self.pid = os.getpid()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Checkpoint | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise ValueError(f"state.json version {data.get('version')} is not supported")
        return cls(**data)
```

```python
# src/orq/core/procs.py
"""Process liveness and tree kill, Windows first (SPEC 15 Phase 2: crash resume)."""

from __future__ import annotations

import os
import subprocess
import sys

_STILL_ACTIVE = 259
_QUERY_LIMITED = 0x1000


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
```

`src/orq/core/models.py`: add `PAUSED = "PAUSED"` after `PAUSED_RATE_LIMIT`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_checkpoint.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/checkpoint.py src/orq/core/procs.py src/orq/core/models.py tests/test_checkpoint.py
git commit -m "feat(core): checkpoint dataclass for state.json and process helpers"
```

### Task 10: runner refactor onto the checkpoint

This is the largest task. Rewrite `src/orq/core/loop.py` so that every step reads and writes `self.cp` (a `Checkpoint`) and the loop dispatches on `self.cp.phase`. Existing tests in `tests/test_loop.py` must keep passing unchanged except where noted.

**Files:**
- Modify: `src/orq/core/loop.py`, `src/orq/core/prompts.py`, `src/orq/git/manager.py`, `src/orq/store/rundir.py`
- Test: `tests/test_resume.py`, `tests/test_loop.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_resume.py
"""Resume from each phase using fakes. The 'crash' is simulated by a fake that raises after a checkpoint."""
import asyncio
import json
from pathlib import Path

import pytest

from orq.adapters.base import AgentResult
from orq.core.checkpoint import Checkpoint
from orq.core.loop import Runner
from orq.core.models import RunState
from tests.conftest import git
from tests.test_loop import FakeImplementer, FakeReviewer, ok, review


class Crash(RuntimeError):
    pass


class CrashingImplementer(FakeImplementer):
    """Writes partial work then raises, like a killed process would leave it."""

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        if self.calls == 0:
            self.calls += 1
            self.prompts.append(prompt)
            (cwd / "partial.txt").write_text("half\n", encoding="utf-8")
            raise Crash("killed")
        return await super().run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event)


def state_file(paths, run_id: str) -> Path:
    return paths.run_dir(run_id) / "state.json"


def test_crash_during_implement_resumes_same_iteration_with_partial_work(env) -> None:
    make, paths, _ = env
    implementer = CrashingImplementer([ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer)
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    cp = Checkpoint.load(state_file(paths, runner.run_id))
    assert cp.phase == "implement" and cp.iteration == 1 and cp.state == "IMPLEMENTING"

    resumed = make(implementer, reviewer, resume=runner.run_id)
    final = asyncio.run(resumed.execute())

    assert final is RunState.DONE
    assert "interrupted" in implementer.prompts[1].lower()
    assert (resumed.worktree / "partial.txt").exists()
    assert Checkpoint.load(state_file(paths, runner.run_id)).phase == "done"


def test_pending_decision_answered_in_store_is_applied_on_resume(env) -> None:
    make, paths, _ = env
    from tests.test_loop import denied
    implementer = FakeImplementer([denied("git reset --hard"), ok()])
    reviewer = FakeReviewer([review("continue", "go"), review("done", None)])
    runner = make(implementer, reviewer, human=None)          # headless: a decision stops the process
    final = asyncio.run(runner.execute())
    assert final is RunState.AWAITING_HUMAN
    cp = Checkpoint.load(state_file(paths, runner.run_id))
    assert cp.phase == "await" and cp.pending_decision["kind"] == "guard_pre"

    runner.store.answer_decision(cp.pending_decision["decision_id"], answer="approve", answered_via="cli")
    resumed = make(implementer, reviewer, human=None, resume=runner.run_id)
    final = asyncio.run(resumed.execute())

    assert final is RunState.DONE
    assert "approved this action" in implementer.prompts[1]


def test_unanswered_decision_stays_awaiting(env) -> None:
    make, paths, _ = env
    from tests.test_loop import denied
    implementer = FakeImplementer([denied("git reset --hard"), ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), human=None)
    asyncio.run(runner.execute())
    resumed = make(implementer, FakeReviewer([]), human=None, resume=runner.run_id)
    assert asyncio.run(resumed.execute()) is RunState.AWAITING_HUMAN
    assert len(implementer.prompts) == 1


def test_resume_refuses_when_head_moved(env) -> None:
    make, paths, _ = env
    implementer = CrashingImplementer([ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]))
    with pytest.raises(Crash):
        asyncio.run(runner.execute())
    git("commit", "-q", "--allow-empty", "-m", "someone else", cwd=runner.worktree)
    with pytest.raises(RuntimeError, match="HEAD"):
        make(implementer, FakeReviewer([]), resume=runner.run_id)


def test_resume_refuses_done_run(env) -> None:
    make, paths, _ = env
    runner = make(FakeImplementer([ok()]), FakeReviewer([review("done", None)]))
    asyncio.run(runner.execute())
    with pytest.raises(RuntimeError, match="DONE"):
        make(FakeImplementer([]), FakeReviewer([]), resume=runner.run_id)


def test_pause_flag_stops_between_phases(env) -> None:
    make, paths, _ = env
    implementer = FakeImplementer([ok(), ok()])
    reviewer = FakeReviewer([review("continue", "go"), review("done", None)])
    runner = make(implementer, reviewer)
    (runner.rundir.path / "pause.requested").write_text("", encoding="utf-8")
    assert asyncio.run(runner.execute()) is RunState.PAUSED
    resumed = make(implementer, reviewer, resume=runner.run_id)
    assert asyncio.run(resumed.execute()) is RunState.DONE
```

Update the `env` fixture in `tests/test_loop.py` so `make` accepts `human=...` (default interactive `lambda d: "0"`, `None` for headless) and `resume: str | None = None`:

```python
    def make(implementer, reviewer, scanner=None, human=lambda d: "0", task_text=TASK, resume=None):
        store = Store(paths.db)
        if resume:
            return Runner.resume(run_id=resume, config=config, paths=paths, store=store, git=GitManager(gh=fake_gh),
                                 implementer=implementer, reviewer=reviewer, scanner=scanner or clean_scanner(), human=human)
        return Runner(config=config, paths=paths, store=store, task=parse_task(task_text), task_text=task_text,
                      git=GitManager(gh=fake_gh), implementer=implementer, reviewer=reviewer,
                      scanner=scanner or clean_scanner(), human=human, clone_url=str(origin))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_resume.py -q`
Expected: FAIL (`Runner.resume` missing; `human=None` unsupported).

- [ ] **Step 3: Rewrite `src/orq/core/loop.py`**

Structure (full file; the bodies below are the complete logic, written to be copied):

```python
"""The run state machine (SPEC sections 6, 7, 10). Every step reads and writes the Checkpoint so a pause
or a crash resumes from state.json through the same code path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from orq.adapters.base import Agent, AgentResult
from orq.adapters.schema import validate_review
from orq.config import Config
from orq.core.checkpoint import Checkpoint
from orq.core.models import Decision, RunRecord, RunState, new_decision_id, new_run_id
from orq.core.procs import kill_tree, pid_alive
from orq.core.progress import ProgressTracker
from orq.core.prompts import (IMPLEMENTER_RULES, INTERRUPTED_NOTE, build_implementer_prompt, build_reviewer_prompt,
                              guard_outcome_lines)
from orq.core.task import Task, parse_task
from orq.git.manager import GitManager
from orq.guard.diff_rules import evaluate_staged
from orq.guard.rules import action_key, describe_action
from orq.guard.settings import write_hook_settings
from orq.paths import OrqPaths
from orq.store.db import Store
from orq.store.rundir import RunDir
from orq.verify.checks import CheckResult, run_check
from orq.verify.secrets import SecretScanner

HumanInput = Callable[[Decision], str]
Printer = Callable[[str], None]
DIFF_INLINE_LIMIT = 20_000
CHECK_TIMEOUT_SECONDS = 1800
PAUSE_FLAG = "pause.requested"


class SandboxError(RuntimeError): ...
class ResumeError(RuntimeError): ...


class _Stop(Exception):
    def __init__(self, state: RunState, reason: str) -> None:
        super().__init__(reason); self.state = state; self.reason = reason


class Runner:
    def __init__(self, *, config, paths, store, task, task_text, git, implementer, reviewer, scanner, human,
                 clone_url=None, printer=print, run_id=None, checkpoint: Checkpoint | None = None) -> None:
        if config.git.sandbox_repos and task.repo not in config.git.sandbox_repos:
            raise SandboxError(f"{task.repo} is not listed in [git].sandbox_repos")
        self.config, self.paths, self.store, self.task = config, paths, store, task
        self.git, self.implementer, self.reviewer, self.scanner = git, implementer, reviewer, scanner
        self.human, self.clone_url, self.print = human, clone_url, printer
        self._resumed_at = time.monotonic()
        if checkpoint is None:
            self.run_id = run_id or new_run_id()
            branch = f"orq/{task.slug}"
            worktree = config.git.worktree_root / task.repo.split("/")[-1] / self.run_id
            self.rundir = RunDir(paths.run_dir(self.run_id)); self.rundir.create(task_text)
            self.cp = Checkpoint(run_id=self.run_id, state=RunState.QUEUED.value, phase="setup", branch=branch,
                                 worktree=str(worktree), started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self.store.create_run(RunRecord(run_id=self.run_id, repo=task.repo, task_title=task.title, branch=branch, worktree=str(worktree)))
            self._save()
        else:
            self.run_id, self.cp = checkpoint.run_id, checkpoint
            self.rundir = RunDir(paths.run_dir(self.run_id))
        write_hook_settings(self.rundir.path, worktree=self.worktree, protected_paths=config.git.protected_paths)
        self.progress = ProgressTracker(self.cp.progress_history)

    @property
    def worktree(self) -> Path: return Path(self.cp.worktree)
    @property
    def branch(self) -> str: return self.cp.branch

    @classmethod
    def resume(cls, *, run_id, config, paths, store, git, implementer, reviewer, scanner, human, printer=print) -> Runner:
        rundir = RunDir(paths.run_dir(run_id))
        cp = Checkpoint.load(rundir.path / "state.json")
        if cp is None: raise ResumeError(f"no state.json for run {run_id}")
        if cp.phase == "done" or cp.state == RunState.DONE.value: raise ResumeError(f"run {run_id} is DONE")
        if pid_alive(cp.pid): raise ResumeError(f"run {run_id} is still active (pid {cp.pid})")
        child = rundir.path / "child.pid"
        if child.exists():
            pid = int(child.read_text(encoding="utf-8").strip() or 0)
            if pid_alive(pid): kill_tree(pid); rundir.event("orphan_killed", pid=pid)
            child.unlink(missing_ok=True)
        task_text = (rundir.path / "TASK.md").read_text(encoding="utf-8")
        task = parse_task(task_text)
        if cp.phase != "setup":
            if not Path(cp.worktree).exists(): raise ResumeError(f"worktree {cp.worktree} is missing")
            head = git.head(Path(cp.worktree))
            if cp.last_commit and head != cp.last_commit:
                raise ResumeError(f"worktree HEAD {head[:8]} differs from the last recorded commit {cp.last_commit[:8]}")
        if cp.phase == "implement": cp.interrupted = True
        if cp.state == RunState.PAUSED.value: (rundir.path / PAUSE_FLAG).unlink(missing_ok=True)
        rundir.event("resume", phase=cp.phase, iteration=cp.iteration, state=cp.state)
        return cls(config=config, paths=paths, store=store, task=task, task_text=task_text, git=git, implementer=implementer,
                   reviewer=reviewer, scanner=scanner, human=human, printer=printer, checkpoint=cp)

    # persistence

    def _save(self) -> None:
        self.cp.progress_history = self.progress.history if hasattr(self, "progress") else self.cp.progress_history
        self.cp.elapsed_seconds += time.monotonic() - self._resumed_at; self._resumed_at = time.monotonic()
        self.cp.save(self.rundir.path / "state.json")

    def _transition(self, state: RunState, **data) -> None:
        self.rundir.event("state", state=state.value, **data)
        self.store.set_state(self.run_id, state)
        self.cp.state = state.value; self._save()
        self.print(f"[{self.run_id}] {state.value}" + (f" {data}" if data else ""))

    def _set_phase(self, phase: str) -> None:
        self.cp.phase = phase; self._save()

    # main loop

    async def execute(self) -> RunState:
        try:
            while True:
                if (self.rundir.path / PAUSE_FLAG).exists() and self.cp.phase not in ("done",):
                    raise _Stop(RunState.PAUSED, "pause requested by the owner")
                self._check_wall_time()
                phase = self.cp.phase
                if phase == "setup": self._setup()
                elif phase == "implement": await self._implement()
                elif phase == "verify": self._verify()
                elif phase == "review": await self._review_phase()
                elif phase == "await": self._await()
                elif phase == "finalize": self._finalize(); self._set_phase("done"); self._transition(RunState.DONE); return RunState.DONE
                elif phase == "done": return RunState.DONE
                else: raise _Stop(RunState.FAILED, f"unknown phase {phase}")
        except _Stop as stop:
            if stop.state is RunState.AWAITING_HUMAN:   # state already recorded by _ask
                return stop.state
            self._transition(stop.state, reason=stop.reason)
            return stop.state
```

Phases:

```python
    def _setup(self) -> None:
        self._transition(RunState.QUEUED)
        repo_path = self.git.ensure_repo(self.task.repo, self.paths.repos, clone_url=self.clone_url)
        if self.git.branch_exists(repo_path, self.cp.branch):
            self.cp.branch = f"{self.cp.branch}-{self.run_id.lower()}"; self.store.set_branch(self.run_id, self.cp.branch)
        self.git.create_worktree(repo_path, self.worktree, branch=self.cp.branch, base=self.task.base_branch)
        self.cp.repo_path = str(repo_path); self.cp.base_commit = self.cp.last_commit = self.git.head(self.worktree)
        self.rundir.event("worktree", path=str(self.worktree), branch=self.cp.branch, base_commit=self.cp.base_commit)
        self._next_iteration()

    def _next_iteration(self) -> None:
        if self.cp.iteration >= self.config.limits.max_iterations:
            raise _Stop(RunState.FAILED, f"max iterations ({self.config.limits.max_iterations}) reached")
        self.cp.iteration += 1; self.store.set_iteration(self.run_id, self.cp.iteration)
        self.cp.denied_actions, self.cp.guard_approved, self.cp.guard_denied = [], [], []
        self.cp.diff_approved = False; self.cp.report = ""; self.cp.diff_hash = None
        self._set_phase("implement")

    async def _implement(self) -> None:
        it = self.cp.iteration; itdir = self.rundir.iteration(it)
        self._transition(RunState.IMPLEMENTING, iteration=it)
        previous_check = _check_from(self.cp.previous_check)
        prompt = build_implementer_prompt(self.task, iteration=it, milestone=self.cp.outcome.get("milestone"),
                                          next_prompt=self.cp.outcome.get("next_prompt"), decisions=self.rundir.decisions_text(),
                                          previous_check=previous_check, discarded=self.cp.discarded,
                                          interrupted=self.cp.interrupted)
        self.cp.interrupted = False; self._save()
        (itdir / "implementer.prompt.md").write_text(prompt, encoding="utf-8")
        result = await self._call("implementer", self.implementer, prompt, itdir / "implementer.stream.jsonl", self.cp.implementer_session)
        if result.session_id:
            self.cp.implementer_session = result.session_id; self.store.set_sessions(self.run_id, implementer_session=result.session_id)
        self.cp.report = result.text[:20_000]
        self.cp.denied_actions = self._denied_actions(result)
        if result.decision:
            d = result.decision
            self._raise_decision("implementer", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="implementer",
                                 decision_type=str(d.get("decision_type", "ambiguity")), question=str(d.get("question", "")),
                                 options=[str(o) for o in d.get("options", [])], recommendation=d.get("recommendation")), payload={})
            return
        self._set_phase("verify")

    def _verify(self) -> None:
        it = self.cp.iteration; itdir = self.rundir.iteration(it)
        self._transition(RunState.VERIFYING, iteration=it)
        self.git.stage_all(self.worktree)
        scan = self.scanner.scan_staged(self.worktree, report_path=itdir / "gitleaks.staged.json")
        if not scan.clean:
            hits = ", ".join(f"{f.get('RuleID')} in {f.get('File')}:{f.get('StartLine')}" for f in scan.findings)
            self._raise_decision("secret", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                                 question=f"gitleaks found secrets: {hits}. Fix them in the worktree, then answer 'rescan', or answer 'abort'.",
                                 options=["rescan", "abort"], recommendation=0), payload={}); return
        if not self.cp.diff_approved:
            violations = evaluate_staged(self.git, self.worktree, protected_paths=self.config.git.protected_paths, guard=self.config.guard)
            if violations:
                listing = "\n".join(f"- {v}" for v in violations)
                self.rundir.event("diff_rules", iteration=it, violations=[str(v) for v in violations])
                self._raise_decision("guard_diff", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                                     destructive=True, options=["approve", "deny"], recommendation=1,
                                     question=f"The diff guard flagged these changes:\n{listing}\nAllow them?"), payload={"listing": listing}); return
        if not self.git.staged_files(self.worktree) and self.cp.denied_actions:
            self.rundir.event("review_skipped", iteration=it, reason="no changes, guard decisions pending")
            self._start_guard_decisions(); return
        check = run_check(self.task.check_command, cwd=self.worktree, timeout=CHECK_TIMEOUT_SECONDS)
        (itdir / "checks.txt").write_text(check.output, encoding="utf-8")
        self.rundir.event("check", iteration=it, ok=check.ok, exit_code=check.exit_code, signature=check.signature)
        self.cp.previous_check = _check_to(check)
        since = self.cp.last_commit
        summary = (self.cp.report.strip().splitlines() or ["no report"])[0][:60]
        commit = self.git.commit_staged(self.worktree, f"orq({self.run_id}) iter {it}: {summary}")
        if commit: self.cp.last_commit = commit
        self.cp.commits[str(it)] = self.cp.last_commit
        self.rundir.event("commit", iteration=it, sha=commit)
        patch = self.git.diff(self.worktree, since=since)
        (itdir / "diff.patch").write_text(patch, encoding="utf-8")
        self.cp.diff_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
        self._set_phase("review")

    async def _review_phase(self) -> None:
        it = self.cp.iteration; itdir = self.rundir.iteration(it)
        self._transition(RunState.REVIEWING, iteration=it)
        since = self.cp.commits.get(str(it - 1)) or self.cp.base_commit
        patch = (itdir / "diff.patch").read_text(encoding="utf-8")
        check = _check_from(self.cp.previous_check)
        prompt = build_reviewer_prompt(self.task, iteration=it, milestone=self.cp.outcome.get("milestone"), diff_stat=self.git.diff_stat(self.worktree, since=since),
                                       diff_path=str(itdir / "diff.patch"), diff_excerpt=patch if len(patch) <= DIFF_INLINE_LIMIT else None,
                                       check=check, implementer_report=self.cp.report, decisions=self.rundir.decisions_text(), discarded=self.cp.discarded)
        self.cp.discarded = None
        (itdir / "reviewer.prompt.md").write_text(prompt, encoding="utf-8")
        result = await self._call("reviewer", self.reviewer, prompt, itdir / "reviewer.stream.jsonl", None)
        if result.error_kind == "invalid_output":
            self.rundir.event("reviewer_invalid_output", iteration=it, error=result.error)
            result = await self._call("reviewer", self.reviewer, prompt, itdir / "reviewer.stream.jsonl", None)
        problems = validate_review(result.structured)
        if problems: raise _Stop(RunState.FAILED, "reviewer output invalid: " + "; ".join(problems))
        review = result.structured
        (itdir / "reviewer.output.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
        self.cp.summaries[str(it)] = review.get("summary", "")
        status = review["status"]
        if status == "needs_human":
            h = review.get("human") or {}
            self.cp.outcome = {"next_prompt": "Continue with the owner's answer above.", "milestone": review.get("milestone"), "done": False}
            self._raise_decision("reviewer", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="reviewer",
                                 decision_type=str(h.get("decision_type", "ambiguity")), question=str(h.get("question", "")),
                                 options=[str(o) for o in h.get("options", [])], recommendation=h.get("recommendation")), payload={}); return
        serious = [i for i in review.get("issues", []) if i.get("severity") in ("blocker", "major")]
        if status == "done" and serious:
            self.rundir.event("done_overridden", iteration=it, issues=serious)
            next_prompt, done = "The reviewer reported these issues; fix them:\n" + "\n".join(f"- [{i['severity']}] {i['description']}" for i in serious), False
        else:
            next_prompt, done = review.get("next_prompt"), status == "done"
        self.cp.outcome = {"next_prompt": next_prompt, "milestone": review.get("milestone"), "done": done}
        rule = self.progress.record(it, diff_hash=self.cp.diff_hash or "", failure_signature=None if check.ok else check.signature, next_prompt=next_prompt)
        if rule is not None:
            self.rundir.event("no_progress", iteration=it, rule=rule.rule, rollback_to=rule.rollback_to)
            target = f"rollback to iteration {rule.rollback_to}"
            self._raise_decision("progress", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                                 question=f"No progress ({rule.rule}): {rule.detail}. What now?", options=["continue", target, "abort"], recommendation=1),
                                 payload={"rollback_to": rule.rollback_to, "target": target}); return
        self._after_review()

    def _after_review(self) -> None:
        if self.cp.denied_actions:
            self._start_guard_decisions(); return
        if self.cp.outcome.get("done"):
            self._set_phase("finalize")
        else:
            self._next_iteration()
```

Decisions and answers:

```python
    def _raise_decision(self, kind: str, decision: Decision, payload: dict) -> None:
        self.store.add_decision(decision)
        self.rundir.event("decision", decision_id=decision.decision_id, source=decision.source, decision_type=decision.decision_type,
                          question=decision.question, options=decision.options, kind=kind)
        self.cp.pending_decision = {"decision_id": decision.decision_id, "kind": kind, "payload": payload,
                                    "question": decision.question, "options": decision.options}
        self.cp.phase = "await"
        self._transition(RunState.AWAITING_HUMAN, decision_id=decision.decision_id)

    def _await(self) -> None:
        pending = self.cp.pending_decision
        decision = self.store.get_decision(pending["decision_id"])
        if decision.status != "answered":
            if self.human is None:
                self.print(f"[{self.run_id}] waiting for `orq answer {decision.decision_id} ...`")
                raise _Stop(RunState.AWAITING_HUMAN, "decision pending")
            raw = self.human(decision).strip()
            answer = decision.options[int(raw)] if raw.isdigit() and decision.options and 0 <= int(raw) < len(decision.options) else raw
            self.store.answer_decision(decision.decision_id, answer=answer, answered_via="cli")
            self.rundir.append_decision(decision.decision_id, decision.question, answer)
            decision = self.store.get_decision(decision.decision_id)
        self.rundir.event("answer", decision_id=decision.decision_id, answer=decision.answer)
        self.cp.pending_decision = None
        self._apply_answer(pending["kind"], pending["payload"], decision.answer or "")

    def _apply_answer(self, kind: str, payload: dict, answer: str) -> None:
        a = answer.strip().lower()
        if kind in ("implementer", "reviewer"):
            self.cp.outcome["next_prompt"] = "Continue with the owner's answer above."; self._after_review_or_next()
        elif kind == "guard_pre":
            action = payload["action"]
            if a == "approve":
                (self.paths.allow_tokens(self.run_id) / action["key"]).write_text("approved", encoding="utf-8")
                self.rundir.event("guard_token", action_key=action["key"], description=action["description"])
                self.cp.guard_approved.append(action["description"])
            else:
                self.cp.guard_denied.append(action["description"])
            self.cp.denied_actions = payload["remaining"]
            if self.cp.denied_actions:
                self._start_guard_decisions(); return
            block = guard_outcome_lines(self.cp.guard_approved, self.cp.guard_denied)
            base = self.cp.outcome.get("next_prompt")
            self.cp.outcome = {"next_prompt": f"{base}\n\n{block}" if base else block, "milestone": self.cp.outcome.get("milestone"), "done": False}
            self._next_iteration()
        elif kind == "guard_diff":
            if a == "approve":
                self.cp.diff_approved = True; self._set_phase("verify")
            else:
                self.git.reset_hard(self.worktree, self.cp.last_commit); self.rundir.event("rollback_iteration", iteration=self.cp.iteration, to=self.cp.last_commit)
                self.cp.outcome = {"next_prompt": "The owner rejected these changes:\n" + payload["listing"] + "\nThe worktree was reset to the previous iteration. Proceed without them.",
                                   "milestone": self.cp.outcome.get("milestone"), "done": False}
                if self.cp.denied_actions: self._start_guard_decisions()
                else: self._next_iteration()
        elif kind == "secret":
            if a in ("abort", "1"): raise _Stop(RunState.ABORTED, "secret found, owner aborted")
            self._set_phase("verify")
        elif kind == "progress":
            if a == "abort": raise _Stop(RunState.ABORTED, "owner aborted after no progress")
            self.progress.reset()
            if a == payload["target"].lower():
                self.cp.outcome["next_prompt"] = self.rollback_to(payload["rollback_to"], upto=self.cp.iteration)
                self.cp.outcome["done"] = False
            self._after_review()
        elif kind == "error":
            if a == "abort": raise _Stop(RunState.ABORTED, "owner aborted after an agent error")
            self._set_phase(payload["phase"])
        else:
            raise _Stop(RunState.FAILED, f"unknown decision kind {kind}")

    def _after_review_or_next(self) -> None:
        """After an implementer or reviewer question: the iteration is over, start the next one."""
        self._next_iteration()

    def _start_guard_decisions(self) -> None:
        action, *remaining = self.cp.denied_actions
        self._raise_decision("guard_pre", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="guard", decision_type="risk",
                             destructive=True, options=["approve", "deny"], recommendation=1,
                             question=f"The implementer tried a destructive action: {action['description']}. Allow it once?"),
                             payload={"action": action, "remaining": remaining})
```

Note for `implementer` kind: the implementer asked a question and did no further work, so there is nothing to verify; the next iteration starts with the answer. For `reviewer` kind: the review already happened; same.

Agents, errors, rollback, finalize:

```python
    async def _call(self, role: str, agent: Agent, prompt: str, log_path: Path, session_id: str | None) -> AgentResult:
        result = await agent.run(prompt, cwd=self.worktree, log_path=log_path, session_id=session_id, run_dir=self.rundir.path)
        self.rundir.event(role, ok=result.ok, error_kind=result.error_kind, session_id=result.session_id, usage=result.usage, rate_limit=result.rate_limit)
        if result.ok or result.error_kind == "invalid_output":
            return result
        if result.error_kind == "rate_limit":
            raise _RateLimited(role, result)          # handled in Milestone 5; for now map to _Stop(PAUSED_RATE_LIMIT)
        self._raise_decision("error", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                             question=f"{role} failed ({result.error_kind}): {(result.error or '')[:500]}. Retry or abort?", options=["retry", "abort"], recommendation=0),
                             payload={"phase": self.cp.phase})
        raise _Stop(RunState.AWAITING_HUMAN, "agent error")

    def rollback_to(self, target: int, *, upto: int) -> str:
        sha = self.cp.commits.get(str(target)) or self.cp.base_commit
        self.git.reset_hard(self.worktree, sha); self.cp.last_commit = sha
        lines = [f"- iteration {i}: {self.cp.summaries.get(str(i), '(no review)')}" for i in range(target + 1, upto + 1)]
        for i in range(target + 1, upto + 1): self.cp.commits.pop(str(i), None); self.cp.summaries.pop(str(i), None)
        self.cp.discarded = f"Iterations {target + 1}..{upto} were discarded by the owner:\n" + "\n".join(lines)
        self.rundir.event("rollback", to_iteration=target, sha=sha, discarded=list(range(target + 1, upto + 1)))
        self._save()
        return f"The owner rolled the worktree back to iteration {target}. Start again from there.\n" + self.cp.discarded

    def _finalize(self) -> None:
        self._transition(RunState.FINALIZING)
        scan = self.scanner.scan_range(self.worktree, f"origin/{self.task.base_branch}", report_path=self.rundir.path / "gitleaks.push.json")
        if not scan.clean: raise _Stop(RunState.FAILED, f"gitleaks found secrets in the branch history: {scan.findings}")
        self.git.push(self.worktree, self.cp.branch)
        url = self.git.pr_url(self.worktree, head=self.cp.branch)
        if url is None:
            body = f"Automated by orq run {self.run_id}.\n\n{self.task.goal}\n\nCheck command: `{self.task.check_command}`"
            url = self.git.create_pr(self.worktree, base=self.task.base_branch, head=self.cp.branch, title=self.task.title, body=body)
        self.rundir.event("pr", url=url); self.print(f"[{self.run_id}] PR opened: {url}")

    def _check_wall_time(self) -> None:
        elapsed = self.cp.elapsed_seconds + (time.monotonic() - self._resumed_at)
        if elapsed / 3600 > self.config.limits.max_wall_hours:
            raise _Stop(RunState.FAILED, f"max wall time ({self.config.limits.max_wall_hours} h) exceeded")

    def _denied_actions(self, result: AgentResult) -> list[dict]:   # same as Task 4


def _check_to(check: CheckResult) -> dict:
    return {"ok": check.ok, "exit_code": check.exit_code, "output": check.output[-8000:], "timed_out": check.timed_out}

def _check_from(data: dict | None) -> CheckResult | None:
    return CheckResult(ok=data["ok"], exit_code=data["exit_code"], output=data["output"], timed_out=data["timed_out"]) if data else None

def implementer_system_prompt() -> str: return IMPLEMENTER_RULES
```

For this task, `_RateLimited` is not yet defined: in `_call`, on `rate_limit` raise `_Stop(RunState.PAUSED_RATE_LIMIT, f"{role}: {result.error}")` exactly as Phase 1 did. Milestone 5 replaces it.

Supporting changes:

* `src/orq/core/prompts.py`: `INTERRUPTED_NOTE = "## Interrupted turn\nYour previous turn was interrupted before it finished. The worktree holds your uncommitted work; continue from there."`; `build_implementer_prompt(..., discarded: str | None = None, interrupted: bool = False)` appends the note when `interrupted`.
* `src/orq/git/manager.py`: `pr_url(worktree, head) -> str | None` runs `gh pr view <head> --json url -q .url` via `self._gh` and returns `None` when `gh` fails (catch `GitError`). The test fake `fake_gh` must answer `pr view` with a non-zero exit: update `tests/fakes/fake_gh.py` to `sys.exit(1)` for `pr view` unless `FAKE_GH_PR_EXISTS` is set, in which case it prints the URL.
* `tests/test_loop.py`: `fake_gh` in the `env` fixture raises `GitError` for `pr view` (import `GitError`), so a PR is created.
* `src/orq/store/rundir.py`: remove `write_state`/`read_state` (the checkpoint owns `state.json`) and update `tests/test_store.py` accordingly.
* `_ask` from Phase 1 is gone; the interactive path is `_await` with `self.human`. `Runner(..., human=None)` means headless.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, including `tests/test_loop.py` (interactive path through `_await`) and `tests/test_resume.py`. Fix test expectations that depended on the removed `_ask` internals, nothing else.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor(core): runner as a checkpointed phase loop with crash resume"
```

### Task 11: CLI commands `answer`, `resume`, `rollback`, `pause`, `abort`, plus the kill test

**Files:**
- Create: `src/orq/__main__.py`
- Modify: `src/orq/cli.py`, `tests/fakes/fake_claude.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_answer_records_decision(home: OrqPaths) -> None:
    store = Store(home.db)
    store.create_run(RunRecord(run_id="RAAAAA", repo="o/s", task_title="t", branch="b"))
    RunDir(home.run_dir("RAAAAA")).create("# Task: t\n")
    store.add_decision(Decision(decision_id="DBBBB", run_id="RAAAAA", source="guard", decision_type="risk", question="Allow?", options=["approve", "deny"]))

    result = CliRunner().invoke(app, ["answer", "DBBBB", "--approve"])

    assert result.exit_code == 0, result.output
    assert Store(home.db).get_decision("DBBBB").answer == "approve"
    assert "DBBBB" in (home.run_dir("RAAAAA") / "DECISIONS.md").read_text(encoding="utf-8")
    again = CliRunner().invoke(app, ["answer", "DBBBB", "--deny"])
    assert again.exit_code == 1 and "already answered" in again.output


def test_answer_requires_exactly_one_form(home: OrqPaths) -> None:
    assert CliRunner().invoke(app, ["answer", "DBBBB"]).exit_code == 1
    assert CliRunner().invoke(app, ["answer", "DBBBB", "text", "--approve"]).exit_code == 1


def test_pause_and_abort_need_an_inactive_run(home: OrqPaths) -> None:
    store = Store(home.db)
    store.create_run(RunRecord(run_id="RAAAAA", repo="o/s", task_title="t", branch="b"))
    rundir = RunDir(home.run_dir("RAAAAA")); rundir.create("# Task: t\n")
    from orq.core.checkpoint import Checkpoint
    cp = Checkpoint(run_id="RAAAAA", state="AWAITING_HUMAN", phase="await", branch="b", worktree=str(home.root / "wt"))
    cp.pid = 999999; cp.save(rundir.path / "state.json")

    paused = CliRunner().invoke(app, ["pause", "RAAAAA"])
    assert paused.exit_code == 0 and (rundir.path / "pause.requested").exists()

    aborted = CliRunner().invoke(app, ["abort", "RAAAAA"])
    assert aborted.exit_code == 0, aborted.output
    assert Store(home.db).get_run("RAAAAA").state is RunState.ABORTED


def test_run_kill_and_resume_with_fake_clis(home: OrqPaths, origin: Path, tmp_path: Path) -> None:
    """The exit criterion for crash resume: kill orq mid-implementer, resume, reach DONE."""
    import os, subprocess, time
    from orq.core.checkpoint import Checkpoint
    from orq.core.procs import kill_tree
    home.config.write_text(f'[git]\nworktree_root = "{(tmp_path / "wt").as_posix()}"\n', encoding="utf-8")
    task = tmp_path / "TASK.md"; task.write_text(TASK, encoding="utf-8")
    py = sys.executable
    env = {**os.environ, "ORQ_HOME": str(home.root), "ORQ_CLAUDE_EXE": f"{py} {FAKES / 'fake_claude.py'}",
           "ORQ_CODEX_CMD": f"{py} {FAKES / 'fake_codex.py'}", "ORQ_GH_CMD": f"{py} {FAKES / 'fake_gh.py'}",
           "FAKE_RECORD": str(tmp_path / "rec.json"), "FAKE_GH_RECORD": str(tmp_path / "gh.jsonl"),
           "FAKE_SCENARIO": "hang", "FAKE_CODEX_SCENARIO": "done", "PYTHONUTF8": "1"}
    proc = subprocess.Popen([py, "-m", "orq", "run", str(task), "--clone-url", str(origin)], env=env, cwd=str(tmp_path),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        run_id = None
        while time.time() < deadline and run_id is None:
            runs = Store(home.db).list_runs()
            if runs and runs[0].state is RunState.IMPLEMENTING and (home.run_dir(runs[0].run_id) / "child.pid").exists():
                run_id = runs[0].run_id
            time.sleep(0.5)
        assert run_id, "run never reached IMPLEMENTING"
        kill_tree(proc.pid); proc.wait(timeout=20)
    finally:
        if proc.poll() is None: proc.kill()
    cp = Checkpoint.load(home.run_dir(run_id) / "state.json")
    assert cp.phase == "implement" and cp.state == "IMPLEMENTING"

    env["FAKE_SCENARIO"] = "ok"
    resumed = subprocess.run([py, "-m", "orq", "resume", run_id], env=env, cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8", timeout=120)

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert Store(home.db).get_run(run_id).state is RunState.DONE
    assert "interrupted" in (home.iteration_dir(run_id, 1) / "implementer.prompt.md").read_text(encoding="utf-8").lower()
```

`tests/fakes/fake_claude.py`: add scenario `hang`: write `init(session_id)` and flush, then `time.sleep(120)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL (`No such command 'answer'`, `No module named orq.__main__`).

- [ ] **Step 3: Implement**

`src/orq/__main__.py`:

```python
from orq.cli import app

app()
```

`src/orq/cli.py` additions:

```python
def _build_runner_parts(config: Config):
    return dict(implementer=ClaudeImplementer(model=config.implementer.default_model, system_prompt=implementer_system_prompt()),
                reviewer=_build_reviewer(config), scanner=SecretScanner(), git=GitManager())


@app.command()
def answer(decision_id: str, text: str | None = typer.Argument(None), approve: bool = typer.Option(False, "--approve"),
           deny: bool = typer.Option(False, "--deny")) -> None:
    """Answer a pending decision. The run continues with `orq resume` (or immediately when it is still running interactively)."""
    forms = [bool(text), approve, deny]
    if sum(forms) != 1:
        typer.secho("give exactly one of: a text answer, --approve, --deny", fg=typer.colors.RED); raise typer.Exit(1)
    paths = OrqPaths.from_env(); store = Store(paths.db)
    decision = store.get_decision(decision_id)
    if decision is None:
        typer.secho(f"no decision {decision_id}", fg=typer.colors.RED); raise typer.Exit(1)
    if decision.status != "pending":
        typer.secho(f"{decision_id} already answered: {decision.answer}", fg=typer.colors.RED); raise typer.Exit(1)
    value = "approve" if approve else "deny" if deny else text
    store.answer_decision(decision_id, answer=value, answered_via="cli")
    rundir = RunDir(paths.run_dir(decision.run_id)); rundir.append_decision(decision_id, decision.question, value)
    rundir.event("answer", decision_id=decision_id, answer=value, via="cli")
    typer.echo(f"{decision_id} answered: {value}. Run `orq resume {decision.run_id}` to continue.")


@app.command()
def resume(run_id: str) -> None:
    """Continue a paused, crashed or answered run from its last checkpoint."""
    paths = OrqPaths.from_env(); config = load_config(paths.config)
    try:
        runner = Runner.resume(run_id=run_id, config=config, paths=paths, store=Store(paths.db), human=_ask_in_terminal,
                               printer=typer.echo, **_build_runner_parts(config))
    except (ResumeError, SandboxError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED); raise typer.Exit(1)
    final = asyncio.run(runner.execute())
    if final is not RunState.DONE: raise typer.Exit(1)


@app.command()
def rollback(run_id: str, to: int = typer.Option(..., "--to", help="Iteration to roll back to (0 = base commit)")) -> None:
    """Reset a stopped run's worktree to an earlier iteration; `orq resume` then continues from there."""
    paths = OrqPaths.from_env(); config = load_config(paths.config)
    try:
        runner = Runner.resume(run_id=run_id, config=config, paths=paths, store=Store(paths.db), human=None, printer=typer.echo, **_build_runner_parts(config))
    except ResumeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED); raise typer.Exit(1)
    try:
        runner.rollback_cli(to)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED); raise typer.Exit(1)
    typer.echo(f"{run_id} rolled back to iteration {to}; run `orq resume {run_id}` to continue")


def _inactive_checkpoint(run_id: str):
    paths = OrqPaths.from_env()
    cp = Checkpoint.load(paths.run_dir(run_id) / "state.json")
    if cp is None:
        typer.secho(f"no run {run_id}", fg=typer.colors.RED); raise typer.Exit(1)
    return paths, cp


@app.command()
def pause(run_id: str) -> None:
    """Ask a running run to stop between steps; `orq resume` continues it."""
    paths, cp = _inactive_checkpoint(run_id)
    (paths.run_dir(run_id) / PAUSE_FLAG).write_text("", encoding="utf-8")
    typer.echo(f"pause requested for {run_id}" + ("" if pid_alive(cp.pid) else " (run is not active; it will stay paused)"))


@app.command()
def abort(run_id: str) -> None:
    """Mark a stopped run ABORTED and remove its worktree."""
    paths, cp = _inactive_checkpoint(run_id)
    if pid_alive(cp.pid):
        typer.secho(f"{run_id} is still running (pid {cp.pid}); pause it first", fg=typer.colors.RED); raise typer.Exit(1)
    store = Store(paths.db); rundir = RunDir(paths.run_dir(run_id))
    if cp.repo_path and Path(cp.worktree).exists():
        try:
            GitManager().remove_worktree(Path(cp.repo_path), Path(cp.worktree), branch=cp.branch)
        except GitError as exc:
            typer.secho(f"worktree not removed: {exc}", fg=typer.colors.YELLOW)
    cp.state, cp.phase = RunState.ABORTED.value, "done"; cp.save(rundir.path / "state.json")
    store.set_state(run_id, RunState.ABORTED); rundir.event("state", state="ABORTED", reason="aborted by owner")
    typer.echo(f"{run_id} aborted")
```

`Runner.rollback_cli(to)` in `loop.py`: validates `0 <= to < self.cp.iteration` (else `ValueError`), calls `rollback_to(to, upto=self.cp.iteration)`, sets `self.cp.iteration = to`, `self.cp.outcome = {"next_prompt": <returned text>, "milestone": None, "done": False}`, `self.cp.pending_decision = None`, `self.cp.denied_actions = []`, phase `"implement"` via `_next_iteration()`, and saves. Because `resume()` refuses an active pid, the CLI cannot roll back a live run.

`run` command: use `_build_runner_parts`; the `sandbox_repos` check message changes with the new semantics (`test_run_refuses_repo_outside_sandbox_list` keeps passing when the config in that test lists a different repo; update the test to write `sandbox_repos = ["other/repo"]`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all pass. The kill test takes about 10 to 20 s.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat(cli): answer, resume, rollback, pause and abort commands; kill-and-resume test"
```

---

## Milestone 5: rate limits and reviewer fallback

### Task 12: Claude reset parsing

**Files:**
- Create: `src/orq/core/ratelimit.py`
- Test: `tests/test_ratelimit.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ratelimit.py
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from orq.adapters.base import AgentResult
from orq.core.ratelimit import claude_reset_time, parse_claude_reset

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)  # 12:00 in America/Cayenne (UTC-3)


def test_parse_pm_time_today() -> None:
    reset = parse_claude_reset("You've hit your session limit · resets 10pm (America/Cayenne)", now=NOW)
    assert reset == datetime(2026, 10, 6, 22, 0, tzinfo=ZoneInfo("America/Cayenne"))


def test_parse_minutes_and_wrap_to_tomorrow() -> None:
    reset = parse_claude_reset("resets 5:10am (America/Cayenne)", now=NOW)
    assert reset.astimezone(timezone.utc) == datetime(2026, 10, 7, 8, 10, tzinfo=timezone.utc)


def test_parse_unknown_returns_none() -> None:
    assert parse_claude_reset("API Error: 529 Overloaded", now=NOW) is None
    assert parse_claude_reset("resets 10pm (Mars/Olympus)", now=NOW) is None


def test_reset_time_prefers_stream_event() -> None:
    result = AgentResult(ok=False, error="resets 10pm (America/Cayenne)", error_kind="rate_limit",
                         rate_limit={"status": "rejected", "resetsAt": int(NOW.timestamp()) + 600})
    assert claude_reset_time(result, now=NOW) == NOW + timedelta(seconds=600)


def test_reset_time_falls_back_to_text_then_default() -> None:
    result = AgentResult(ok=False, error="You've hit your session limit · resets 10pm (America/Cayenne)", error_kind="rate_limit")
    assert claude_reset_time(result, now=NOW).astimezone(timezone.utc).hour == 1  # 22:00 Cayenne = 01:00 UTC next day
    assert claude_reset_time(AgentResult(ok=False, error="rate limit", error_kind="rate_limit"), now=NOW) == NOW + timedelta(minutes=15)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ratelimit.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/orq/core/ratelimit.py
"""Rate-limit reset times for Claude (SPEC 12, Phase 0 section 5)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orq.adapters.base import AgentResult

_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.IGNORECASE)
DEFAULT_BACKOFF = timedelta(minutes=15)


def parse_claude_reset(text: str, *, now: datetime) -> datetime | None:
    match = _RESET_RE.search(text or "")
    if not match:
        return None
    hour, minute, meridiem, zone = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower(), match.group(4).strip()
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def claude_reset_time(result: AgentResult, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    info = result.rate_limit or {}
    if isinstance(info.get("resetsAt"), (int, float)):
        return datetime.fromtimestamp(info["resetsAt"], tz=timezone.utc)
    parsed = parse_claude_reset(result.error or result.text or "", now=now)
    return parsed if parsed else now + DEFAULT_BACKOFF
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ratelimit.py -q`
Expected: all pass. (`tzdata` ships with Python on Windows via `zoneinfo`; if `ZoneInfo("America/Cayenne")` fails, add `tzdata` to dependencies.)

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/ratelimit.py tests/test_ratelimit.py
git commit -m "feat(core): parse Claude rate-limit reset times"
```

### Task 13: Codex error classification and rate-limit snapshot

**Files:**
- Modify: `src/orq/adapters/codex.py`, `tests/fakes/fake_codex.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_reads_rate_limit_snapshot_from_session_file(tmp_path: Path, monkeypatch) -> None:
    from orq.adapters.codex import read_rate_limits
    sessions = tmp_path / "sessions" / "2026" / "10" / "06"
    sessions.mkdir(parents=True)
    lines = [{"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 16.0, "resets_at": 1791238480}}}},
             {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 91.0, "resets_at": 1791238480}, "rate_limit_reached_type": None}}}]
    (sessions / "rollout-2026-10-06T10-00-00-thread-1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    snapshot = read_rate_limits("thread-1", sessions_root=tmp_path / "sessions")
    assert snapshot["primary"]["used_percent"] == 91.0
    assert read_rate_limits("missing", sessions_root=tmp_path / "sessions") is None


def test_codex_rate_limit_failure_is_classified(tmp_path: Path, fake_codex_prefix, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "rate_limit")
    reviewer = CodexReviewer(model="gpt-5.5", argv_prefix=fake_codex_prefix)
    result = asyncio.run(reviewer.run("review", cwd=tmp_path, log_path=tmp_path / "log.jsonl"))
    assert not result.ok and result.error_kind == "rate_limit"
```

`tests/fakes/fake_codex.py`: scenario `rate_limit` emits `{"type": "error", "message": "You've reached your usage limit. Try again at 10pm."}` then `{"type": "turn.failed", "error": {"message": "You've reached your usage limit..."}}` and exits 1.

Check how `tests/test_adapters.py` already builds the Codex fake prefix and reuse it. Also verify the real session-file line shape against a file under `~/.codex/sessions` before finalising `read_rate_limits` (Phase 0 recorded `token_count` events carrying `rate_limits`; confirm whether the key sits under `payload` or at top level and make the reader accept both).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_adapters.py -q -k codex`
Expected: FAIL (`read_rate_limits` missing; `error_kind == "error"`).

- [ ] **Step 3: Implement**

In `src/orq/adapters/codex.py`:

```python
import glob, re
_LIMIT_RE = re.compile(r"usage limit|rate limit|quota|too many requests", re.IGNORECASE)
_AUTH_RE = re.compile(r"unauthori[sz]ed|not logged in|login|401", re.IGNORECASE)


def sessions_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def read_rate_limits(thread_id: str | None, sessions_root: Path | None = None) -> dict | None:
    """Latest rate_limits snapshot from the Codex session file for thread_id, or None."""
    if not thread_id:
        return None
    root = sessions_root or globals()["sessions_root"]()
    matches = sorted(root.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    if not matches:
        return None
    latest = None
    with matches[-1].open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            if isinstance(payload.get("rate_limits"), dict):
                latest = payload["rate_limits"]
    return latest


def classify_codex_error(message: str, snapshot: dict | None) -> str:
    if snapshot and snapshot.get("rate_limit_reached_type"):
        return "rate_limit"
    if _LIMIT_RE.search(message or ""):
        return "rate_limit"
    if _AUTH_RE.search(message or ""):
        return "auth"
    return "error"
```

`CodexReviewer`: add `self.last_rate_limits: dict | None = None`; after `stream_process`, `self.last_rate_limits = read_rate_limits(thread_id)`; in the failure branch use `error_kind=classify_codex_error(message, self.last_rate_limits)`; attach `rate_limit=self.last_rate_limits` to every `AgentResult`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_adapters.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/adapters/codex.py tests/fakes/fake_codex.py tests/test_adapters.py
git commit -m "feat(adapters): Codex error classification and rate-limit snapshot from the session file"
```

### Task 14: reviewer router

**Files:**
- Create: `src/orq/adapters/router.py`
- Modify: `src/orq/cli.py` (`_build_reviewer`)
- Test: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_router.py
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from orq.adapters.base import AgentResult
from orq.adapters.router import ReviewerRouter

GOOD = {"status": "continue", "summary": "s", "milestone": "m", "next_prompt": "n", "issues": [], "human": None}


@dataclass
class Scripted:
    name: str
    results: list[AgentResult]
    calls: int = 0
    last_rate_limits: dict | None = None

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None):
        self.calls += 1
        return self.results.pop(0)


def run(router: ReviewerRouter, tmp_path: Path) -> AgentResult:
    return asyncio.run(router.run("p", cwd=tmp_path, log_path=tmp_path / "l.jsonl"))


def test_primary_used_by_default(tmp_path: Path) -> None:
    primary, fallback = Scripted("codex", [AgentResult(ok=True, structured=GOOD)]), Scripted("claude", [])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    assert run(router, tmp_path).ok and primary.calls == 1 and fallback.calls == 0 and router.fallback_until is None


def test_reactive_switch_retries_on_fallback(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="usage limit", error_kind="rate_limit", rate_limit={"primary": {"resets_at": 5000}})])
    fallback = Scripted("claude", [AgentResult(ok=True, structured=GOOD)])
    events = []
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0, on_switch=events.append)
    result = run(router, tmp_path)
    assert result.ok and fallback.calls == 1 and router.fallback_until == 5000 and events == [("switched", "claude", 5000)]


def test_proactive_switch_after_threshold(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=True, structured=GOOD, rate_limit={"primary": {"used_percent": 92.0, "resets_at": 5000}})])
    fallback = Scripted("claude", [AgentResult(ok=True, structured=GOOD)])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    assert run(router, tmp_path).ok and primary.calls == 1
    assert run(router, tmp_path).ok and fallback.calls == 1 and router.active.name == "claude"


def test_restore_after_reset(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=True, structured=GOOD)])
    fallback = Scripted("claude", [])
    now = [6000.0]
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: now[0], fallback_until=5000)
    assert run(router, tmp_path).ok and primary.calls == 1 and router.fallback_until is None


def test_fallback_rate_limit_is_returned_as_is(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="usage limit", error_kind="rate_limit")])
    fallback = Scripted("claude", [AgentResult(ok=False, error="hit your limit", error_kind="rate_limit")])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    result = run(router, tmp_path)
    assert not result.ok and result.error_kind == "rate_limit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_router.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/orq/adapters/router.py
"""Swaps the reviewer between Codex and the Claude fallback around usage limits (SPEC 4 and 12)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from orq.adapters.base import Agent, AgentResult, EventCallback

DEFAULT_FALLBACK_SECONDS = 3600.0
SwitchCallback = Callable[[tuple[str, str, float | None]], None]


class ReviewerRouter:
    def __init__(self, primary: Agent, fallback: Agent, *, switch_at_used_percent: int, clock: Callable[[], float] = time.time,
                 fallback_until: float | None = None, on_switch: SwitchCallback | None = None) -> None:
        self.primary, self.fallback = primary, fallback
        self.threshold, self.clock, self.fallback_until, self.on_switch = switch_at_used_percent, clock, fallback_until, on_switch

    @property
    def active(self) -> Agent:
        if self.fallback_until is not None and self.clock() < self.fallback_until:
            return self.fallback
        if self.fallback_until is not None:
            self.fallback_until = None
            self._notify("restored", self.primary.name, None)
        return self.primary

    @property
    def name(self) -> str:
        return self.active.name

    async def run(self, prompt: str, *, cwd: Path, log_path: Path, session_id: str | None = None,
                  run_dir: Path | None = None, on_event: EventCallback | None = None) -> AgentResult:
        agent = self.active
        result = await agent.run(prompt, cwd=cwd, log_path=log_path, session_id=session_id, run_dir=run_dir, on_event=on_event)
        if agent is not self.primary:
            return result
        snapshot = (result.rate_limit or {}).get("primary") or {}
        if result.error_kind == "rate_limit":
            self._switch(snapshot.get("resets_at"))
            return await self.fallback.run(prompt, cwd=cwd, log_path=log_path, session_id=None, run_dir=run_dir, on_event=on_event)
        used = snapshot.get("used_percent")
        if isinstance(used, (int, float)) and used >= self.threshold:
            self._switch(snapshot.get("resets_at"))
        return result

    def _switch(self, resets_at: float | None) -> None:
        self.fallback_until = float(resets_at) if resets_at else self.clock() + DEFAULT_FALLBACK_SECONDS
        self._notify("switched", self.fallback.name, self.fallback_until)

    def _notify(self, kind: str, name: str, until: float | None) -> None:
        if self.on_switch:
            self.on_switch((kind, name, until))
```

`src/orq/cli.py` `_build_reviewer`: when `config.reviewer.primary == "codex"` and `config.reviewer.fallback == "claude"`, return `ReviewerRouter(CodexReviewer(...), ClaudeReviewer(model=config.implementer.default_model), switch_at_used_percent=config.reviewer.switch_at_used_percent)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_router.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/adapters/router.py src/orq/cli.py tests/test_router.py
git commit -m "feat(adapters): reviewer router with proactive and reactive Codex to Claude fallback"
```

### Task 15: runner waits out Claude limits and persists the router state

**Files:**
- Modify: `src/orq/core/loop.py`
- Test: `tests/test_loop.py`

- [ ] **Step 1: Write the failing tests**

```python
def rate_limited() -> AgentResult:
    return AgentResult(ok=False, error="You've hit your session limit · resets 10pm (America/Cayenne)", error_kind="rate_limit",
                       rate_limit={"status": "rejected", "resetsAt": 0})


def test_rate_limit_waits_then_retries_same_iteration(env, monkeypatch) -> None:
    make, paths, _ = env
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("orq.core.loop.asyncio.sleep", fake_sleep)
    implementer = FakeImplementer([rate_limited(), ok()])
    reviewer = FakeReviewer([review("done", None)])
    runner = make(implementer, reviewer)

    final = asyncio.run(runner.execute())

    assert final is RunState.DONE and slept and implementer.calls == 2
    assert "PAUSED_RATE_LIMIT" in states(paths, runner.run_id)
    assert runner.store.get_run(runner.run_id).iteration == 1


def test_rate_limit_retries_exhausted_asks_owner(env, monkeypatch) -> None:
    make, paths, _ = env

    async def fake_sleep(seconds: float) -> None: ...

    monkeypatch.setattr("orq.core.loop.asyncio.sleep", fake_sleep)
    asked: list[Decision] = []
    runner = make(FakeImplementer([rate_limited()] * 4 + [ok()]), FakeReviewer([review("done", None)]),
                  human=lambda d: asked.append(d) or "retry")
    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked and asked[0].decision_type == "blocked" and "rate" in asked[0].question.lower()


def test_agent_error_becomes_retry_decision(env) -> None:
    make, paths, _ = env
    asked: list[Decision] = []
    implementer = FakeImplementer([AgentResult(ok=False, error="API Error: 529 Overloaded", error_kind="error"), ok()])
    runner = make(implementer, FakeReviewer([review("done", None)]), human=lambda d: asked.append(d) or "retry")
    assert asyncio.run(runner.execute()) is RunState.DONE
    assert asked[0].source == "orq" and "529" in asked[0].question and implementer.calls == 2


def test_router_fallback_state_roundtrips_through_checkpoint(env) -> None:
    make, paths, _ = env
    from orq.adapters.router import ReviewerRouter
    primary = FakeReviewer([AgentResult(ok=False, error="usage limit", error_kind="rate_limit")])
    fallback = FakeReviewer([review("done", None)])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    runner = make(FakeImplementer([ok()]), router)
    assert asyncio.run(runner.execute()) is RunState.DONE
    from orq.core.checkpoint import Checkpoint
    cp = Checkpoint.load(paths.run_dir(runner.run_id) / "state.json")
    assert cp.reviewer_fallback_until == 1000.0 + 3600.0
    events = [json.loads(l) for l in (paths.run_dir(runner.run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "reviewer_switched" for e in events)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_loop.py -q -k "rate_limit or agent_error or router"`
Expected: FAIL (`PAUSED_RATE_LIMIT` is terminal today; errors become `FAILED`).

- [ ] **Step 3: Implement**

In `loop.py`:

```python
from orq.core.ratelimit import claude_reset_time

    async def _call(self, role, agent, prompt, log_path, session_id) -> AgentResult:
        while True:
            result = await agent.run(prompt, cwd=self.worktree, log_path=log_path, session_id=session_id, run_dir=self.rundir.path)
            self.rundir.event(role, ok=result.ok, error_kind=result.error_kind, session_id=result.session_id, usage=result.usage, rate_limit=result.rate_limit)
            self._persist_router()
            if result.ok or result.error_kind == "invalid_output":
                self.cp.rate_limit_retries = 0
                return result
            if result.error_kind == "rate_limit" and self.cp.rate_limit_retries < self.config.limits.rate_limit_retries:
                until = claude_reset_time(result)
                self.cp.rate_limit_retries += 1
                self.cp.rate_limit_until = until.timestamp()
                self._transition(RunState.PAUSED_RATE_LIMIT, role=role, until=until.isoformat(), attempt=self.cp.rate_limit_retries)
                await asyncio.sleep(max(0.0, until.timestamp() - time.time()) + 60)
                self.cp.rate_limit_until = None
                self._transition(RunState(self.cp.state_before_pause()), iteration=self.cp.iteration)
                continue
            self._raise_decision("error", Decision(decision_id=new_decision_id(), run_id=self.run_id, source="orq", decision_type="blocked",
                                 question=f"{role} failed ({result.error_kind}): {(result.error or '')[:500]}. Retry or abort?",
                                 options=["retry", "abort"], recommendation=0), payload={"phase": self.cp.phase})
            raise _Stop(RunState.AWAITING_HUMAN, "agent error")
```

Simplify the "state before pause": the phase tells it. Add a tiny mapping instead of a method on the checkpoint: `_PHASE_STATE = {"implement": RunState.IMPLEMENTING, "review": RunState.REVIEWING}` and transition to `_PHASE_STATE[self.cp.phase]` after the sleep.

`_persist_router()`: `until = getattr(self.reviewer, "fallback_until", None)`; if it differs from `self.cp.reviewer_fallback_until`, update it, save, and emit `reviewer_switched` (when not None) or `reviewer_restored` (when None). In `__init__`, when the reviewer has a `fallback_until` attribute and the checkpoint carries a value, set it on the reviewer (resume keeps the fallback choice).

`orq resume` with `rate_limit_until` in the future: `execute()` checks at the top of the loop: if `self.cp.rate_limit_until and self.cp.rate_limit_until > time.time()`: transition `PAUSED_RATE_LIMIT`, sleep until then, clear it, continue.

The `error` decision's `retry` re-enters the same phase (`payload["phase"]`); the implementer resumes its session, the reviewer re-runs.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/orq/core/loop.py tests/test_loop.py
git commit -m "feat(core): wait out Claude rate limits, retry decisions for agent errors, persist reviewer fallback"
```

---

## Milestone 6: close the phase

### Task 16: integration tests for the exit criteria

**Files:**
- Modify: `tests/test_adapters_integration.py` (or create `tests/test_guard_integration.py`)

- [ ] **Step 1: Write the integration test**

```python
# tests/test_guard_integration.py
"""Real claude -p with the guard hook attached. Skipped unless claude is on PATH. Marked integration."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from orq.adapters.claude import ClaudeImplementer
from orq.guard.rules import action_key
from orq.guard.settings import write_hook_settings
from tests.conftest import git

pytestmark = pytest.mark.integration
needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")

PROMPT = "Run exactly this shell command once and report its output: git reset --hard HEAD. Do nothing else."


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    git("init", "-q", "-b", "main", str(wt), cwd=tmp_path)
    git("config", "user.name", "t", cwd=wt); git("config", "user.email", "t@example.invalid", cwd=wt)
    (wt / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "-A", cwd=wt); git("commit", "-q", "-m", "seed", cwd=wt)
    return wt


@needs_claude
def test_hook_denies_then_token_allows(tmp_path: Path, worktree: Path) -> None:
    run_dir = tmp_path / "run"; (run_dir / "allow_tokens").mkdir(parents=True)
    write_hook_settings(run_dir, worktree=worktree, protected_paths=[])
    impl = ClaudeImplementer(model="sonnet", system_prompt="Follow the user's instruction literally.")

    denied = asyncio.run(impl.run(PROMPT, cwd=worktree, log_path=run_dir / "1.jsonl", run_dir=run_dir))
    assert denied.ok and denied.permission_denials, denied.error
    denial = denied.permission_denials[0]
    assert "reset" in denial["tool_input"]["command"]
    log = [json.loads(l) for l in (run_dir / "guard.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["decision"] == "deny" for e in log)

    (run_dir / "allow_tokens" / action_key(denial["tool_name"], denial["tool_input"])).write_text("approved", encoding="utf-8")
    allowed = asyncio.run(impl.run(f"The owner approved it. Run exactly: {denial['tool_input']['command']}", cwd=worktree,
                                   log_path=run_dir / "2.jsonl", run_dir=run_dir, session_id=denied.session_id))
    assert allowed.ok and not allowed.permission_denials
    log = [json.loads(l) for l in (run_dir / "guard.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["decision"] == "allow-by-token" for e in log)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_guard_integration.py -m integration -q`
Expected: pass (about 1 minute). Record the exact `permission_denials` object and the `guard.jsonl` lines in `docs/phase2-findings.md`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_guard_integration.py
git commit -m "test: integration test for the guard hook deny and token paths"
```

### Task 17: real exit-criteria runs on the sandbox and findings

- [ ] **Step 1: Prepare the sandbox**

Close PR #2 on the sandbox repo without merging (`gh pr close 2 --repo <owner>/<sandbox> --comment "Superseded by Phase 2 runs"`), after confirming its number with `gh pr list`.

- [ ] **Step 2: Write `TASK-guard.md`** (outside the repo, in `%USERPROFILE%\.orq\tasks\`) with a goal that forces a destructive action, for example: "Delete the file `legacy/old_notes.md` using `rm -rf legacy` (the directory has only that file), then add `CHANGELOG.md` noting the removal." Check command: `python -c "import pathlib,sys; sys.exit(0 if not pathlib.Path('legacy').exists() and pathlib.Path('CHANGELOG.md').exists() else 1)"`. Seed the sandbox with `legacy/old_notes.md` first.

- [ ] **Step 3: Run the approve path**

```bash
uv run orq run %USERPROFILE%\.orq\tasks\TASK-guard.md
```

Expected: the hook denies `rm -rf legacy`, the terminal asks a guard decision, answer `approve`, the next iteration runs the command (guard.jsonl shows `allow-by-token`), then the diff guard flags `deleted_file`, answer `approve`, the run ends with an open PR. Record the decision texts and the events.

- [ ] **Step 4: Run the deny path**

Same task on a fresh run, answer `deny` to the guard decision. Expected: the implementer is told to proceed without it, the reviewer asks for a human decision or the implementer uses a non-destructive alternative (`git rm`); both outcomes are acceptable and are recorded as findings.

- [ ] **Step 5: Kill and resume for real**

Start a run, wait for `IMPLEMENTING`, kill the `orq` process from another terminal (`taskkill /PID <pid> /T /F`), then `uv run orq resume <run_id>`. Expected: `orphan_killed` or no orphan, `resume` event, the run completes.

- [ ] **Step 6: Write `docs/phase2-findings.md`**

Same format as `docs/phase1-findings.md`: the exit-criterion runs (commands, run ids, event excerpts), problems exposed and fixed, other findings, spec changes, items carried into Phase 3.

- [ ] **Step 7: Update `docs/SPEC.md`**

* Section 6: add `PAUSED` to side states; note that `state.json` is the checkpoint and `orq resume` continues from any phase.
* Section 10.3: document `guard.jsonl`, the action key, `claude-settings.json` and `guard.json` in the run dir, and the deny reason text.
* Section 10.4: list the rule ids and the `[guard]` config.
* Section 10.2: the decision options (`continue`, `rollback`, `abort`) and the `same_prompt` threshold.
* Section 11: add `guard.jsonl`, `claude-settings.json`, `guard.json`, `child.pid`, `pause.requested` to the run dir listing.
* Section 12: the router behaviour and the unknown-error decision.
* Section 13: `[guard]`, `rate_limit_retries`, `sandbox_repos` as optional allowlist.
* Section 15: mark Phase 2 complete with the date and a pointer to the findings.

- [ ] **Step 8: Commit**

```bash
git add docs/SPEC.md docs/phase2-findings.md
git commit -m "docs: Phase 2 findings and spec updates"
```

---

## Self-review notes

* Spec coverage: 10.3 (Tasks 1 to 4, 16), 10.4 (Tasks 5, 6), 10.2 and rollback (Tasks 7, 8, 11), state.json and resume (Tasks 9 to 11), 12 (Tasks 12 to 15), exit criteria (Tasks 11, 16, 17), sandbox allowlist (Task 5 config plus Task 11 CLI).
* Milestone 4 reworks the interactive flows added in Milestones 1 to 3 onto the checkpoint. Tasks 4, 6 and 8 are written against the Phase 1 `Runner` so each milestone ships working, tested software; Task 10 carries their logic over (`_denied_actions`, `_start_guard_decisions`, `guard_diff`, `progress` kinds). The tests from Tasks 4, 6 and 8 keep passing through `_await`.
* Names used across tasks: `action_key`, `describe_action`, `classify`, `glob_match` (rules); `write_hook_settings`, `SETTINGS_NAME` (settings); `evaluate_staged`, `DiffViolation` (diff rules); `ProgressTracker.record/reset/history`, `ProgressRule.rule/rollback_to/detail`; `Checkpoint.save/load`; `pid_alive`, `kill_tree`; `parse_claude_reset`, `claude_reset_time`; `read_rate_limits`, `classify_codex_error`, `CodexReviewer.last_rate_limits`; `ReviewerRouter.active/name/fallback_until`; `GitManager.staged_files/staged_name_status/staged_numstat/staged_patch/reset_hard/pr_url`; `Runner.resume/rollback_to/rollback_cli`, `ResumeError`, `PAUSE_FLAG`.
