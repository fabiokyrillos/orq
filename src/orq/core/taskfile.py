"""TASK.md from form fields (Phase 5 dashboard). The inverse of `parse_task`; validation stays in `parse_task`."""

from __future__ import annotations

from dataclasses import dataclass, field

from orq.core.task import Task


@dataclass
class TaskFields:
    title: str
    repo: str
    goal: str
    check_command: str
    base_branch: str = "main"
    acceptance_criteria: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    plan_approval: str = "required"
    models: dict = field(default_factory=dict)  # optional per-task models and efforts (Phase 6)


def _items(values: list[str]) -> list[str]:
    return [" ".join(v.split()) for v in values if v and v.strip()]


def render_task(fields: TaskFields) -> str:
    lines = [f"# Task: {fields.title.strip()}", "## Repo", f"{fields.repo.strip()}, base branch {fields.base_branch.strip() or 'main'}",
             "## Goal", fields.goal.strip(), "## Acceptance criteria", *[f"- [ ] {c}" for c in _items(fields.acceptance_criteria)]]
    if _items(fields.out_of_scope):
        lines += ["## Out of scope", *[f"- {c}" for c in _items(fields.out_of_scope)]]
    if _items(fields.constraints):
        lines += ["## Constraints", *[f"- {c}" for c in _items(fields.constraints)]]
    lines += ["## Check command", fields.check_command.strip(), "## Plan approval", fields.plan_approval.strip() or "required"]
    models = {k: v.strip() for k, v in (fields.models or {}).items() if v and v.strip()}
    if models:
        lines += ["## Models", *[f"{k}: {v}" for k, v in models.items()]]
    return "\n".join(lines) + "\n"


def fields_from_task(task: Task) -> TaskFields:
    return TaskFields(title=task.title, repo=task.repo, base_branch=task.base_branch, goal=task.goal,
                      acceptance_criteria=list(task.acceptance_criteria), out_of_scope=list(task.out_of_scope),
                      constraints=list(task.constraints), check_command=task.check_command, plan_approval=task.plan_approval,
                      models=dict(task.models))
