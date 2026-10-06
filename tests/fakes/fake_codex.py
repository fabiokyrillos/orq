"""Stand-in for `codex` in unit tests. Emits canned --json events shaped like Codex CLI 0.139.0.

Scenario comes from FAKE_SCENARIO; argv, stdin and env are recorded to FAKE_RECORD as JSON.
"""

import json
import os
import sys

REVIEW = {"status": "continue", "summary": "Looks fine so far", "milestone": "m1",
          "next_prompt": "Add the tests", "issues": [{"severity": "minor", "description": "nit"}], "human": None}


def message(text: str, item_id: str) -> dict:
    return {"type": "item.completed", "item": {"id": item_id, "type": "agent_message", "text": text}}


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    record = {"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
              "env_claude_keys": sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC")))}
    with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
        json.dump(record, handle)

    scenario = os.environ.get("FAKE_CODEX_SCENARIO") or os.environ.get("FAKE_SCENARIO", "ok")
    if scenario == "done":
        REVIEW.update(status="done", next_prompt=None)
        scenario = "ok"
    sys.stderr.write("Reading additional input from stdin...\n")
    lines: list[dict] = [{"type": "thread.started", "thread_id": "thread-1"}, {"type": "turn.started"}]
    code = 0
    if scenario == "ok":
        lines += [message(json.dumps({**REVIEW, "status": "continue", "summary": "progress note"}), "item_0"),
                  message(json.dumps(REVIEW), "item_2"),
                  {"type": "turn.completed", "usage": {"input_tokens": 86381, "cached_input_tokens": 58496, "output_tokens": 494, "reasoning_output_tokens": 74}}]
        if "-o" in argv:
            with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as handle:
                handle.write(json.dumps(REVIEW))
    elif scenario == "schema_error":
        err = {"type": "error", "error": {"type": "invalid_request_error", "code": "invalid_json_schema", "message": "Invalid schema"}, "status": 400}
        lines += [{"type": "error", "message": json.dumps(err)}, {"type": "turn.failed", "error": {"message": json.dumps(err)}}]
        code = 1
    elif scenario == "rate_limit":
        # Shape from Phase 0 section 5: a generic error event, then turn.failed, exit 1. Message text is still unverified.
        msg = "You've reached your usage limit. Try again at 10pm."
        lines += [{"type": "error", "message": msg}, {"type": "turn.failed", "error": {"message": msg}}]
        code = 1
    elif scenario == "bad_output":
        lines += [message("I forgot the schema", "item_0"), {"type": "turn.completed", "usage": {}}]
    for line in lines:
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
