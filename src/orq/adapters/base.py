"""Common agent interface and the subprocess plumbing shared by every CLI adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Variables injected by a parent Claude Code session; passing them on changes the child's behaviour.
_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC")

EventCallback = Callable[[dict], None]


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    session_id: str | None = None
    structured: dict | None = None
    decision: dict | None = None
    error: str | None = None
    error_kind: str = "none"  # none | rate_limit | auth | invalid_output | error
    usage: dict = field(default_factory=dict)
    rate_limit: dict | None = None
    permission_denials: list[dict] = field(default_factory=list)
    exit_code: int | None = None


class Agent(Protocol):
    name: str

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        log_path: Path,
        session_id: str | None = None,
        run_dir: Path | None = None,
        on_event: EventCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        contract: object | None = None,
    ) -> AgentResult:
        """model: implementer model override per call; effort and contract: reviewer reasoning effort and output schema."""
        ...


def split_command(value: str) -> list[str]:
    """Split an ORQ_*_CMD override into argv; quote tokens that contain spaces."""
    return [token.strip('"') for token in shlex.split(value, posix=False)]


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_SCRUB_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


@dataclass
class Completed:
    exit_code: int
    events: list[dict]
    stderr: str
    raw_lines: list[str]


async def stream_process(
    argv: list[str],
    *,
    cwd: Path,
    stdin_text: str,
    log_path: Path,
    env: dict[str, str],
    on_event: EventCallback | None = None,
    pid_file: Path | None = None,
) -> Completed:
    """Run a CLI, feed the prompt on stdin, mirror every stdout line to log_path, parse JSON lines.

    While the child runs, its pid is kept in pid_file (if given) so a resumed orq can kill an orphan.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    if pid_file is not None:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid), encoding="utf-8")

    async def feed() -> None:
        proc.stdin.write(stdin_text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

    events: list[dict] = []
    raw_lines: list[str] = []

    async def read_stdout() -> None:
        with log_path.open("a", encoding="utf-8") as log:
            while True:
                chunk = await proc.stdout.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
                raw_lines.append(line)
                log.write(line + "\n")
                log.flush()
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append(event)
                    if on_event:
                        on_event(event)

    async def read_stderr() -> bytes:
        return await proc.stderr.read()

    _, _, stderr = await asyncio.gather(feed(), read_stdout(), read_stderr())
    exit_code = await proc.wait()
    if pid_file is not None:
        pid_file.unlink(missing_ok=True)
    return Completed(exit_code, events, stderr.decode("utf-8", errors="replace"), raw_lines)
