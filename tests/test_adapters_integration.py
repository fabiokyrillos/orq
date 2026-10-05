"""Real CLI calls. Run with: uv run pytest -m integration tests/test_adapters_integration.py"""

import asyncio
import shutil
from pathlib import Path

import pytest

from orq.adapters.claude import ClaudeImplementer, ClaudeReviewer
from orq.adapters.codex import CodexReviewer
from tests.conftest import git

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(origin: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    git("clone", "-q", str(origin), str(path), cwd=tmp_path)
    (path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "add app.py", cwd=path)
    return path


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_real_claude_implementer_round_trip(repo: Path, tmp_path: Path) -> None:
    agent = ClaudeImplementer(model="sonnet", system_prompt="Reply tersely.")

    result = asyncio.run(agent.run("Reply with exactly: OK", cwd=repo, log_path=tmp_path / "impl.jsonl", run_dir=tmp_path))

    assert result.ok, result.error
    assert result.text.strip() == "OK"
    assert result.session_id and result.rate_limit is not None


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_real_claude_reviewer_returns_schema(repo: Path, tmp_path: Path) -> None:
    agent = ClaudeReviewer(model="sonnet")

    result = asyncio.run(agent.run("Review app.py in one sentence. status must be done.", cwd=repo, log_path=tmp_path / "rev.jsonl"))

    assert result.ok, result.error
    assert result.structured["status"] in ("done", "continue", "needs_human")


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex not installed")
def test_real_codex_reviewer_isolated(repo: Path, tmp_path: Path) -> None:
    agent = CodexReviewer(model="gpt-5.5", effort="low", ignore_user_config=True)

    result = asyncio.run(agent.run(
        "Review app.py. Run `git log --oneline -1` and try once to create probe.txt; report in summary whether the write was blocked. status must be done.",
        cwd=repo, log_path=tmp_path / "codex.jsonl",
    ))

    assert result.ok, result.error
    assert result.structured["status"] == "done"
    assert not (repo / "probe.txt").exists()
    print("codex usage:", result.usage)
