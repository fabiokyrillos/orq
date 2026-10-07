"""Progress digest (Phase 6): what a run has done and what it is doing, for WhatsApp and the dashboard.

Pure over the run dir. The prose comes from the reviewer's `owner_update` field (one per review, no extra agent call);
orq adds the structure and the numbers. Capped at 1500 characters, oldest milestones trimmed first.
"""

from __future__ import annotations

import re
from pathlib import Path

from orq.core.checkpoint import Checkpoint
from orq.core.summary import _files_changed, build_summary, read_events

CAP = 1500
IDLE_STATES = {"QUEUED", "AWAITING_HUMAN", "AWAITING_PLAN_APPROVAL", "PAUSED"}  # time that is not the run working
_TITLE = re.compile(r"^#\s*Task:\s*(.+\S)\s*$", re.MULTILINE)
_REPO = re.compile(r"^##\s*Repo\s*\n\s*([\w.-]+/[\w.-]+)", re.MULTILINE)
_PHASE_WORDS = {"plan": "planning", "implement": "implementing", "verify": "running the check", "review": "in review",
                "await": "waiting for you", "finalize": "opening the PR", "gate_ci": "waiting for CI", "gate_review": "final review",
                "gate_merge": "merging"}


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def build_digest(run_dir: Path, *, repo: str | None = None) -> str:
    """`repo` is the fallback when TASK.md cannot be read."""
    events = read_events(run_dir)
    task = (run_dir / "TASK.md").read_text(encoding="utf-8") if (run_dir / "TASK.md").exists() else ""
    title, found = _TITLE.search(task), _REPO.search(task)
    cp = Checkpoint.try_load(run_dir / "state.json")
    summary = build_summary(run_dir)
    work = sum(seconds for state, seconds in summary["time_by_state"].items() if state not in IDLE_STATES)
    state = summary["state"] or (cp.state if cp else "QUEUED")
    header = f"*[orq] {run_dir.name} · {(found.group(1) if found else (repo or '?')).split('/')[-1]} · {state} · {_minutes(work)} of work*"

    milestones = ((cp.plan or {}).get("milestones") or []) if cp else []
    updates = [e for e in events if e["type"] == "owner_update" and str(e.get("text") or "").strip()]
    by_iteration = {e.get("iteration"): str(e["text"]).strip() for e in updates}
    done_lines = []
    for e in events:
        if e["type"] == "milestone_done":
            index = int(e.get("milestone") or 0)
            name = milestones[index - 1]["title"] if 0 < index <= len(milestones) else f"milestone {index}"
            text = _clip(by_iteration.get(e.get("iteration"), "done"), 240)
            done_lines.append(f"- {index}/{e.get('of') or len(milestones)} {name}: {text}")

    now = []
    if cp and state not in ("DONE", "FAILED", "ABORTED"):
        where = _PHASE_WORDS.get(cp.phase, cp.phase)
        if milestones:
            index = min(cp.milestone_index, len(milestones) - 1)
            now.append(f"Now: milestone {index + 1}/{len(milestones)} {milestones[index]['title']}, iteration {cp.iteration}" +
                       (f", {where}" if cp.phase not in ("implement",) else ""))
        else:
            now.append(f"Now: iteration {cp.iteration}, {where}")
        if cp.pending_decision:
            now.append(f"Waiting for you: {cp.pending_decision['decision_id']}")
    latest = [f"Latest: {_clip(updates[-1]['text'], 500)}"] if updates else []
    checks = summary["checks"]
    numbers = f"Checks {checks['runs'] - checks['failed']}/{checks['runs']} passed · {len(_files_changed(run_dir))} files changed"

    def render(done: list[str], latest_lines: list[str]) -> str:
        parts = [header, title.group(1) if title else run_dir.name]
        if done:
            parts += ["Done:", *done]
        return "\n".join(parts + now + latest_lines + [numbers])

    text = render(done_lines, latest)
    while len(text) > CAP and len(done_lines) > 1:
        done_lines = ["- (earlier milestones omitted)", *done_lines[2:]] if not done_lines[0].startswith("- (") else \
            [done_lines[0], *done_lines[2:]]
        text = render(done_lines, latest)
    if len(text) > CAP and latest:
        room = CAP - (len(text) - len(latest[0]))
        latest = [latest[0][: max(0, room - 3)].rstrip() + "..."]
        text = render(done_lines, latest)
    return text[:CAP]
