"""gitleaks scans before commit and before push (SPEC section 10.5)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    findings: list[dict] = field(default_factory=list)


def _run_gitleaks(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    exe = shutil.which("gitleaks")
    if exe is None:
        raise RuntimeError("gitleaks is not installed (winget install Gitleaks.Gitleaks)")
    return subprocess.run([exe, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")


class SecretScanner:
    def __init__(self, runner: Runner = _run_gitleaks) -> None:
        self._runner = runner

    def scan_staged(self, worktree: Path, report_path: Path) -> ScanResult:
        return self._scan(["git", "--pre-commit", "--staged"], worktree, report_path)

    def scan_range(self, worktree: Path, base_ref: str, report_path: Path) -> ScanResult:
        return self._scan(["git", f"--log-opts={base_ref}..HEAD"], worktree, report_path)

    def _scan(self, mode: list[str], worktree: Path, report_path: Path) -> ScanResult:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        args = [*mode, "--redact", "--no-banner", "--exit-code", "1",
                "--report-format", "json", "--report-path", str(report_path), str(worktree)]
        proc = self._runner(args, worktree)
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"gitleaks failed ({proc.returncode}): {proc.stderr.strip()}")
        findings = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else []
        return ScanResult(clean=proc.returncode == 0 and not findings, findings=findings or [])
