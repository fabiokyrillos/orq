"""Run summary and replay model (Phase 5): pure readers over a run dir."""

import json
from pathlib import Path

from orq.core.checkpoint import Checkpoint
from orq.core.summary import build_replay, build_summary, write_summary

TASK = "# Task: Remove probe\n## Repo\nowner/sandbox, base branch main\n## Goal\nx\n## Acceptance criteria\n- [ ] y\n## Check command\nz\n"


def ev(ts: str, type_: str, **data) -> dict:
    return {"ts": f"2026-10-07T16:{ts}+00:00", "type": type_, **data}


EVENTS = [
    ev("00:00.000", "state", state="QUEUED"),
    ev("00:10.000", "slot_acquired", phase="setup"),
    ev("00:10.000", "state", state="PLANNING"),
    ev("00:40.000", "planner", ok=True, usage={"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 50}),
    ev("00:40.000", "plan", milestones=[["Add changelog", "mechanical"], ["Remove probe", "mechanical"]], summary="s"),
    ev("00:40.000", "state", state="IMPLEMENTING", iteration=1),
    ev("00:40.000", "implementer_model", iteration=1, model="sonnet", milestone=1),
    ev("01:00.000", "implementer", ok=True, usage={"input_tokens": 10, "cache_creation_input_tokens": 90, "cache_read_input_tokens": 900, "output_tokens": 100}),
    ev("01:00.000", "state", state="VERIFYING", iteration=1),
    ev("01:01.000", "check", iteration=1, ok=False, exit_code=1, signature="x"),
    ev("01:01.000", "commit", iteration=1, sha="a1"),
    ev("01:01.000", "state", state="REVIEWING", iteration=1),
    ev("01:21.000", "reviewer", ok=True, usage={"input_tokens": 2000, "output_tokens": 80}),
    ev("01:21.000", "decision", decision_id="DAAAA", source="reviewer", decision_type="blocked", question="Approve deletion?\nmore", options=["yes", "no"], kind="reviewer"),
    ev("01:21.000", "state", state="AWAITING_HUMAN", decision_id="DAAAA"),
    ev("01:21.000", "slot_released", phase="await"),
    ev("03:21.000", "answer", decision_id="DAAAA", answer="yes", via="whatsapp"),
    ev("03:24.000", "answer_applied", decision_id="DAAAA", answer="yes", via="whatsapp"),
    ev("03:24.000", "slot_acquired", phase="implement"),
    ev("03:24.000", "state", state="IMPLEMENTING", iteration=2),
    ev("03:24.000", "implementer_model", iteration=2, model="opus", milestone=2),
    ev("03:44.000", "implementer", ok=True, usage={"input_tokens": 5, "output_tokens": 20}),
    ev("03:44.000", "state", state="VERIFYING", iteration=2),
    ev("03:44.500", "diff_rules", iteration=2, violations=["[deleted_file] probe.txt"]),
    ev("03:45.000", "check", iteration=2, ok=True, exit_code=0, signature=None),
    ev("03:45.000", "state", state="REVIEWING", iteration=2),
    ev("04:05.000", "reviewer", ok=True, usage={"input_tokens": 1500, "output_tokens": 60}),
    ev("04:05.000", "reviewer_switched", reviewer="router", until="x"),
    ev("04:05.000", "milestone_done", iteration=2, milestone=1, of=2),
    ev("04:05.000", "state", state="FINALIZING", step="push"),
    ev("04:06.000", "pr", url="https://github.com/owner/sandbox/pull/6", number=6),
    ev("04:30.000", "ci", state="success", reruns=1, waited_seconds=24, checks=[]),
    ev("04:50.000", "final_review", status="done", summary="ok"),
    ev("04:58.000", "pr_merged", number=6, url="https://github.com/owner/sandbox/pull/6", strategy="squash"),
    ev("05:00.000", "state", state="DONE"),
]


def make_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "RTEST1"
    (run_dir / "iterations" / "1").mkdir(parents=True)
    (run_dir / "iterations" / "2").mkdir()
    (run_dir / "iterations" / "1.discarded-20261007T160300").mkdir()
    (run_dir / "TASK.md").write_text(TASK, encoding="utf-8")
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n", encoding="utf-8")
    (run_dir / "iterations" / "1" / "diff.patch").write_text("diff --git a/CHANGELOG.md b/CHANGELOG.md\n+x\n", encoding="utf-8")
    (run_dir / "iterations" / "1" / "implementer.prompt.md").write_text("do it", encoding="utf-8")
    (run_dir / "iterations" / "2" / "diff.patch").write_text(
        "diff --git a/probe.txt b/probe.txt\ndeleted file mode 100644\ndiff --git a/CHANGELOG.md b/CHANGELOG.md\n", encoding="utf-8")
    (run_dir / "iterations" / "2" / "reviewer.output.json").write_text("{}", encoding="utf-8")
    (run_dir / "iterations" / "1.discarded-20261007T160300" / "diff.patch").write_text("diff --git a/junk b/junk\n", encoding="utf-8")
    (run_dir / "planner.prompt.md").write_text("plan it", encoding="utf-8")
    Checkpoint(run_id="RTEST1", state="DONE", phase="done", branch="orq/x", worktree="w", iteration=2,
               pr_url="https://github.com/owner/sandbox/pull/6").save(run_dir / "state.json")
    return run_dir


def test_summary_from_events(tmp_path: Path) -> None:
    s = build_summary(make_run(tmp_path))

    assert s["run_id"] == "RTEST1" and s["title"] == "Remove probe" and s["repo"] == "owner/sandbox"
    assert s["state"] == "DONE" and s["iterations"] == 2 and s["wall_seconds"] == 300
    assert s["time_by_state"]["AWAITING_HUMAN"] == 123 and s["time_by_state"]["QUEUED"] == 10
    assert s["milestones"] == {"planned": 2, "done": 2}
    assert s["pr"] == {"url": "https://github.com/owner/sandbox/pull/6", "merged": True}
    assert s["decisions"] == [{"decision_id": "DAAAA", "source": "reviewer", "decision_type": "blocked", "kind": "reviewer",
                               "question": "Approve deletion?", "answer": "yes", "via": "whatsapp", "waited_seconds": 120}]
    # Codex counts cached tokens inside input_tokens; Claude reports cache creation and reads apart from them.
    assert s["agents"]["planner"] == {"calls": 1, "failed": 0, "seconds": 30, "input_tokens": 1000, "output_tokens": 50}
    assert s["agents"]["implementer"] == {"calls": 2, "failed": 0, "seconds": 40, "input_tokens": 1005, "output_tokens": 120}
    assert s["agents"]["reviewer"]["calls"] == 2 and s["agents"]["reviewer"]["seconds"] == 40
    assert s["implementer_models"] == ["sonnet", "opus"] and s["reviewer_switches"] == 1
    assert s["checks"] == {"runs": 2, "failed": 1}
    assert s["ci"] == {"state": "success", "waited_seconds": 24, "reruns": 1}
    assert s["guard"] == {"pre_denials": 0, "diff_rule_hits": 1}
    assert s["files_changed"] == ["CHANGELOG.md", "probe.txt"]  # the discarded iteration does not count


def test_summary_of_a_run_still_in_progress_and_of_an_empty_dir(tmp_path: Path) -> None:
    run_dir = make_run(tmp_path)
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[:16]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    s = build_summary(run_dir)

    assert s["ended_at"] is None and s["decisions"][0]["answer"] is None
    empty = build_summary(tmp_path / "missing")
    assert empty["state"] is None and empty["iterations"] == 0


def test_write_summary_creates_summary_json(tmp_path: Path) -> None:
    run_dir = make_run(tmp_path)

    write_summary(run_dir)

    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["state"] == "DONE"


def test_replay_splits_plan_and_iterations_with_their_artifacts(tmp_path: Path) -> None:
    replay = build_replay(make_run(tmp_path))

    steps = replay["steps"]
    assert [s["label"] for s in steps] == ["Plan", "Iteration 1", "Iteration 2"]
    assert steps[0]["artifacts"] == ["TASK.md", "planner.prompt.md"]
    assert steps[0]["events"][-1]["type"] == "plan"
    assert steps[1]["dir"] == "iterations/1" and steps[1]["artifacts"] == ["iterations/1/implementer.prompt.md", "iterations/1/diff.patch"]
    assert steps[1]["events"][0]["type"] == "state" and any(e["type"] == "answer" for e in steps[1]["events"])
    assert steps[2]["events"][-1]["state"] == "DONE"  # the gate belongs to the last iteration
    assert replay["discarded"] == [{"dir": "iterations/1.discarded-20261007T160300", "artifacts": ["iterations/1.discarded-20261007T160300/diff.patch"]}]


def test_replay_merges_a_resumed_iteration_and_maps_rollbacks_to_archives(tmp_path: Path) -> None:
    run_dir = tmp_path / "RROLL1"
    for name in ("1", "2", "2.discarded-20261007T160000"):
        (run_dir / "iterations" / name).mkdir(parents=True)
        (run_dir / "iterations" / name / "diff.patch").write_text("", encoding="utf-8")
    events = [ev("00:00.000", "state", state="PLANNING"),
              ev("00:01.000", "state", state="IMPLEMENTING", iteration=1),
              ev("00:02.000", "resume", phase="implement", iteration=1),
              ev("00:03.000", "state", state="IMPLEMENTING", iteration=1),   # killed and resumed: same step
              ev("00:04.000", "state", state="IMPLEMENTING", iteration=2),
              ev("00:05.000", "rollback", to_iteration=1),
              ev("00:06.000", "state", state="IMPLEMENTING", iteration=2)]   # after the rollback
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    steps = build_replay(run_dir)["steps"]

    assert [(s["label"], s["dir"]) for s in steps] == [
        ("Plan", ""), ("Iteration 1", "iterations/1"),
        ("Iteration 2 (discarded)", "iterations/2.discarded-20261007T160000"), ("Iteration 2", "iterations/2")]
    assert len(steps[1]["events"]) == 3


def test_time_in_the_queue_before_the_process_started_counts_as_queued(tmp_path: Path) -> None:
    run_dir = tmp_path / "RQ1"
    run_dir.mkdir()
    events = [ev("00:00.000", "queued", repo="o/a", title="t"),
              ev("00:36.000", "resume_spawned", pid=1),
              ev("00:36.500", "state", state="QUEUED"),
              ev("00:40.000", "state", state="PLANNING"),
              ev("01:00.000", "state", state="DONE")]
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    s = build_summary(run_dir)

    assert s["time_by_state"] == {"QUEUED": 40, "PLANNING": 20} and s["wall_seconds"] == 60
