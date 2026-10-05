"""Shared helpers for Phase 0 probes. Probes are throwaway validation scripts, not product code."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

P0 = Path.home() / ".orq" / "phase0"
OUT = P0 / "out"
SCRATCH = P0 / "scratch"

# Variables injected by a parent Claude Code session; a real orq run would not have them.
_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC")


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_SCRUB_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def claude_argv() -> list[str]:
    """Resolve the real claude.exe behind the npm .cmd shim so no shell is involved."""
    shim = shutil.which("claude")
    if shim is None:
        raise FileNotFoundError("claude not on PATH")
    exe = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return [str(exe)] if exe.exists() else [shim]


def codex_argv() -> list[str]:
    """Resolve node + codex.js behind the npm .cmd shim so no shell is involved."""
    shim = shutil.which("codex")
    if shim is None:
        raise FileNotFoundError("codex not on PATH")
    script = Path(shim).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = shutil.which("node")
    return [node, str(script)] if script.exists() and node else [shim]


@dataclass
class Result:
    name: str
    argv: list[str]
    code: int | None
    seconds: float
    stdout: str
    stderr: str
    timed_out: bool

    def jsonl(self) -> list[dict]:
        events = []
        for line in self.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return events

    def json(self):
        return json.loads(self.stdout)


def run(
    name: str,
    argv: list[str],
    cwd: Path,
    stdin_text: str | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: int = 300,
) -> Result:
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            input=(stdin_text or "").encode("utf-8"),
            capture_output=True,
            env=clean_env(env_extra),
            timeout=timeout,
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, code, out, err = True, None, exc.stdout or b"", exc.stderr or b""
    seconds = round(time.monotonic() - started, 1)
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    (OUT / f"{name}.stdout.txt").write_text(stdout, encoding="utf-8")
    (OUT / f"{name}.stderr.txt").write_text(stderr, encoding="utf-8")
    meta = {"argv": argv, "cwd": str(cwd), "exit_code": code, "seconds": seconds, "timed_out": timed_out}
    (OUT / f"{name}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return Result(name, argv, code, seconds, stdout, stderr, timed_out)


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")


def header(result: Result) -> None:
    print(f"\n=== {result.name}  exit={result.code}  {result.seconds}s  timed_out={result.timed_out}")
    if result.stderr.strip():
        print("  stderr:", result.stderr.strip()[:400])
