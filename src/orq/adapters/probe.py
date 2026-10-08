"""One minimal call to check that a model works with the local CLI and plan (Phase 6 "Test model" button).

The same call as the Phase 6 milestone 0 probes: read-only, low effort, a one-word answer. It costs a few thousand tokens.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable

from orq.adapters.base import clean_env
from orq.adapters.claude import ISOLATION_FLAGS, claude_argv
from orq.adapters.codex import codex_argv
from orq.core.procs import no_window

PROMPT = "Reply with the single word OK."
Runner = Callable[..., subprocess.CompletedProcess]


def _codex(model: str, effort: str, windows_sandbox: str) -> list[str]:
    argv = [*codex_argv(), "-m", model, "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
            "--ignore-user-config", "-c", f'model_reasoning_effort="{effort}"']
    if windows_sandbox:
        argv += ["-c", f'windows.sandbox="{windows_sandbox}"']
    return [*argv, "-"]


def _codex_outcome(stdout: str) -> tuple[bool, str]:
    answer, failure = None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            answer = str(event["item"].get("text", ""))
        elif event.get("type") in ("turn.failed", "error"):
            message = (event.get("error") or {}).get("message") or event.get("message") or ""
            try:  # the API error arrives as JSON inside the message
                message = json.loads(message)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            failure = failure or str(message)
    if failure:
        return False, failure
    return (True, answer.strip()[:80]) if answer is not None else (False, "no answer from codex")


def _claude_outcome(stdout: str) -> tuple[bool, str]:
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, (stdout.strip()[-300:] or "no answer from claude")
    text = str(result.get("result") or "")
    return (False, text[:300] or "error") if result.get("is_error") else (True, text.strip()[:80])


def probe_model(kind: str, model: str, *, effort: str = "low", windows_sandbox: str = "unelevated",
                run: Runner = subprocess.run, timeout: float = 180) -> tuple[bool, str]:
    """(ok, the answer or the CLI's error message)."""
    if kind == "codex":
        argv = _codex(model, effort, windows_sandbox)
    elif kind == "claude":
        argv = [*claude_argv(), "-p", "--output-format", "json", *ISOLATION_FLAGS, "--model", model]
    else:
        raise ValueError(f"unknown model kind {kind}")
    with tempfile.TemporaryDirectory(prefix="orq-probe-") as cwd:
        try:
            proc = run(argv, cwd=cwd, input=PROMPT, capture_output=True, text=True, encoding="utf-8", env=clean_env(), timeout=timeout,
                       creationflags=no_window())
        except subprocess.TimeoutExpired:
            return False, f"no answer within {timeout:.0f} s"
        except FileNotFoundError as exc:
            return False, str(exc)
    return _codex_outcome(proc.stdout) if kind == "codex" else _claude_outcome(proc.stdout)
