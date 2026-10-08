import json
from pathlib import Path

from orq.core.models import RunRecord
from orq.core.usage import collect
from orq.paths import OrqPaths
from orq.store.db import Store


def write_run(paths: OrqPaths, store: Store, run_id: str, repo: str, events: list[dict], compactions: int = 0) -> None:
    store.create_run(RunRecord(run_id=run_id, repo=repo, task_title=f"task {run_id}", branch="b"))
    run_dir = paths.run_dir(run_id)
    (run_dir / "iterations" / "1").mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    stream = [{"type": "system", "subtype": "compact_boundary", "session_id": "s"}] * compactions + [{"type": "result"}]
    (run_dir / "iterations" / "1" / "implementer.stream.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in stream), encoding="utf-8")


CODEX_LIMITS = {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1791499181},
                "secondary": {"used_percent": 30.0, "window_minutes": 10080, "resets_at": 1791977038}}
CLAUDE_LIMITS = {"status": "allowed", "resetsAt": 1791500000, "rateLimitType": "five_hour"}


def test_usage_totals_by_day_project_role_model_with_limits_and_compactions(tmp_path: Path) -> None:
    paths, store = OrqPaths(tmp_path / "home"), Store(tmp_path / "home" / "orq.db")
    write_run(paths, store, "RA0001", "owner/a", [
        {"ts": "2026-10-07T12:00:00+00:00", "type": "state", "state": "PLANNING"},
        {"ts": "2026-10-07T12:01:00+00:00", "type": "planner", "ok": True, "model": "gpt-6.1-sol",
         "usage": {"input_tokens": 1000, "output_tokens": 100}, "rate_limit": {**CODEX_LIMITS, "primary": {"used_percent": 5.0}}},
        {"ts": "2026-10-08T12:02:00+00:00", "type": "implementer", "ok": True, "model": "opus",
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 490, "output_tokens": 50}, "rate_limit": CLAUDE_LIMITS},
    ], compactions=2)
    write_run(paths, store, "RB0001", "owner/b", [
        {"ts": "2026-10-08T13:00:00+00:00", "type": "reviewer", "ok": True, "model": "gpt-6.1-sol",
         "usage": {"input_tokens": 2000, "output_tokens": 200}, "rate_limit": CODEX_LIMITS},
    ])
    chat = paths.root / "chats" / "owner__a" / "C1"
    chat.mkdir(parents=True)
    (chat / "messages.jsonl").write_text(
        json.dumps({"role": "user", "text": "oi", "ts": "2026-10-08T14:00:00+00:00"}) + "\n"
        + json.dumps({"role": "assistant", "text": "olá", "ts": "2026-10-08T14:00:30+00:00", "model": "opus",
                      "usage": {"input_tokens": 300, "output_tokens": 30}}) + "\n", encoding="utf-8")
    (chat / "claude.stream.jsonl").write_text(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n", encoding="utf-8")

    usage = collect(paths, store)

    assert usage["totals"] == {"calls": 4, "input_tokens": 3800, "output_tokens": 380, "compactions": 3}
    by_project = {row["project"]: row for row in usage["by_project"]}
    assert (by_project["owner/a"]["input_tokens"], by_project["owner/a"]["calls"], by_project["owner/a"]["compactions"]) == (1800, 3, 3)
    assert by_project["owner/b"]["output_tokens"] == 200
    assert {row["role"]: row["calls"] for row in usage["by_role"]} == {"planner": 1, "implementer": 1, "reviewer": 1, "chat": 1}
    assert {row["model"]: row["input_tokens"] for row in usage["by_model"]} == {"gpt-6.1-sol": 3000, "opus": 800}
    assert [row["day"] for row in usage["by_day"]] == ["2026-10-07", "2026-10-08"]
    assert usage["runs"][0]["run_id"] == "RB0001" and usage["runs"][0]["title"] == "task RB0001"  # most tokens first
    assert usage["limits"]["codex"]["primary"]["used_percent"] == 12.0  # the latest snapshot wins
    assert usage["limits"]["codex"]["at"] == "2026-10-08T13:00:00+00:00"
    assert usage["limits"]["claude"]["status"] == "allowed"

    only_b = collect(paths, store, project="owner/b")
    assert only_b["totals"]["calls"] == 1 and [r["project"] for r in only_b["by_project"]] == ["owner/b"]
    assert only_b["limits"]["codex"]["primary"]["used_percent"] == 12.0  # plan limits are account-wide


def test_usage_of_an_empty_install(tmp_path: Path) -> None:
    usage = collect(OrqPaths(tmp_path / "home"), Store(tmp_path / "orq.db"))
    assert usage["totals"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "compactions": 0}
    assert usage["limits"] == {"codex": None, "claude": None}
