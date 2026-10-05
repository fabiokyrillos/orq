"""Minimal PreToolUse guard used only by the Phase 0 hook probe.

Denies `git reset --hard` unless a one-time allow token exists. Deny format is chosen by
ORQ_GUARD_MODE: "exit2" (exit code 2 + stderr) or "json" (permissionDecision on stdout).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

run_dir = Path(os.environ.get("ORQ_RUN_DIR", Path.home() / ".orq" / "phase0" / "out"))
mode = os.environ.get("ORQ_GUARD_MODE", "json")
payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
command = str(payload.get("tool_input", {}).get("command", ""))
destructive = payload.get("tool_name") == "Bash" and re.search(r"git\s+reset\s+--hard", command)
token = run_dir / "allow_tokens" / "reset-hard"

decision = "allow"
if destructive:
    if token.exists():
        token.unlink()
        decision = "allow-by-token"
    else:
        decision = "deny"

record = {
    "decision": decision,
    "mode": mode,
    "tool_name": payload.get("tool_name"),
    "command": command,
    "payload_keys": sorted(payload.keys()),
    "cwd": payload.get("cwd"),
    "permission_mode": payload.get("permission_mode"),
    "run_dir_from_env": "ORQ_RUN_DIR" in os.environ,
    "python": sys.executable,
    "shell_hints": {k: os.environ.get(k) for k in ("SHELL", "COMSPEC", "MSYSTEM", "PSModulePath") if os.environ.get(k)},
}
run_dir.mkdir(parents=True, exist_ok=True)
with (run_dir / "hook-log.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")

if decision != "deny":
    sys.exit(0)
reason = "orq guard: `git reset --hard` is destructive and needs owner approval (café ✓)."
if mode == "exit2":
    sys.stderr.buffer.write(reason.encode("utf-8"))
    sys.exit(2)
sys.stdout.buffer.write(
    json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
    ).encode("utf-8")
)
sys.exit(0)
