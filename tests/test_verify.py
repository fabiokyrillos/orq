import json
import shutil
import subprocess
from pathlib import Path

import pytest

from orq.verify.checks import run_check
from orq.verify.secrets import SecretScanner
from tests.conftest import git


def test_check_passes_and_captures_output(tmp_path: Path) -> None:
    result = run_check('python -c "print(\'caf\\u00e9 ok\')"', cwd=tmp_path, timeout=30)

    assert result.ok is True
    assert result.exit_code == 0
    assert "café ok" in result.output


def test_check_fails_with_exit_code_and_output(tmp_path: Path) -> None:
    result = run_check('python -c "import sys; print(\'boom\'); sys.exit(3)"', cwd=tmp_path, timeout=30)

    assert result.ok is False
    assert result.exit_code == 3
    assert "boom" in result.output


def test_check_timeout_is_a_failure(tmp_path: Path) -> None:
    result = run_check('python -c "import time; time.sleep(5)"', cwd=tmp_path, timeout=1)

    assert result.ok is False
    assert result.timed_out is True


def test_check_signature_ignores_numbers_and_is_stable(tmp_path: Path) -> None:
    first = run_check('python -c "print(\'FAILED test_x took 0.12s\')"', cwd=tmp_path, timeout=30)
    second = run_check('python -c "print(\'FAILED test_x took 0.98s\')"', cwd=tmp_path, timeout=30)
    other = run_check('python -c "print(\'FAILED test_y took 0.12s\')"', cwd=tmp_path, timeout=30)

    assert first.signature == second.signature
    assert first.signature != other.signature


def _fake_gitleaks(exit_code: int, findings: list[dict]):
    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        report = Path(args[args.index("--report-path") + 1])
        report.write_text(json.dumps(findings), encoding="utf-8")
        return subprocess.CompletedProcess(args, exit_code, stdout="", stderr="INF scanned")

    return runner


def test_staged_scan_reports_findings(tmp_path: Path) -> None:
    scanner = SecretScanner(runner=_fake_gitleaks(1, [{"RuleID": "github-pat", "File": "a.py", "StartLine": 3}]))

    result = scanner.scan_staged(tmp_path, report_path=tmp_path / "leaks.json")

    assert result.clean is False
    assert result.findings == [{"RuleID": "github-pat", "File": "a.py", "StartLine": 3}]


def test_staged_scan_clean(tmp_path: Path) -> None:
    scanner = SecretScanner(runner=_fake_gitleaks(0, []))

    result = scanner.scan_staged(tmp_path, report_path=tmp_path / "leaks.json")

    assert result.clean is True
    assert result.findings == []


def test_range_scan_uses_log_opts(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        seen.append(args)
        return _fake_gitleaks(0, [])(args, cwd)

    SecretScanner(runner=runner).scan_range(tmp_path, "origin/main", report_path=tmp_path / "r.json")

    assert "--log-opts=origin/main..HEAD" in seen[0]
    assert "--pre-commit" not in seen[0]


def test_unexpected_exit_code_raises(tmp_path: Path) -> None:
    scanner = SecretScanner(runner=_fake_gitleaks(126, []))

    with pytest.raises(RuntimeError, match="gitleaks"):
        scanner.scan_staged(tmp_path, report_path=tmp_path / "leaks.json")


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_real_gitleaks_finds_staged_secret(origin: Path, tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    (repo / "leaky.py").write_text('TOKEN = "ghp_' + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + '"\n', encoding="utf-8")
    git("add", "leaky.py", cwd=repo)

    result = SecretScanner().scan_staged(repo, report_path=tmp_path / "leaks.json")

    assert result.clean is False
    assert result.findings[0]["RuleID"] == "github-pat"
