"""Progress digest (Phase 6): what a run has done and what it is doing, for WhatsApp and the dashboard.

Pure over the run dir. The prose comes from the reviewer's `owner_update` field (one per review, no extra agent call);
orq adds the structure and the numbers. Phase 6.1: in Brazilian Portuguese, in short sections with emojis. Capped at
1500 characters, oldest milestones trimmed first.
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
_PHASE_WORDS = {"plan": "planejando", "implement": "implementando", "verify": "rodando o check", "review": "em revisão",
                "await": "esperando você", "finalize": "abrindo o PR", "gate_ci": "esperando o CI", "gate_review": "revisão final",
                "gate_merge": "fazendo o merge"}
_STATE_WORDS = {"DONE": "concluída", "FAILED": "falhou", "ABORTED": "abortada"}


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"


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
    repo_name = (found.group(1) if found else (repo or "?")).split("/")[-1]
    header = [f"📊 *Progresso* · {run_dir.name} · {repo_name}", title.group(1) if title else run_dir.name,
              f"⏱️ {_minutes(work)} de trabalho · {_STATE_WORDS.get(state, state)}"]

    milestones = ((cp.plan or {}).get("milestones") or []) if cp else []
    updates = [e for e in events if e["type"] == "owner_update" and str(e.get("text") or "").strip()]
    by_iteration = {e.get("iteration"): str(e["text"]).strip() for e in updates}
    done_lines = []
    for e in events:
        if e["type"] == "milestone_done":
            index = int(e.get("milestone") or 0)
            name = milestones[index - 1]["title"] if 0 < index <= len(milestones) else f"milestone {index}"
            text = _clip(by_iteration.get(e.get("iteration"), "pronto"), 240)
            done_lines.append(f"• {index}/{e.get('of') or len(milestones)} *{name}*: {text}")
    if state == "DONE" and milestones and len(done_lines) < len(milestones):
        # The last milestone ends in the merge gate, not in a milestone_done event.
        last = len(milestones)
        done_lines.append(f"• {last}/{last} *{milestones[-1]['title']}*: " + (_clip(updates[-1]["text"], 240) if updates else "pronto"))

    now: list[str] = []
    if cp and state not in ("DONE", "FAILED", "ABORTED"):
        where = _PHASE_WORDS.get(cp.phase, cp.phase)
        if milestones:
            index = min(cp.milestone_index, len(milestones) - 1)
            now.append(f"milestone {index + 1}/{len(milestones)} *{milestones[index]['title']}* · iteração {cp.iteration} · {where}")
        else:
            now.append(f"iteração {cp.iteration} · {where}")
        if cp.pending_decision:
            now.append(f"⏳ Esperando você: {cp.pending_decision['decision_id']}")
    latest = _clip(updates[-1]["text"], 500) if updates and state != "DONE" else ""
    checks = summary["checks"]
    numbers = f"🧪 {checks['runs'] - checks['failed']}/{checks['runs']} checks ok · 📁 {len(_files_changed(run_dir))} arquivos alterados"

    def render(done: list[str], latest_text: str) -> str:
        parts = list(header)
        if done:
            parts += ["", "✅ *Feito*", *done]
        if now:
            parts += ["", "🔨 *Agora*", *now]
        if latest_text:
            parts += ["", "📝 *Última atualização*", latest_text]
        return "\n".join(parts + ["", numbers])

    text = render(done_lines, latest)
    while len(text) > CAP and len(done_lines) > 1:
        done_lines = ["• (milestones anteriores omitidos)", *done_lines[2:]] if not done_lines[0].startswith("• (") else \
            [done_lines[0], *done_lines[2:]]
        text = render(done_lines, latest)
    if len(text) > CAP and latest:
        room = CAP - (len(text) - len(latest))
        latest = latest[: max(0, room - 3)].rstrip() + "..."
        text = render(done_lines, latest)
    return text[:CAP]
