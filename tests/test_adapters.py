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

def test_implementer_adds_settings_when_run_dir_has_hook(record, tmp_path, monkeypatch) -> None:
    from orq.guard.settings import write_hook_settings
    scenario(monkeypatch, "ok")
    run_dir = tmp_path / "run"
    write_hook_settings(run_dir, worktree=tmp_path / "wt", protected_paths=[])

    run(implementer(), "hi", tmp_path, run_dir=run_dir)

    argv = record()["argv"]
    assert argv[argv.index("--settings") + 1] == str(run_dir / "claude-settings.json")
    assert record()["child_pid"].isdigit()                 # pid file was present while the CLI ran
    assert not (run_dir / "child.pid").exists()            # and removed afterwards


def test_implementer_without_hook_settings_has_no_settings_flag(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")
    run(implementer(), "hi", tmp_path, run_dir=tmp_path / "run")
    assert "--settings" not in record()["argv"]



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


def write_session_file(root: Path, thread_id: str, snapshots: list[dict]) -> None:
    day = root / "2026" / "10" / "06"
    day.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": "t", "ordinal": i, "type": "event_msg",
              "payload": {"type": "token_count", "info": None, "rate_limits": snap}} for i, snap in enumerate(snapshots)]
    lines.insert(0, {"timestamp": "t", "type": "session_meta", "payload": {"id": thread_id}})
    (day / f"rollout-2026-10-06T10-00-00-{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")


def test_codex_reads_latest_rate_limit_snapshot_from_session_file(tmp_path: Path) -> None:
    from orq.adapters.codex import read_rate_limits
    write_session_file(tmp_path / "sessions", "thread-1", [
        {"primary": {"used_percent": 16.0, "resets_at": 1791238480}, "rate_limit_reached_type": None},
        {"primary": {"used_percent": 91.0, "resets_at": 1791238480}, "rate_limit_reached_type": None},
    ])
    snapshot = read_rate_limits("thread-1", sessions_root=tmp_path / "sessions")
    assert snapshot["primary"]["used_percent"] == 91.0
    assert read_rate_limits("missing", sessions_root=tmp_path / "sessions") is None
    assert read_rate_limits(None, sessions_root=tmp_path / "sessions") is None
    assert read_rate_limits("thread-1", sessions_root=tmp_path / "nowhere") is None


def test_codex_attaches_snapshot_to_result(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    write_session_file(tmp_path / "codex-home" / "sessions", "thread-1", [{"primary": {"used_percent": 42.0, "resets_at": 1}}])

    result = run(codex(), "review it", tmp_path)

    assert result.ok and result.rate_limit == {"primary": {"used_percent": 42.0, "resets_at": 1}}


def test_codex_rate_limit_failure_is_classified(record, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "rate_limit")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    result = run(codex(), "review it", tmp_path)

    assert not result.ok and result.error_kind == "rate_limit" and "usage limit" in result.error


def test_codex_snapshot_reached_type_marks_rate_limit(record, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "schema_error")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    write_session_file(tmp_path / "codex-home" / "sessions", "thread-1",
                       [{"primary": {"used_percent": 100.0, "resets_at": 1}, "rate_limit_reached_type": "primary"}])

    result = run(codex(), "review it", tmp_path)

    assert not result.ok and result.error_kind == "rate_limit"


def test_classify_codex_error() -> None:
    from orq.adapters.codex import classify_codex_error
    assert classify_codex_error("You've reached your usage limit", None) == "rate_limit"
    assert classify_codex_error("429 Too Many Requests", None) == "rate_limit"
    assert classify_codex_error("Not logged in. Run codex login", None) == "auth"
    assert classify_codex_error("Invalid schema", None) == "error"
    assert classify_codex_error("Invalid schema", {"rate_limit_reached_type": "primary"}) == "rate_limit"


# Phase 3: plan contract, per-call model and effort


def test_validate_plan_accepts_and_rejects() -> None:
    from orq.adapters.schema import PLAN_SCHEMA, validate_plan
    good = {"status": "plan", "summary": "s", "human": None,
            "milestones": [{"title": "t", "goal": "g", "done_when": "d", "difficulty": "hard"}]}
    assert validate_plan(good) == []
    assert any("milestone" in p for p in validate_plan({**good, "milestones": []}))
    assert any("difficulty" in p for p in validate_plan({**good, "milestones": [{"title": "t", "goal": "g", "done_when": "d", "difficulty": "easy"}]}))
    assert any("human" in p for p in validate_plan({**good, "status": "needs_human", "milestones": []}))
    assert validate_plan({"status": "needs_human", "summary": "s", "milestones": [],
                          "human": {"decision_type": "ambiguity", "question": "q", "options": ["a"], "recommendation": 0}}) == []
    assert validate_plan("nope") == ["output is not a JSON object"]

    def strict(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or node.get("type") == ["object", "null"]:
                assert node.get("additionalProperties") is False and set(node["properties"]) == set(node["required"])
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    strict(PLAN_SCHEMA)


def test_implementer_model_override_per_call(record, tmp_path, monkeypatch) -> None:
    scenario(monkeypatch, "ok")
    run(implementer(), "hi", tmp_path, model="sonnet")
    argv = record()["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_codex_effort_and_plan_contract_per_call(record, tmp_path, monkeypatch) -> None:
    from orq.adapters.schema import PLAN_CONTRACT, PLAN_SCHEMA
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "plan")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    result = run(codex(), "plan it", tmp_path, effort="high", contract=PLAN_CONTRACT)

    argv = record()["argv"]
    assert 'model_reasoning_effort="high"' in argv
    assert json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8")) == PLAN_SCHEMA
    assert result.ok and result.structured["milestones"][1]["difficulty"] == "mechanical"


def test_codex_review_contract_rejects_a_plan_shaped_answer(record, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "plan")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    result = run(codex(), "review it", tmp_path)
    assert not result.ok and result.error_kind == "invalid_output"


def test_claude_reviewer_plan_contract(record, tmp_path, monkeypatch) -> None:
    from orq.adapters.schema import PLAN_CONTRACT, PLAN_SCHEMA
    scenario(monkeypatch, "structured")
    reviewer = ClaudeReviewer(model="opus", argv_prefix=[sys.executable, str(FAKES / "fake_claude.py")])
    result = run(reviewer, "plan it", tmp_path, contract=PLAN_CONTRACT, model="sonnet")
    argv = record()["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == PLAN_SCHEMA
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert not result.ok and result.error_kind == "invalid_output"  # the fake answers a review, not a plan
