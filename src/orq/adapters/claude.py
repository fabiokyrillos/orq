"""Claude Code adapters: implementer (full tools) and read-only reviewer (SPEC sections 4 and 5)."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from orq.adapters.base import AgentResult, Completed, EventCallback, clean_env, split_command, stream_process
from orq.adapters.schema import REVIEW_CONTRACT, OutputContract
from orq.guard.settings import SETTINGS_NAME

# Owner's global plugins, hooks and MCP servers stay out; --settings hooks still load (Phase 0).
ISOLATION_FLAGS = ["--setting-sources", "project,local", "--strict-mcp-config"]

_MARKER_RE = re.compile(r"```orq-decision\s*\n(.*?)\n```\s*$", re.DOTALL)
_RATE_LIMIT_RE = re.compile(r"hit your .*limit|rate.?limit", re.IGNORECASE)
_AUTH_RE = re.compile(r"failed to authenticate|oauth", re.IGNORECASE)


def claude_argv() -> list[str]:
    """Resolve claude.exe behind the npm shim; ORQ_CLAUDE_EXE overrides."""
    override = os.environ.get("ORQ_CLAUDE_EXE")
    if override:
        return split_command(override)
    shim = shutil.which("claude")
    if shim is None:
        raise FileNotFoundError("claude is not on PATH")
    exe = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return [str(exe)] if exe.exists() else [shim]


def parse_decision_marker(text: str) -> dict | None:
    match = _MARKER_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _classify_error(text: str) -> str:
    if _RATE_LIMIT_RE.search(text):
        return "rate_limit"
    if _AUTH_RE.search(text):
        return "auth"
    return "error"


def _result_from(completed: Completed, session_id: str | None) -> AgentResult:
    init = next((e for e in completed.events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    final = next((e for e in reversed(completed.events) if e.get("type") == "result"), None)
    rate = next((e.get("rate_limit_info") for e in completed.events if e.get("type") == "rate_limit_event"), None)
    sid = (final or init or {}).get("session_id") or session_id

    if final is None:
        tail = "\n".join(completed.raw_lines[-5:])
        error = (completed.stderr.strip() or tail or "no result event").strip()
        return AgentResult(ok=False, session_id=sid, error=error, error_kind=_classify_error(error),
                           rate_limit=rate, exit_code=completed.exit_code)

    text = final.get("result") or ""
    base = dict(session_id=sid, usage=final.get("usage") or {}, rate_limit=rate,
                permission_denials=final.get("permission_denials") or [], exit_code=completed.exit_code)
    if final.get("is_error") or completed.exit_code != 0:
        return AgentResult(ok=False, text=text, error=text or completed.stderr.strip(), error_kind=_classify_error(text), **base)
    return AgentResult(ok=True, text=text, structured=final.get("structured_output"),
                       decision=parse_decision_marker(text), **base)


class ClaudeImplementer:
    name = "claude-implementer"

    def __init__(self, model: str, system_prompt: str, argv_prefix: list[str] | None = None, extra_args: list[str] | None = None) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self._prefix = argv_prefix
        self._extra = extra_args or []

    async def run(self, prompt: str, *, cwd: Path, log_path: Path, session_id: str | None = None,
                  run_dir: Path | None = None, on_event: EventCallback | None = None, model: str | None = None,
                  effort: str | None = None, contract: OutputContract | None = None) -> AgentResult:
        sid = session_id or str(uuid.uuid4())
        session_flag = ["--resume", sid] if session_id else ["--session-id", sid]
        # A resumed session keeps its context under a different --model (Phase 0); routing per milestone relies on it.
        argv = [*(self._prefix or claude_argv()), "-p", "--output-format", "stream-json", "--verbose",
                *ISOLATION_FLAGS, "--dangerously-skip-permissions", "--model", model or self.model,
                "--append-system-prompt", self.system_prompt, *session_flag]
        pid_file = None
        if run_dir is not None:
            # The runner writes the guard hook settings into the run dir (SPEC 10.3); attach them when present.
            settings = run_dir / SETTINGS_NAME
            if settings.exists():
                argv += ["--settings", str(settings)]
            pid_file = run_dir / "child.pid"
        argv += self._extra
        env = clean_env({"ORQ_RUN_DIR": str(run_dir)} if run_dir else None)
        completed = await stream_process(argv, cwd=cwd, stdin_text=prompt, log_path=log_path, env=env, on_event=on_event,
                                         pid_file=pid_file)
        return _result_from(completed, sid)


class ClaudeReviewer:
    name = "claude-reviewer"
    kind = "claude"

    def __init__(self, model: str = "opus", argv_prefix: list[str] | None = None) -> None:
        self.model = model
        self._prefix = argv_prefix

    async def run(self, prompt: str, *, cwd: Path, log_path: Path, session_id: str | None = None,
                  run_dir: Path | None = None, on_event: EventCallback | None = None, model: str | None = None,
                  effort: str | None = None, contract: OutputContract | None = None) -> AgentResult:
        contract = contract or REVIEW_CONTRACT
        argv = [*(self._prefix or claude_argv()), "-p", "--output-format", "stream-json", "--verbose",
                *ISOLATION_FLAGS, "--model", model or self.model, "--tools", "Read,Grep,Glob",
                "--json-schema", json.dumps(contract.schema)]
        if run_dir:
            argv += ["--add-dir", str(run_dir)]
        completed = await stream_process(argv, cwd=cwd, stdin_text=prompt, log_path=log_path, env=clean_env(), on_event=on_event)
        result = _result_from(completed, session_id)
        if result.ok:
            problems = contract.validate(result.structured)
            if problems:
                return AgentResult(ok=False, text=result.text, session_id=result.session_id, usage=result.usage,
                                   error="; ".join(problems), error_kind="invalid_output", exit_code=result.exit_code)
        return result


CHAT_SYSTEM_PROMPT = (
    "You are talking with the owner of this repository inside orq. The working directory is a read-only checkout of the "
    "project at the latest base branch. Answer the owner's questions about the code, its structure, behaviour and history "
    "by reading the files: cite paths and line numbers, say when something is not in the code, and keep answers short "
    "unless asked for detail. You cannot change files, run commands or start work; if the owner wants a change, suggest "
    "how to phrase it as an orq task. Always answer in Brazilian Portuguese."
)


class ClaudeChat:
    """Phase 7.1: the owner's conversation about a project. Read tools only; one session per conversation."""

    name = "claude-chat"

    def __init__(self, model: str = "opus", argv_prefix: list[str] | None = None) -> None:
        self.model = model
        self._prefix = argv_prefix

    async def run(self, prompt: str, *, cwd: Path, log_path: Path, session_id: str | None = None, model: str | None = None,
                  system_prompt: str | None = None, **_: object) -> AgentResult:
        sid = session_id or str(uuid.uuid4())
        session_flag = ["--resume", sid] if session_id else ["--session-id", sid]
        # The appended system prompt is not stored in the session, so each call may use another one (Phase 7.2: WhatsApp).
        argv = [*(self._prefix or claude_argv()), "-p", "--output-format", "stream-json", "--verbose", *ISOLATION_FLAGS,
                "--model", model or self.model, "--tools", "Read,Grep,Glob",
                "--append-system-prompt", system_prompt or CHAT_SYSTEM_PROMPT, *session_flag]
        completed = await stream_process(argv, cwd=cwd, stdin_text=prompt, log_path=log_path, env=clean_env())
        return _result_from(completed, sid)
