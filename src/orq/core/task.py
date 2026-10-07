"""TASK.md parser (SPEC section 8.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_REPO_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)(?:\s*,\s*base branch\s+(?P<base>\S+))?$")
_BULLET_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(.*\S)")
_TITLE_RE = re.compile(r"^#[ \t]*Task:[ \t]*(.*\S)[ \t]*$", re.MULTILINE)  # one line: an empty title must not swallow the next heading


class TaskError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    title: str
    repo: str
    base_branch: str
    goal: str
    acceptance_criteria: list[str]
    out_of_scope: list[str]
    constraints: list[str]
    check_command: str
    plan_approval: str

    @property
    def slug(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return slug[:40].rstrip("-")


def parse_task(text: str) -> Task:
    title_match = _TITLE_RE.search(text)
    if not title_match:
        raise TaskError("missing '# Task: <title>' heading")
    sections = _split_sections(text)

    repo_line = _required(sections, "Repo")
    repo_match = _REPO_RE.match(repo_line.strip())
    if not repo_match:
        raise TaskError(f"Repo must be '<owner>/<repo>[, base branch <name>]', got: {repo_line!r}")

    criteria = _bullets(_required(sections, "Acceptance criteria"))
    if not criteria:
        raise TaskError("Acceptance criteria must contain at least one bullet")

    plan_approval = sections.get("Plan approval", "required").strip() or "required"
    if plan_approval not in ("required", "skip"):
        raise TaskError(f"Plan approval must be 'required' or 'skip', got: {plan_approval!r}")

    return Task(
        title=title_match.group(1),
        repo=repo_match.group("repo"),
        base_branch=repo_match.group("base") or "main",
        goal=_required(sections, "Goal"),
        acceptance_criteria=criteria,
        out_of_scope=_bullets(sections.get("Out of scope", "")),
        constraints=_bullets(sections.get("Constraints", "")),
        check_command=_required(sections, "Check command"),
        plan_approval=plan_approval,
    )


def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current, lines = line[3:].strip(), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def _required(sections: dict[str, str], name: str) -> str:
    value = sections.get(name, "").strip()
    if not value:
        raise TaskError(f"missing or empty section: {name}")
    return value


def _bullets(block: str) -> list[str]:
    items = []
    for line in block.splitlines():
        match = _BULLET_RE.match(line)
        if match:
            items.append(match.group(1).strip())
    return items
