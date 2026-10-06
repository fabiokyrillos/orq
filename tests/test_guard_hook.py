"""Runs the PreToolUse hook script as Claude Code would: a subprocess with the payload on stdin."""

import json
import os
import subprocess
import sys
from pathlib import Path

from orq.guard.rules import action_key
from orq.guard.settings import write_hook_settings

HOOK = Path(__file__).resolve().parents[1] / "src" / "orq" / "guard" / "hook.py"


def run_hook(run_dir: Path, payload: dict | bytes) -> subprocess.CompletedProcess:
    # A bare environment: the hook may only rely on python.exe and the checkout.
    env = {"ORQ_RUN_DIR": str(run_dir), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": ""}
    data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True, env=env)


def payload(tool: str, tool_input: dict) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": "C:/wt", "session_id": "s"}


def prepare(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "allow_tokens").mkdir(parents=True)
    write_hook_settings(run_dir, worktree=tmp_path / "wt", protected_paths=[".github/**"])
    return run_dir


def log_lines(run_dir: Path) -> list[dict]:
    return [json.loads(l) for l in (run_dir / "guard.jsonl").read_text(encoding="utf-8").splitlines()]


def decision(proc: subprocess.CompletedProcess) -> str:
    return json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"]


def test_settings_file_points_at_this_python_and_hook(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    settings = json.loads((run_dir / "claude-settings.json").read_text(encoding="utf-8"))
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash|Write|Edit|MultiEdit|NotebookEdit"
    command = hook["hooks"][0]["command"]
    assert "\\" not in command
    assert Path(sys.executable).as_posix() in command and command.endswith('hook.py"')
    guard = json.loads((run_dir / "guard.json").read_text(encoding="utf-8"))
    assert guard["protected_paths"] == [".github/**"] and guard["worktree"].endswith("wt")


def test_destructive_call_is_denied_with_json(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, payload("Bash", {"command": "git reset --hard HEAD~1"}))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "git_reset_hard" in out["permissionDecisionReason"] and "Do not work around it" in out["permissionDecisionReason"]
    last = log_lines(run_dir)[-1]
    assert last["decision"] == "deny" and last["rule"] == "git_reset_hard"
    assert last["action_key"] == action_key("Bash", {"command": "git reset --hard HEAD~1"})
    assert last["tool_input"] == {"command": "git reset --hard HEAD~1"}


def test_token_allows_once(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    tool_input = {"command": "git reset --hard HEAD~1"}
    token = run_dir / "allow_tokens" / action_key("Bash", tool_input)
    token.write_text("approved", encoding="utf-8")

    first = run_hook(run_dir, payload("Bash", tool_input))
    assert first.returncode == 0 and first.stdout.strip() == b"" and not token.exists()
    assert log_lines(run_dir)[-1]["decision"] == "allow-by-token"

    second = run_hook(run_dir, payload("Bash", tool_input))
    assert decision(second) == "deny"


def test_safe_call_prints_nothing(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, payload("Bash", {"command": "git status"}))
    assert proc.returncode == 0 and proc.stdout.strip() == b""
    assert log_lines(run_dir)[-1]["decision"] == "allow"


def test_write_outside_worktree_uses_guard_json(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    assert decision(run_hook(run_dir, payload("Write", {"file_path": str(tmp_path / "elsewhere.txt")}))) == "deny"
    assert run_hook(run_dir, payload("Write", {"file_path": str(tmp_path / "wt" / "ok.txt")})).stdout.strip() == b""
    assert decision(run_hook(run_dir, payload("Edit", {"file_path": str(tmp_path / "wt" / ".github" / "ci.yml")}))) == "deny"


def test_utf8_survives(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, payload("Bash", {"command": "rm -rf café ✓"}))
    reason = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "café ✓" in reason


def test_malformed_payload_allows_and_logs(tmp_path: Path) -> None:
    run_dir = prepare(tmp_path)
    proc = run_hook(run_dir, b"not json")
    assert proc.returncode == 0 and proc.stdout.strip() == b""
    assert log_lines(run_dir)[-1]["decision"] == "allow" and "bad payload" in log_lines(run_dir)[-1]["error"]


def test_missing_guard_json_still_denies_commands(tmp_path: Path) -> None:
    run_dir = tmp_path / "bare"
    (run_dir / "allow_tokens").mkdir(parents=True)
    assert decision(run_hook(run_dir, payload("Bash", {"command": "git push --force"}))) == "deny"
