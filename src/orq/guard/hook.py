"""PreToolUse hook executed by Claude Code for every Bash/Write/Edit call (SPEC 10.3).

Reads the payload on stdin, denies destructive actions unless a one-time token exists, and logs every
decision to <ORQ_RUN_DIR>/guard.jsonl. Any internal failure allows the call and logs it: the post-execution
guard and the reviewer are the next layers, and a crashing hook must never block the implementer.

Runs as a plain script (`python hook.py`), so `src/` is put on sys.path explicitly.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orq.guard.rules import Violation, action_key, classify, describe_action  # noqa: E402

REASON = (
    "orq guard: {description} is destructive and needs owner approval (rule {rule}). "
    "Do not work around it with another command. Finish any independent work, then end your turn "
    "and state the exact command you need and why."
)


def main() -> int:
    run_dir = _run_dir()
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (ValueError, UnicodeDecodeError) as exc:
        _log(run_dir, {"decision": "allow", "error": f"bad payload: {exc}"})
        return 0
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    config = _load_config(run_dir)
    worktree = Path(config["worktree"]) if config.get("worktree") else None
    try:
        violation = classify(tool_name, tool_input, worktree=worktree, protected_paths=list(config.get("protected_paths", [])))
    except Exception as exc:  # noqa: BLE001 - see module docstring
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


def _run_dir() -> Path:
    env = os.environ.get("ORQ_RUN_DIR")
    if env:
        return Path(env)
    try:
        return Path.home() / ".orq" / "guard-orphan"
    except RuntimeError:  # no USERPROFILE/HOME in the environment
        return Path("orq-guard-orphan")


def _load_config(run_dir: Path) -> dict:
    try:
        data = json.loads((run_dir / "guard.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _log(run_dir: Path, record: dict) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
        with (run_dir / "guard.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
