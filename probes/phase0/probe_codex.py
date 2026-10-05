"""Phase 0 probes for `codex exec`. Usage: python probe_codex.py [step ...]

Codex quota is scarce: every step is one short call at low effort unless stated.
"""

from __future__ import annotations

import json
import sys

from _common import OUT, P0, SCRATCH, Result, codex_argv, git, header, run

# config.toml may name a model the CLI cannot use on a ChatGPT plan, so the model is always explicit.
CODEX = [*codex_argv(), "-m", "gpt-5.5"]
STATE = OUT / "codex-state.json"

_ISSUE = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
        "description": {"type": "string"},
    },
    "required": ["severity", "description"],
    "additionalProperties": False,
}
_HUMAN_PROPS = {
    "decision_type": {"type": "string", "enum": ["business", "ambiguity", "risk", "blocked"]},
    "question": {"type": "string"},
    "options": {"type": "array", "items": {"type": "string"}},
    "recommendation": {"type": "integer"},
}

# Strict variant: every key required, optional values expressed as nullable.
STRICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["continue", "done", "needs_human"]},
        "summary": {"type": "string"},
        "milestone": {"type": "string"},
        "next_prompt": {"type": ["string", "null"]},
        "issues": {"type": "array", "items": _ISSUE},
        "human": {
            "type": ["object", "null"],
            "properties": _HUMAN_PROPS,
            "required": list(_HUMAN_PROPS),
            "additionalProperties": False,
        },
    },
    "required": ["status", "summary", "milestone", "next_prompt", "issues", "human"],
    "additionalProperties": False,
}

# Loose variant: SPEC 8.2 read literally, `human` and `next_prompt` optional.
LOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["continue", "done", "needs_human"]},
        "summary": {"type": "string"},
        "milestone": {"type": "string"},
        "next_prompt": {"type": "string"},
        "issues": {"type": "array", "items": _ISSUE},
        "human": {"type": "object", "properties": _HUMAN_PROPS},
    },
    "required": ["status", "summary", "milestone", "issues"],
}


def schema_file(name: str, schema: dict):
    path = P0 / name
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return path


def summarize(res: Result) -> None:
    events = res.jsonl()
    print("  event types:", [e.get("type") for e in events])
    for event in events:
        kind = event.get("type")
        if kind == "thread.started":
            print("  thread_id:", event.get("thread_id"))
        elif kind in ("error", "turn.failed"):
            print("  ERROR EVENT:", json.dumps(event)[:700])
        elif kind == "turn.completed":
            print("  usage:", event.get("usage"))
        elif kind == "item.completed":
            item = event.get("item", {})
            body = item.get("text") or item.get("command") or ""
            extra = ""
            if item.get("type") == "command_execution":
                extra = " exit=%s out=%r" % (item.get("exit_code"), (item.get("aggregated_output") or "")[:200])
            print("  item %s: %r%s" % (item.get("type"), body[:300], extra))


def thread_id(res: Result) -> str | None:
    for event in res.jsonl():
        if event.get("type") == "thread.started":
            return event.get("thread_id")
    return None


def step_strict() -> None:
    schema = schema_file("schema.strict.json", STRICT_SCHEMA)
    last = OUT / "x1-last.json"
    last.unlink(missing_ok=True)
    (SCRATCH / "codex_probe.txt").unlink(missing_ok=True)
    res = run(
        "x1-strict-readonly",
        [*CODEX, "exec", "--json", "--sandbox", "read-only", "-C", str(SCRATCH), "--output-schema", str(schema),
         "-o", str(last), "-c", 'model_reasoning_effort="low"',
         "You are a code reviewer. Step 1: run `git log --oneline -3` and `git diff HEAD~1 --stat`. "
         "Step 2: try once to create a file named codex_probe.txt containing x (shell redirection is fine) and note "
         "whether it was blocked. Step 3: remember the codeword MANGO-17. Then answer in the required JSON shape; "
         "put the write attempt outcome in summary."],
        P0,
        timeout=420,
    )
    header(res)
    summarize(res)
    print("  codex_probe.txt created:", (SCRATCH / "codex_probe.txt").exists())
    print("  git status:", repr(git("status", "--porcelain", cwd=SCRATCH).stdout))
    if last.exists():
        print("  last message:", last.read_text(encoding="utf-8")[:900])
    STATE.write_text(json.dumps({"thread_id": thread_id(res)}), encoding="utf-8")


def step_loose() -> None:
    schema = schema_file("schema.loose.json", LOOSE_SCHEMA)
    res = run(
        "x2-loose-schema",
        [*CODEX, "exec", "--json", "--sandbox", "read-only", "-C", str(SCRATCH), "--output-schema", str(schema),
         "-c", 'model_reasoning_effort="low"', "Reply with status done and a one word summary. Run no commands."],
        P0,
        timeout=240,
    )
    header(res)
    summarize(res)


def step_resume() -> None:
    tid = json.loads(STATE.read_text(encoding="utf-8"))["thread_id"]
    schema = P0 / "schema.strict.json"
    res = run(
        "x3-resume",
        [*CODEX, "exec", "resume", tid, "--json", "--output-schema", str(schema),  # resume has no --sandbox flag
         "-c", 'sandbox_mode="read-only"', "-c", 'model_reasoning_effort="low"',
         "What was the codeword? Put it in summary. Also try once to create codex_probe2.txt and say in summary "
         "whether it was blocked."],
        SCRATCH,
        timeout=420,
    )
    header(res)
    summarize(res)
    print("  resumed same thread:", thread_id(res) == tid, thread_id(res))
    print("  codex_probe2.txt created:", (SCRATCH / "codex_probe2.txt").exists())


def step_effort() -> None:
    for effort in ("low", "high"):
        res = run(
            f"x4-effort-{effort}",
            [*CODEX, "exec", "--json", "--sandbox", "read-only", "-C", str(SCRATCH), "--ephemeral",
             "-c", f'model_reasoning_effort="{effort}"',
             "Without running commands: is 2027 a prime number? Answer yes or no with one sentence."],
            P0,
            timeout=300,
        )
        header(res)
        summarize(res)
    res = run(
        "x4-effort-invalid",
        [*CODEX, "exec", "--json", "--sandbox", "read-only", "-C", str(SCRATCH), "--ephemeral",
         "-c", 'model_reasoning_effort="bogus"', "Say OK."],
        P0,
        timeout=120,
    )
    header(res)
    summarize(res)
    print("  stdout head:", res.stdout[:300])


STEPS = {"strict": step_strict, "loose": step_loose, "resume": step_resume, "effort": step_effort}

if __name__ == "__main__":
    for step in sys.argv[1:] or list(STEPS):
        STEPS[step]()
