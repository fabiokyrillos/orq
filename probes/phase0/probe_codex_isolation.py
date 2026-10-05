"""Compare Codex context cost with and without --ignore-user-config on the Phase 0 trivial prompt."""

from __future__ import annotations

from _common import P0, SCRATCH, codex_argv, header, run

CODEX = [*codex_argv(), "-m", "gpt-5.5"]
PROMPT = "Without running commands: is 2027 a prime number? Answer yes or no with one sentence."
ISOLATED = ["--ignore-user-config", "-c", 'windows.sandbox="elevated"']

for name, flags in (("x5-user-config", []), ("x5-isolated", ISOLATED)):
    res = run(name, [*CODEX, "exec", "--json", "--sandbox", "read-only", "-C", str(SCRATCH), "--ephemeral", *flags,
                     "-c", 'model_reasoning_effort="low"', PROMPT], P0, timeout=300)
    header(res)
    for event in res.jsonl():
        if event.get("type") == "turn.completed":
            print("  usage:", event.get("usage"))
        if event.get("type") in ("error", "turn.failed"):
            print("  ERROR:", str(event)[:300])
