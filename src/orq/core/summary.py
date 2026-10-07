"""Run summary and replay model (Phase 5). Pure readers over a run dir: no LLM, no store, no network.

The summary is cached in `summary.json` when a run ends; the replay groups the event log and the recorded artifacts by
step (the plan, then one step per implementer turn, the merge gate belonging to the last one).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from orq.core.checkpoint import Checkpoint

TERMINAL = {"DONE", "FAILED", "ABORTED"}
ROLES = ("planner", "implementer", "reviewer")
PLAN_ARTIFACTS = ("TASK.md", "planner.prompt.md", "planner.stream.jsonl", "PLAN.md", "DECISIONS.md")
ITERATION_ARTIFACTS = ("implementer.prompt.md", "implementer.stream.jsonl", "diff.patch", "checks.txt", "reviewer.prompt.md",
                       "reviewer.stream.jsonl", "reviewer.output.json", "final_review.prompt.md", "final_review.stream.jsonl",
                       "final_review.output.json")
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_TITLE = re.compile(r"^#\s*Task:\s*(.+\S)\s*$", re.MULTILINE)
_REPO = re.compile(r"^##\s*Repo\s*\n\s*([\w.-]+/[\w.-]+)", re.MULTILINE)


def read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a partial last line while the run is writing
    return events


def _ts(event: dict) -> float:
    return datetime.fromisoformat(event["ts"]).timestamp()


def _seconds(a: dict, b: dict) -> int:
    return round(_ts(b) - _ts(a))


def _input_tokens(usage: dict) -> int:
    # Codex: cached tokens are part of input_tokens. Claude: cache creation and reads come on top of input_tokens.
    return int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) + \
        int(usage.get("cache_read_input_tokens") or 0)


def build_summary(run_dir: Path) -> dict:
    events = read_events(run_dir)
    task = (run_dir / "TASK.md").read_text(encoding="utf-8") if (run_dir / "TASK.md").exists() else ""
    title, repo = _TITLE.search(task), _REPO.search(task)
    cp = Checkpoint.try_load(run_dir / "state.json")
    # A queued run has no process until the dispatcher starts it; its `queued` event opens the QUEUED time.
    states = [e if e["type"] == "state" else {**e, "state": "QUEUED"} for e in events if e["type"] in ("state", "queued")]
    states = [s for i, s in enumerate(states) if i == 0 or s["state"] != states[i - 1]["state"]]
    final = states[-1] if states else None
    ended = final if final and final["state"] in TERMINAL else None

    time_by_state: dict[str, int] = {}
    for current, nxt in zip(states, states[1:] + ([] if ended or not events else [events[-1]])):
        time_by_state[current["state"]] = time_by_state.get(current["state"], 0) + _seconds(current, nxt)

    decisions: dict[str, dict] = {}
    raised_at: dict[str, dict] = {}
    for e in events:
        if e["type"] == "decision":
            raised_at[e["decision_id"]] = e
            decisions[e["decision_id"]] = {
                "decision_id": e["decision_id"], "source": e.get("source"), "decision_type": e.get("decision_type"),
                "kind": e.get("kind"), "question": (str(e.get("question", "")).splitlines() or [""])[0][:200],
                "answer": None, "via": None, "waited_seconds": None}
        elif e["type"] == "answer" and e.get("decision_id") in decisions:
            decisions[e["decision_id"]].update(answer=e.get("answer"), via=e.get("via"),
                                               waited_seconds=_seconds(raised_at[e["decision_id"]], e))

    agents = {role: {"calls": 0, "failed": 0, "seconds": 0, "input_tokens": 0, "output_tokens": 0} for role in ROLES}
    for previous, e in zip(events, events[1:]):
        if e["type"] in ROLES:
            stats, usage = agents[e["type"]], e.get("usage") or {}
            stats["calls"] += 1
            stats["failed"] += 0 if e.get("ok") else 1
            stats["seconds"] += _seconds(previous, e)  # the event before a call is its state change or model choice
            stats["input_tokens"] += _input_tokens(usage)
            stats["output_tokens"] += int(usage.get("output_tokens") or 0)

    plans = [e for e in events if e["type"] == "plan"]
    planned = len(plans[-1].get("milestones") or []) if plans else 0
    done_milestones = sum(1 for e in events if e["type"] == "milestone_done") + (1 if ended and ended["state"] == "DONE" else 0)
    checks = [e for e in events if e["type"] == "check"]
    ci = [e for e in events if e["type"] == "ci"]
    pr_events = [e for e in events if e["type"] == "pr"]
    iterations = max([int(e.get("iteration") or 0) for e in events] + [cp.iteration if cp else 0])

    return {
        "run_id": run_dir.name,
        "title": title.group(1) if title else None,
        "repo": repo.group(1) if repo else None,
        "state": final["state"] if final else None,
        "started_at": events[0]["ts"] if events else None,
        "ended_at": ended["ts"] if ended else None,
        "wall_seconds": _seconds(events[0], ended or events[-1]) if events else 0,
        "time_by_state": time_by_state,
        "iterations": iterations,
        "milestones": {"planned": planned, "done": min(done_milestones, planned) if planned else done_milestones},
        "pr": {"url": (cp.pr_url if cp and cp.pr_url else (pr_events[-1].get("url") if pr_events else None)),
               "merged": any(e["type"] == "pr_merged" for e in events)},
        "decisions": list(decisions.values()),
        "agents": agents,
        "implementer_models": [e.get("model") for e in events if e["type"] == "implementer_model"],
        "reviewer_switches": sum(1 for e in events if e["type"] == "reviewer_switched"),
        "checks": {"runs": len(checks), "failed": sum(1 for e in checks if not e.get("ok"))},
        "ci": {"state": ci[-1].get("state"), "waited_seconds": sum(int(e.get("waited_seconds") or 0) for e in ci),
               "reruns": sum(int(e.get("reruns") or 0) for e in ci)} if ci else None,
        "guard": {"pre_denials": sum(1 for d in decisions.values() if d["kind"] == "guard_pre"),
                  "diff_rule_hits": sum(1 for e in events if e["type"] == "diff_rules")},
        "files_changed": _files_changed(run_dir),
    }


def _files_changed(run_dir: Path) -> list[str]:
    files: set[str] = set()
    root = run_dir / "iterations"
    if root.exists():
        for patch in root.glob("*/diff.patch"):
            if patch.parent.name.isdigit():  # discarded iterations do not count
                for match in _DIFF_HEADER.finditer(patch.read_text(encoding="utf-8", errors="replace")):
                    files.add(match.group(2))
    return sorted(files)


def write_summary(run_dir: Path) -> dict:
    summary = build_summary(run_dir)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def load_summary(run_dir: Path) -> dict:
    """The cached summary of a finished run, else a fresh one."""
    path = run_dir / "summary.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return build_summary(run_dir)


def _artifacts(run_dir: Path, rel_dir: str, names: tuple[str, ...]) -> list[str]:
    base = run_dir / rel_dir if rel_dir else run_dir
    return [f"{rel_dir}/{name}" if rel_dir else name for name in names if (base / name).exists()]


def build_replay(run_dir: Path) -> dict:
    """Steps in order: the plan, then one per implementer turn. A rolled-back iteration number appears again later;
    its earlier turns map to the archived `n.discarded-<ts>` directories, oldest first."""
    events = read_events(run_dir)
    starts = [i for i, e in enumerate(events) if e["type"] == "state" and e.get("state") == "IMPLEMENTING" and e.get("iteration")]
    root = run_dir / "iterations"
    discarded: dict[int, list[str]] = {}
    if root.exists():
        for path in sorted(root.iterdir()):
            head, _, _ = path.name.partition(".discarded-")
            if path.is_dir() and head.isdigit() and path.name != head:
                discarded.setdefault(int(head), []).append(f"iterations/{path.name}")

    occurrences: dict[int, int] = {}
    for i in starts:
        n = int(events[i]["iteration"])
        occurrences[n] = occurrences.get(n, 0) + 1
    seen: dict[int, int] = {}
    steps = [{"label": "Plan", "iteration": 0, "dir": "", "discarded": False,
              "events": events[: starts[0] if starts else len(events)], "artifacts": _artifacts(run_dir, "", PLAN_ARTIFACTS)}]
    for k, i in enumerate(starts):
        n = int(events[i]["iteration"])
        seen[n] = seen.get(n, 0) + 1
        end = starts[k + 1] if k + 1 < len(starts) else len(events)
        archived = discarded.get(n, [])
        if seen[n] < occurrences[n] and archived:  # rolled back later: this turn's files were archived
            rel = archived.pop(0)
            steps.append({"label": f"Iteration {n} (discarded)", "iteration": n, "dir": rel, "discarded": True,
                          "events": events[i:end], "artifacts": _artifacts(run_dir, rel, ITERATION_ARTIFACTS)})
            continue
        previous = steps[-1]
        if previous["iteration"] == n and not previous["discarded"]:
            previous["events"] = previous["events"] + events[i:end]  # the same iteration resumed after a crash or pause
            continue
        rel = f"iterations/{n}"
        steps.append({"label": f"Iteration {n}", "iteration": n, "dir": rel, "discarded": False, "events": events[i:end],
                      "artifacts": _artifacts(run_dir, rel, ITERATION_ARTIFACTS)})
    leftovers = [{"dir": rel, "artifacts": _artifacts(run_dir, rel, ITERATION_ARTIFACTS)} for rels in discarded.values() for rel in rels]
    return {"run_id": run_dir.name, "steps": steps, "discarded": leftovers}
