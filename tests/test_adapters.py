import asyncio
import json
import sys
from pathlib import Path

import pytest

from orq.adapters.base import AgentResult
from orq.adapters.claude import ClaudeImplementer, ClaudeReviewer, parse_decision_marker
from orq.adapters.codex import CodexReviewer
from orq.adapters.schema import REVIEW_SCHEMA, validate_review

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(path))
    monkeypatch.setenv("CLAUDECODE", "1")  # simulates being launched from inside a Claude session
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://leak")
    return lambda: json.loads(path.read_text(encoding="utf-8"))


def scenario(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", name)


def run(agent, prompt: str, tmp_path: Path, **kwargs) -> AgentResult:
    return asyncio.run(agent.run(prompt, cwd=tmp_path, log_path=tmp_path / "stream.jsonl", **kwargs))


def implementer(**kwargs) -> ClaudeImplementer:
    return ClaudeImplementer(model="sonnet", system_prompt="rules", argv_prefix=[sys.executable, str(FAKES / "fake_claude.py")], **kwargs)


# ClaudeImplementer

def test_implementer_sends_prompt_on_stdin_with_isolation_flags(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    result = run(implementer(), "do the thing\n% ^ & \"quoted\"", tmp_path, run_dir=tmp_path / "run")

    argv = record()["argv"]
    assert record()["stdin"] == "do the thing\n% ^ & \"quoted\""
    for flag in ("-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "project,local",
                 "--strict-mcp-config", "--dangerously-skip-permissions", "--model", "sonnet", "--append-system-prompt", "rules"):
        assert flag in argv
    assert record()["env_claude_keys"] == []
    assert record()["orq_run_dir"] == str(tmp_path / "run")
    assert record()["cwd"] == str(tmp_path)
    assert result.ok is True


def test_implementer_new_session_uses_generated_session_id(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    result = run(implementer(), "hi", tmp_path)

    argv = record()["argv"]
    assert "--session-id" in argv and "--resume" not in argv
    assert result.session_id == argv[argv.index("--session-id") + 1]
    assert len(result.session_id) == 36


def test_implementer_resumes_existing_session(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    result = run(implementer(), "hi", tmp_path, session_id="abc-123")

    argv = record()["argv"]
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in argv
    assert result.session_id == "abc-123"


def test_implementer_success_result_and_stream_log(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    result = run(implementer(), "hi", tmp_path)

    assert result.ok is True
    assert result.text == "Implemented the thing.é"
    assert result.error_kind == "none"
    assert result.usage["output_tokens"] == 5
    assert result.rate_limit == {"status": "allowed", "resetsAt": 1791239400, "rateLimitType": "five_hour"}
    lines = (tmp_path / "stream.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["type"] for l in lines] == ["system", "assistant", "rate_limit_event", "result"]


def test_implementer_extracts_decision_marker(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "decision")

    result = run(implementer(), "hi", tmp_path)

    assert result.ok is True
    assert result.decision == {"decision_type": "business", "question": "Before or after tax?", "options": ["Before", "After"], "recommendation": 0}


def test_implementer_classifies_rate_limit(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "rate_limit")

    result = run(implementer(), "hi", tmp_path)

    assert result.ok is False
    assert result.error_kind == "rate_limit"
    assert "resets 10pm" in result.error


def test_implementer_classifies_auth_failure(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "auth")

    result = run(implementer(), "hi", tmp_path)

    assert result.ok is False
    assert result.error_kind == "auth"


def test_implementer_reports_permission_denials(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "denied")

    result = run(implementer(), "hi", tmp_path)

    assert result.ok is True
    assert result.permission_denials[0]["tool_input"]["command"] == "git reset --hard"


def test_implementer_crash_without_result_is_an_error(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "crash")

    result = run(implementer(), "hi", tmp_path)

    assert result.ok is False
    assert result.error_kind == "error"
    assert "No conversation found" in result.error
    assert result.exit_code == 1


def test_parse_decision_marker_requires_block_at_end() -> None:
    assert parse_decision_marker("text\n```orq-decision\n{\"a\": 1}\n```\nmore text") is None
    assert parse_decision_marker("no marker") is None
    assert parse_decision_marker("x\n```orq-decision\n{\"a\": 1}\n```\n") == {"a": 1}


# ClaudeReviewer

def test_claude_reviewer_is_read_only_with_schema(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "structured")
    reviewer = ClaudeReviewer(model="opus", argv_prefix=[sys.executable, str(FAKES / "fake_claude.py")])

    result = run(reviewer, "review this", tmp_path)

    argv = record()["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == REVIEW_SCHEMA
    assert "--dangerously-skip-permissions" not in argv
    assert "--strict-mcp-config" in argv
    assert result.ok is True
    assert result.structured["status"] == "done"


# CodexReviewer

def codex(**kwargs) -> CodexReviewer:
    return CodexReviewer(model="gpt-5.5", effort="low", argv_prefix=[sys.executable, str(FAKES / "fake_codex.py")], **kwargs)


def test_codex_argv_and_stdin_prompt(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    result = run(codex(), "review it", tmp_path)

    argv = record()["argv"]
    assert argv[:2] == ["-m", "gpt-5.5"]
    assert "exec" in argv and "--json" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert 'model_reasoning_effort="low"' in argv
    schema_path = Path(argv[argv.index("--output-schema") + 1])
    assert json.loads(schema_path.read_text(encoding="utf-8")) == REVIEW_SCHEMA
    assert argv[-1] == "-"
    assert record()["stdin"] == "review it"
    assert record()["env_claude_keys"] == []
    assert result.ok is True


def test_codex_takes_last_message_and_thread_id(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    result = run(codex(), "review it", tmp_path)

    assert result.structured["summary"] == "Looks fine so far"
    assert result.structured["next_prompt"] == "Add the tests"
    assert result.session_id == "thread-1"
    assert result.usage["input_tokens"] == 86381


def test_codex_ignore_user_config_adds_flags(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")

    run(codex(ignore_user_config=True), "review it", tmp_path)

    argv = record()["argv"]
    assert "--ignore-user-config" in argv
    assert 'windows.sandbox="elevated"' in argv


def test_codex_api_error_is_reported(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "schema_error")

    result = run(codex(), "review it", tmp_path)

    assert result.ok is False
    assert result.error_kind == "error"
    assert "invalid_json_schema" in result.error


def test_codex_non_schema_output_is_invalid(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "bad_output")

    result = run(codex(), "review it", tmp_path)

    assert result.ok is False
    assert result.error_kind == "invalid_output"


# schema validation

def test_validate_review_accepts_strict_shape() -> None:
    assert validate_review({"status": "done", "summary": "s", "milestone": "m", "next_prompt": None, "issues": [], "human": None}) == []


def test_validate_review_lists_problems() -> None:
    problems = validate_review({"status": "maybe", "summary": "s"})

    assert any("status" in p for p in problems)
    assert any("milestone" in p for p in problems)


def test_validate_review_needs_human_requires_human_object() -> None:
    problems = validate_review({"status": "needs_human", "summary": "s", "milestone": "m", "next_prompt": None, "issues": [], "human": None})

    assert any("human" in p for p in problems)
