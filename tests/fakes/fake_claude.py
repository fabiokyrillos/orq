"""Stand-in for claude.exe in unit tests. Emits canned stream-json shaped like Claude Code 2.1.219.

Scenario comes from FAKE_SCENARIO; argv, stdin and env are recorded to FAKE_RECORD as JSON.
"""

import json
import os
import sys

RATE_LIMIT = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1791239400, "rateLimitType": "five_hour"}}
USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 5}


def result(text: str, *, is_error: bool = False, session_id: str = "sid-1", extra: dict | None = None) -> dict:
    data = {
        "type": "result", "subtype": "success", "is_error": is_error, "result": text, "session_id": session_id,
        "num_turns": 1, "usage": USAGE, "modelUsage": {"claude-sonnet-5": {}}, "permission_denials": [],
        "terminal_reason": "api_error" if is_error else "completed", "total_cost_usd": 0.01,
    }
    data.update(extra or {})
    return data


def init(session_id: str = "sid-1") -> dict:
    return {"type": "system", "subtype": "init", "session_id": session_id, "model": "claude-sonnet-5", "tools": ["Read"], "cwd": os.getcwd()}


def _read_pid_file() -> str:
    run_dir = os.environ.get("ORQ_RUN_DIR")
    path = os.path.join(run_dir, "child.pid") if run_dir else ""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return ""


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    session_id = "sid-1"
    if "--session-id" in argv:
        session_id = argv[argv.index("--session-id") + 1]
    if "--resume" in argv:
        session_id = argv[argv.index("--resume") + 1]
    record = {"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
              "env_claude_keys": sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC"))),
              "orq_run_dir": os.environ.get("ORQ_RUN_DIR"),
              "child_pid": _read_pid_file()}
    with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
        json.dump(record, handle)

    scenario = os.environ.get("FAKE_SCENARIO", "ok")
    lines: list[dict] = []
    code = 0
    if scenario == "ok":
        lines = [init(session_id), {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
                 RATE_LIMIT, result("Implemented the thing.é", session_id=session_id)]
    elif scenario == "decision":
        text = ("I need a decision.\n\n```orq-decision\n"
                '{"decision_type": "business", "question": "Before or after tax?", "options": ["Before", "After"], "recommendation": 0}'
                "\n```")
        lines = [init(session_id), result(text, session_id=session_id)]
    elif scenario == "rate_limit":
        lines = [init(session_id), result("You've hit your session limit · resets 10pm (America/Cayenne)", is_error=True, session_id=session_id)]
        code = 1
    elif scenario == "auth":
        lines = [init(session_id), result("Failed to authenticate: OAuth session expired and could not be refreshed", is_error=True, session_id=session_id)]
        code = 1
    elif scenario == "denied":
        lines = [init(session_id), result("The Write tool call was denied.", session_id=session_id, extra={
            "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git reset --hard"}}]})]
    elif scenario == "structured":
        structured = {"status": "done", "summary": "fine", "milestone": "m1", "next_prompt": None, "issues": [], "human": None}
        lines = [init(session_id), result(json.dumps(structured), session_id=session_id, extra={"structured_output": structured})]
    elif scenario == "crash":
        sys.stdout.write("not json at all\n")
        sys.stderr.write("No conversation found with session ID: x\n")
        return 1
    for line in lines:
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
