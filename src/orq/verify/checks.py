"""Runs the task's local check command and captures its output."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_DIGITS = re.compile(r"\d+(\.\d+)?")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    exit_code: int | None
    output: str
    timed_out: bool

    @property
    def signature(self) -> str:
        """Stable fingerprint of the output tail, numbers removed, for no-progress detection."""
        tail = "\n".join(self.output.strip().splitlines()[-40:])
        return hashlib.sha256(_DIGITS.sub("N", tail).encode("utf-8")).hexdigest()[:16]


def run_check(command: str, cwd: Path, timeout: float) -> CheckResult:
    # The command comes from TASK.md written by the owner, so a shell is acceptable here.
    # Without these, a Python child on Windows writes the console code page, not UTF-8.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        output = _decode(exc.stdout) + _decode(exc.stderr)
        return CheckResult(ok=False, exit_code=None, output=output + f"\n[orq] timed out after {timeout}s", timed_out=True)
    output = _decode(proc.stdout) + _decode(proc.stderr)
    return CheckResult(ok=proc.returncode == 0, exit_code=proc.returncode, output=output, timed_out=False)


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")
