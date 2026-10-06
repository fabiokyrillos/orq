"""Codex CLI reviewer adapter (SPEC sections 4 and 8.2)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from orq.adapters.base import AgentResult, EventCallback, clean_env, split_command, stream_process
from orq.adapters.schema import REVIEW_CONTRACT, OutputContract

_LIMIT_RE = re.compile(r"usage limit|rate limit|quota|too many requests|\b429\b", re.IGNORECASE)
_AUTH_RE = re.compile(r"unauthori[sz]ed|not logged in|codex login|\b401\b", re.IGNORECASE)


def sessions_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"


def read_rate_limits(thread_id: str | None, sessions_root: Path | None = None) -> dict | None:
    """Latest `rate_limits` snapshot from the Codex session file of `thread_id`, or None (Phase 0 section 5).

    Files live at <sessions>/<yyyy>/<mm>/<dd>/rollout-<ts>-<thread_id>[_<sub>].jsonl; every `token_count`
    event carries the snapshot under `payload.rate_limits`. This is not in the `--json` stdout stream.
    """
    if not thread_id:
        return None
    root = sessions_root if sessions_root is not None else globals()["sessions_root"]()
    if not root.exists():
        return None
    matches = sorted(root.glob(f"*/*/*/rollout-*{thread_id}*.jsonl"))
    if not matches:
        return None
    latest: dict | None = None
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


def codex_argv() -> list[str]:
    """Resolve node + codex.js behind the npm shim; ORQ_CODEX_CMD (space separated) overrides."""
    override = os.environ.get("ORQ_CODEX_CMD")
    if override:
        return split_command(override)
    shim = shutil.which("codex")
    if shim is None:
        raise FileNotFoundError("codex is not on PATH")
    script = Path(shim).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = shutil.which("node")
    return [node, str(script)] if script.exists() and node else [shim]


class CodexReviewer:
    name = "codex-reviewer"

    def __init__(self, model: str, effort: str = "low", ignore_user_config: bool = False, argv_prefix: list[str] | None = None) -> None:
        self.model = model
        self.effort = effort
        self.ignore_user_config = ignore_user_config
        self._prefix = argv_prefix
        self.last_rate_limits: dict | None = None

    async def run(self, prompt: str, *, cwd: Path, log_path: Path, session_id: str | None = None,
                  run_dir: Path | None = None, on_event: EventCallback | None = None, model: str | None = None,
                  effort: str | None = None, contract: OutputContract | None = None) -> AgentResult:
        contract = contract or REVIEW_CONTRACT
        schema_path = log_path.parent / f"{contract.name}.schema.json"
        schema_path.write_text(json.dumps(contract.schema, indent=2), encoding="utf-8")
        last_path = log_path.parent / f"{contract.name}.last.json"
        last_path.unlink(missing_ok=True)

        # The model is explicit: config.toml may name one the CLI cannot use (Phase 0 finding).
        argv = [*(self._prefix or codex_argv()), "-m", model or self.model, "exec", "--json", "--sandbox", "read-only",
                "-C", str(cwd), "--output-schema", str(schema_path), "-o", str(last_path),
                "-c", f'model_reasoning_effort="{effort or self.effort}"']
        if self.ignore_user_config:
            # Keeps the owner's plugins, hooks and MCP servers out; the Windows sandbox setting must then be restated.
            argv += ["--ignore-user-config", "-c", 'windows.sandbox="elevated"']
        argv.append("-")  # prompt on stdin

        completed = await stream_process(argv, cwd=cwd, stdin_text=prompt, log_path=log_path, env=clean_env(), on_event=on_event)

        thread_id = next((e.get("thread_id") for e in completed.events if e.get("type") == "thread.started"), None)
        usage = next((e.get("usage") or {} for e in completed.events if e.get("type") == "turn.completed"), {})
        failure = next((e for e in completed.events if e.get("type") in ("error", "turn.failed")), None)
        self.last_rate_limits = read_rate_limits(thread_id)
        if failure or completed.exit_code != 0:
            message = (failure or {}).get("message") or ((failure or {}).get("error") or {}).get("message") or completed.stderr.strip()
            return AgentResult(ok=False, session_id=thread_id, error=message, error_kind=classify_codex_error(message, self.last_rate_limits),
                               usage=usage, rate_limit=self.last_rate_limits, exit_code=completed.exit_code)

        # Every agent_message is schema-shaped, including progress notes; only the last one is the review.
        messages = [e["item"].get("text", "") for e in completed.events
                    if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
        text = last_path.read_text(encoding="utf-8") if last_path.exists() else (messages[-1] if messages else "")
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            structured = None
        problems = contract.validate(structured)
        if problems:
            return AgentResult(ok=False, text=text, session_id=thread_id, error="; ".join(problems),
                               error_kind="invalid_output", usage=usage, rate_limit=self.last_rate_limits, exit_code=completed.exit_code)
        return AgentResult(ok=True, text=text, session_id=thread_id, structured=structured, usage=usage,
                           rate_limit=self.last_rate_limits, exit_code=completed.exit_code)
