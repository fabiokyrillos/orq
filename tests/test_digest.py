"""Progress digest (Phase 6): what was done, what is being done, from a run dir."""

import json
from pathlib import Path

from orq.core.checkpoint import Checkpoint
from orq.core.digest import build_digest

TASK = "# Task: Invoice totals\n## Repo\nowner/shop, base branch main\n## Goal\nx\n## Acceptance criteria\n- [ ] y\n## Check command\nz\n"
PLAN = {"summary": "s", "milestones": [{"title": "Totals", "goal": "g", "done_when": "d", "difficulty": "hard"},
                                       {"title": "Discounts", "goal": "g", "done_when": "d", "difficulty": "hard"},
                                       {"title": "Docs", "goal": "g", "done_when": "d", "difficulty": "mechanical"}]}


def ev(minute: int, type_: str, **data) -> dict:
    return {"ts": f"2026-10-07T16:{minute:02d}:00.000+00:00", "type": type_, **data}


def make_run(tmp_path: Path, events: list[dict], milestone_index: int = 1, iteration: int = 3) -> Path:
    run_dir = tmp_path / "RDIG01"
    (run_dir / "iterations" / "1").mkdir(parents=True)
    (run_dir / "TASK.md").write_text(TASK, encoding="utf-8")
    (run_dir / "iterations" / "1" / "diff.patch").write_text("diff --git a/totals.py b/totals.py\ndiff --git a/test_totals.py b/test_totals.py\n",
                                                             encoding="utf-8")
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    Checkpoint(run_id="RDIG01", state="IMPLEMENTING", phase="implement", branch="b", worktree="w", iteration=iteration,
               plan=PLAN, milestone_index=milestone_index).save(run_dir / "state.json")
    return run_dir


EVENTS = [
    ev(0, "state", state="QUEUED"), ev(0, "state", state="PLANNING"),
    ev(1, "state", state="IMPLEMENTING", iteration=1), ev(5, "check", ok=False), ev(6, "state", state="REVIEWING", iteration=1),
    ev(7, "owner_update", iteration=1, milestone=1, text="Totals are computed per line; the rounding test still fails."),
    ev(8, "state", state="IMPLEMENTING", iteration=2), ev(10, "check", ok=True), ev(11, "state", state="REVIEWING", iteration=2),
    ev(12, "owner_update", iteration=2, milestone=1, text="Totals now round half up and every test passes."),
    ev(12, "milestone_done", iteration=2, milestone=1, of=3),
    ev(12, "state", state="AWAITING_HUMAN", decision_id="DQQQQ"), ev(40, "state", state="IMPLEMENTING", iteration=3),
]


def test_digest_lists_done_milestones_and_what_is_happening(tmp_path: Path) -> None:
    text = build_digest(make_run(tmp_path, EVENTS))

    lines = text.splitlines()
    assert lines[0] == "*[orq] RDIG01 · shop · IMPLEMENTING · 12m of work*"  # the 28 minutes waiting for the owner are not work
    assert lines[1] == "Invoice totals"
    assert "Done:" in lines and "- 1/3 Totals: Totals now round half up and every test passes." in lines
    assert "Now: milestone 2/3 Discounts, iteration 3" in lines
    assert "Latest: Totals now round half up and every test passes." in lines
    assert lines[-1] == "Checks 1/2 passed · 2 files changed"


def test_digest_mentions_a_pending_decision_and_is_capped(tmp_path: Path) -> None:
    events = EVENTS[:-1] + [ev(12, "owner_update", iteration=2, milestone=2, text="x " * 2000)]
    run_dir = make_run(tmp_path, events)
    (run_dir / "state.json").write_text((run_dir / "state.json").read_text(encoding="utf-8").replace('"pending_decision": null',
                                        '"pending_decision": {"decision_id": "DQQQQ", "kind": "reviewer", "payload": {}}'), encoding="utf-8")

    text = build_digest(run_dir)

    assert "Waiting for you: DQQQQ" in text and len(text) <= 1500


def test_digest_without_plan_or_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "REMPTY"
    run_dir.mkdir()
    (run_dir / "TASK.md").write_text(TASK, encoding="utf-8")

    assert build_digest(run_dir).splitlines()[1] == "Invoice totals"
