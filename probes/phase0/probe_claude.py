"""Phase 0 probes for `claude -p` headless behaviour. Usage: python probe_claude.py [step ...]"""

from __future__ import annotations

import json
import re
import sys
import uuid

from _common import OUT, P0, SCRATCH, Result, claude_argv, header, run

CLAUDE = claude_argv()
STATE = OUT / "claude-state.json"


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(**kv) -> None:
    state = load_state()
    state.update(kv)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def result_event(res: Result) -> dict:
    for event in reversed(res.jsonl()):
        if event.get("type") == "result":
            return event
    return {}


def init_event(res: Result) -> dict:
    for event in res.jsonl():
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event
    return {}


def show_result(event: dict) -> None:
    usage = event.get("usage") or {}
    print("  result:", repr((event.get("result") or "")[:300]))
    print(
        "  subtype=%s is_error=%s session_id=%s turns=%s"
        % (event.get("subtype"), event.get("is_error"), event.get("session_id"), event.get("num_turns"))
    )
    print(
        "  tokens in=%s cache_create=%s cache_read=%s out=%s  models=%s"
        % (
            usage.get("input_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("output_tokens"),
            list((event.get("modelUsage") or {}).keys()),
        )
    )
    if event.get("permission_denials"):
        print("  permission_denials:", json.dumps(event["permission_denials"])[:600])


def step_json() -> None:
    res = run("c1-json", [*CLAUDE, "-p", "Reply with exactly: OK", "--output-format", "json", "--model", "sonnet"], SCRATCH)
    header(res)
    data = res.json()
    print("  top-level type:", type(data).__name__)
    event = data if isinstance(data, dict) else data[-1]
    print("  keys:", sorted(event.keys()))
    show_result(event)


def step_stdin() -> None:
    prompt = 'Line one has "quotes", 100% and a caret ^ & ampersand.\nLine two: reply with exactly the word RECEIVED.'
    res = run("c1b-stdin", [*CLAUDE, "-p", "--output-format", "json", "--model", "sonnet"], SCRATCH, stdin_text=prompt)
    header(res)
    show_result(res.json())


def step_stream() -> None:
    res = run(
        "c2-stream",
        [*CLAUDE, "-p", "Remember the codeword: PINEAPPLE-42. Reply with exactly: STORED",
         "--output-format", "stream-json", "--verbose", "--model", "opus"],
        SCRATCH,
    )
    header(res)
    events = res.jsonl()
    print("  lines=%d parsed=%d" % (len([l for l in res.stdout.splitlines() if l.strip()]), len(events)))
    print("  sequence:", [(e.get("type"), e.get("subtype")) for e in events])
    init = init_event(res)
    print("  init keys:", sorted(init.keys()))
    print("  init model=%s permissionMode=%s tools=%d" % (init.get("model"), init.get("permissionMode"), len(init.get("tools", []))))
    show_result(result_event(res))
    save_state(sid=init.get("session_id"))


def step_resume() -> None:
    sid = load_state()["sid"]
    res = run(
        "c3-resume-model-switch",
        [*CLAUDE, "-p", "What was the codeword? Reply with only the codeword.", "--resume", sid,
         "--output-format", "stream-json", "--verbose", "--model", "sonnet"],
        SCRATCH,
    )
    header(res)
    init = init_event(res)
    print("  original sid=%s\n  init sid    =%s  init model=%s" % (sid, init.get("session_id"), init.get("model")))
    show_result(result_event(res))


def step_resume_other_cwd() -> None:
    sid = load_state()["sid"]
    res = run(
        "c4-resume-other-cwd",
        [*CLAUDE, "-p", "What was the codeword? Reply with only the codeword.", "--resume", sid,
         "--output-format", "json", "--model", "sonnet"],
        P0,
    )
    header(res)
    print("  stdout:", res.stdout.strip()[:400])


def step_session_id() -> None:
    wanted = str(uuid.uuid4())
    res = run(
        "c5-session-id",
        [*CLAUDE, "-p", "Reply with exactly: OK", "--session-id", wanted, "--output-format", "json", "--model", "sonnet"],
        SCRATCH,
    )
    header(res)
    got = res.json().get("session_id")
    print("  wanted=%s got=%s match=%s" % (wanted, got, wanted == got))


def step_append() -> None:
    res = run(
        "c6a-append",
        [*CLAUDE, "-p", "Say hi in three words.", "--append-system-prompt",
         "End every reply with the exact token ORQ-MARK-7.", "--output-format", "json", "--model", "sonnet"],
        SCRATCH,
    )
    header(res)
    first = res.json()
    show_result(first)
    res = run(
        "c6b-append-resume-without-flag",
        [*CLAUDE, "-p", "Say bye in three words.", "--resume", first["session_id"], "--output-format", "json", "--model", "sonnet"],
        SCRATCH,
    )
    header(res)
    show_result(res.json())


DECISION_RULE = (
    "Standing rule: on a business decision or real ambiguity, do not guess and do not edit files. "
    "Stop and end your final message with a fenced block tagged orq-decision containing one JSON object "
    'with keys decision_type ("business" | "ambiguity" | "risk" | "blocked"), question, options (array of strings), '
    "recommendation (index into options). Nothing may follow the block."
)


def step_decision() -> None:
    res = run(
        "c7-decision-marker",
        [*CLAUDE, "-p",
         "Add a 10% discount to price_with_tax in app.py. Nobody has said whether the discount applies before or after tax.",
         "--append-system-prompt", DECISION_RULE, "--permission-mode", "acceptEdits",
         "--output-format", "json", "--model", "sonnet"],
        SCRATCH,
    )
    header(res)
    event = res.json()
    text = event.get("result") or ""
    print("  tail:", repr(text[-500:]))
    match = re.search(r"```orq-decision\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    print("  marker at end:", bool(match))
    if match:
        print("  parsed:", json.loads(match.group(1)))


def step_permissions() -> None:
    variants = {
        "default": [],
        "acceptEdits": ["--permission-mode", "acceptEdits", "--allowedTools", "Bash(python *)"],
        "dontAsk": ["--permission-mode", "dontAsk"],
        "bypass": ["--dangerously-skip-permissions"],
    }
    for name, flags in variants.items():
        target = SCRATCH / f"perm_{name}.txt"
        res = run(
            f"c8-perm-{name}",
            [*CLAUDE, "-p",
             f"Create a file named perm_{name}.txt containing the letter x using the Write tool, then run the shell "
             "command `python --version`. Report briefly what happened. Do not try alternatives if something is denied.",
             *flags, "--output-format", "json", "--model", "sonnet"],
            SCRATCH,
            timeout=240,
        )
        header(res)
        if res.stdout.strip().startswith("{"):
            show_result(res.json())
        print("  file created:", target.exists())
        target.unlink(missing_ok=True)


def step_isolation() -> None:
    variants = {
        "baseline": [],
        "sources-project-local": ["--setting-sources", "project,local", "--strict-mcp-config"],
        "safe-mode": ["--safe-mode"],
    }
    for name, flags in variants.items():
        res = run(
            f"c9-iso-{name}",
            [*CLAUDE, "-p", "Reply with exactly: OK", *flags, "--output-format", "stream-json", "--verbose",
             "--include-hook-events", "--model", "sonnet"],
            SCRATCH,
        )
        header(res)
        events = res.jsonl()
        init = init_event(res)
        hooks = [e for e in events if e.get("type") == "system" and "hook" in str(e.get("subtype"))]
        print("  tools=%d mcp=%s" % (len(init.get("tools", [])), [m.get("name") for m in init.get("mcp_servers", [])]))
        print("  plugins=%s" % [p.get("name") if isinstance(p, dict) else p for p in init.get("plugins", [])])
        print("  slash_commands=%d skills=%d agents=%d" % (
            len(init.get("slash_commands", [])), len(init.get("skills", [])), len(init.get("agents", []))))
        print("  hook events=%d names=%s" % (len(hooks), sorted({str(h.get("hook_name")) for h in hooks})))
        show_result(result_event(res))


STEPS = {
    "json": step_json,
    "stdin": step_stdin,
    "stream": step_stream,
    "resume": step_resume,
    "resume_other_cwd": step_resume_other_cwd,
    "session_id": step_session_id,
    "append": step_append,
    "decision": step_decision,
    "permissions": step_permissions,
    "isolation": step_isolation,
}

if __name__ == "__main__":
    for step in sys.argv[1:] or list(STEPS):
        STEPS[step]()
