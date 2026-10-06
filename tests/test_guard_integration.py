"""Real `claude -p` with the guard hook attached (SPEC 10.3). Skipped unless claude is on PATH; marked integration."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from orq.adapters.claude import ClaudeImplementer
from orq.guard.rules import action_key
from orq.guard.settings import write_hook_settings
from tests.conftest import git

pytestmark = pytest.mark.integration
needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")

PROMPT = ("Run exactly this shell command once with the Bash tool and report its output: git reset --hard HEAD\n"
          "Do nothing else. If the command is denied, stop and end your turn.")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    git("init", "-q", "-b", "main", str(wt), cwd=tmp_path)
    git("config", "user.name", "t", cwd=wt)
    git("config", "user.email", "t@example.invalid", cwd=wt)
    (wt / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "seed", cwd=wt)
    return wt


def guard_log(run_dir: Path) -> list[dict]:
    return [json.loads(l) for l in (run_dir / "guard.jsonl").read_text(encoding="utf-8").splitlines()]


@needs_claude
def test_hook_denies_then_token_allows(tmp_path: Path, worktree: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "allow_tokens").mkdir(parents=True)
    write_hook_settings(run_dir, worktree=worktree, protected_paths=[])
    impl = ClaudeImplementer(model="sonnet", system_prompt="Follow the user's instruction literally.")

    denied = asyncio.run(impl.run(PROMPT, cwd=worktree, log_path=run_dir / "1.jsonl", run_dir=run_dir))

    assert denied.ok, denied.error
    assert denied.permission_denials, "the hook did not deny anything; see guard.jsonl"
    denial = denied.permission_denials[0]
    assert denial["tool_name"] == "Bash" and "reset" in denial["tool_input"]["command"]
    assert any(e["decision"] == "deny" and e["rule"] == "git_reset_hard" for e in guard_log(run_dir))

    (run_dir / "allow_tokens" / action_key(denial["tool_name"], denial["tool_input"])).write_text("approved", encoding="utf-8")
    allowed = asyncio.run(impl.run(
        f"The owner approved it. Run exactly this command once with the Bash tool and report its output: {denial['tool_input']['command']}",
        cwd=worktree, log_path=run_dir / "2.jsonl", run_dir=run_dir, session_id=denied.session_id))

    assert allowed.ok, allowed.error
    assert not allowed.permission_denials
    assert any(e["decision"] == "allow-by-token" for e in guard_log(run_dir))
    assert not list((run_dir / "allow_tokens").iterdir())
