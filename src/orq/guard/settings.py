"""Writes the per-run Claude Code settings that attach the guard hook (SPEC 10.3).

Both files live in the run dir, never in the worktree: the repos are public.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOK_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"
SETTINGS_NAME = "claude-settings.json"
GUARD_CONFIG_NAME = "guard.json"
GUARD_LOG_NAME = "guard.jsonl"


def hook_script() -> Path:
    return Path(__file__).with_name("hook.py").resolve()


def write_hook_settings(run_dir: Path, *, worktree: Path, protected_paths: list[str]) -> Path:
    """Write claude-settings.json and guard.json into run_dir; return the settings path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    # Forward slashes in double quotes: the hook command runs through Git Bash (Phase 0).
    command = f'"{Path(sys.executable).resolve().as_posix()}" "{hook_script().as_posix()}"'
    settings = {"hooks": {"PreToolUse": [{"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]}]}}
    path = run_dir / SETTINGS_NAME
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    guard = {"worktree": str(worktree), "protected_paths": list(protected_paths)}
    (run_dir / GUARD_CONFIG_NAME).write_text(json.dumps(guard, indent=2), encoding="utf-8")
    return path
