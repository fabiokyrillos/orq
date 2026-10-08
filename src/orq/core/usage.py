"""Token usage (Phase 7.1): overall, per project and per run, from the run dirs and the chat logs.

Each agent call is a role event in `events.jsonl` (planner, implementer, reviewer) or an assistant line in a chat's
`messages.jsonl`. Plan limits are account-wide: the latest Codex `rate_limits` snapshot and the latest Claude
`rate_limit_info`. A compaction is a `system`/`compact_boundary` line in a Claude stream.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from orq.core.summary import ROLES, _input_tokens, read_events
from orq.paths import OrqPaths
from orq.store.db import Store

COMPACT_MARK = '"compact_boundary"'


def _day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone().date().isoformat()


def _compactions(files: list[Path]) -> int:
    count = 0
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                count += sum(1 for line in handle if COMPACT_MARK in line and '"subtype"' in line)
        except OSError:
            continue
    return count


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _calls(paths: OrqPaths, store: Store) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """(calls, runs by id, rate-limit snapshots). A call: ts, project, run_id, role, model, input, output."""
    calls: list[dict] = []
    runs: dict[str, dict] = {}
    limits: list[dict] = []
    for run in store.list_runs():
        run_dir = paths.run_dir(run.run_id)
        runs[run.run_id] = {"run_id": run.run_id, "project": run.repo, "title": run.task_title, "state": run.state.value,
                            "calls": 0, "input_tokens": 0, "output_tokens": 0,
                            "compactions": _compactions(list(run_dir.glob("iterations/*/implementer.stream.jsonl")))}
        implementer_model = None  # before Phase 6 the role events had no model; implementer_model events did
        for event in read_events(run_dir):
            if event.get("type") == "implementer_model":
                implementer_model = event.get("model")
            if event.get("type") not in ROLES:
                continue
            usage = event.get("usage") or {}
            model = event.get("model") or (implementer_model if event["type"] == "implementer" else None) or "?"
            calls.append({"ts": event["ts"], "project": run.repo, "run_id": run.run_id, "role": event["type"],
                          "model": model, "input_tokens": _input_tokens(usage),
                          "output_tokens": int(usage.get("output_tokens") or 0)})
            if isinstance(event.get("rate_limit"), dict):
                limits.append({"ts": event["ts"], **event["rate_limit"]})
    chats = paths.root / "chats"
    for chat_dir in sorted(chats.glob("*/*")) if chats.exists() else []:
        project = chat_dir.parent.name.replace("__", "/", 1)
        for message in _read_jsonl(chat_dir / "messages.jsonl"):
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            calls.append({"ts": message["ts"], "project": project, "run_id": None, "role": "chat",
                          "model": message.get("model") or "?", "input_tokens": _input_tokens(usage),
                          "output_tokens": int(usage.get("output_tokens") or 0)})
            if isinstance(message.get("rate_limit"), dict):
                limits.append({"ts": message["ts"], **message["rate_limit"]})
    return calls, runs, limits


def _chat_compactions(paths: OrqPaths, project: str | None) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    chats = paths.root / "chats"
    for chat_dir in sorted(chats.glob("*/*")) if chats.exists() else []:
        name = chat_dir.parent.name.replace("__", "/", 1)
        if project is None or name == project:
            out[name] += _compactions([chat_dir / "claude.stream.jsonl"])
    return out


def _group(calls: list[dict], key: str, extra: dict[str, int] | None = None) -> list[dict]:
    groups: dict[str, dict] = {}
    for call in calls:
        row = groups.setdefault(call[key], {key: call[key], "calls": 0, "input_tokens": 0, "output_tokens": 0})
        row["calls"] += 1
        row["input_tokens"] += call["input_tokens"]
        row["output_tokens"] += call["output_tokens"]
    if extra is not None:
        for name, count in extra.items():
            groups.setdefault(name, {key: name, "calls": 0, "input_tokens": 0, "output_tokens": 0})
        for row in groups.values():
            row["compactions"] = extra.get(row[key], 0)
    return list(groups.values())


def _latest(limits: list[dict], kind: str) -> dict | None:
    """The newest Codex snapshot (has `primary`) or Claude info (has `status`), with its time as `at`."""
    matching = [l for l in limits if ("primary" in l if kind == "codex" else "status" in l)]
    if not matching:
        return None
    newest = max(matching, key=lambda l: l["ts"])
    return {**{k: v for k, v in newest.items() if k != "ts"}, "at": newest["ts"]}


def collect(paths: OrqPaths, store: Store, project: str | None = None) -> dict:
    calls, runs, limits = _calls(paths, store)
    if project is not None:
        calls = [c for c in calls if c["project"] == project]
        runs = {k: v for k, v in runs.items() if v["project"] == project}
    for call in calls:
        if call["run_id"] in runs:
            row = runs[call["run_id"]]
            row["calls"] += 1
            row["input_tokens"] += call["input_tokens"]
            row["output_tokens"] += call["output_tokens"]
    compactions: dict[str, int] = defaultdict(int)
    for row in runs.values():
        compactions[row["project"]] += row["compactions"]
    for name, count in _chat_compactions(paths, project).items():
        compactions[name] += count
    for call in calls:
        call["day"] = _day(call["ts"])
    by_project = _group(calls, "project", {k: v for k, v in compactions.items() if v or any(c["project"] == k for c in calls)})
    return {
        "project": project,
        "totals": {"calls": len(calls), "input_tokens": sum(c["input_tokens"] for c in calls),
                   "output_tokens": sum(c["output_tokens"] for c in calls), "compactions": sum(compactions.values())},
        "by_day": sorted(_group(calls, "day"), key=lambda r: r["day"]),
        "by_project": sorted(by_project, key=lambda r: -(r["input_tokens"] + r["output_tokens"])),
        "by_role": _group(calls, "role"),
        "by_model": sorted(_group(calls, "model"), key=lambda r: -(r["input_tokens"] + r["output_tokens"])),
        "runs": sorted((r for r in runs.values() if r["calls"]), key=lambda r: -(r["input_tokens"] + r["output_tokens"])),
        "limits": {"codex": _latest(limits, "codex"), "claude": _latest(limits, "claude")},
    }
