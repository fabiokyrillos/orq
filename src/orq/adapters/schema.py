"""Reviewer (SPEC 8.2) and planner (SPEC 8.5) output contracts, in the strict form both CLIs accept."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

STATUSES = ("continue", "done", "needs_human")
PLAN_STATUSES = ("plan", "needs_human")
DIFFICULTIES = ("hard", "mechanical")
MAX_MILESTONES = 8
DECISION_TYPES = ("business", "ambiguity", "risk", "blocked")
SEVERITIES = ("blocker", "major", "minor")

_HUMAN = {
    "type": ["object", "null"],
    "properties": {
        "decision_type": {"type": "string", "enum": list(DECISION_TYPES)},
        # Phase 6: the owner decides from the phone, so the question travels with its context and consequences.
        "context": {"type": "string"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "option_details": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "integer"},
        "recommendation_reason": {"type": "string"},
    },
    "required": ["decision_type", "context", "question", "options", "option_details", "recommendation", "recommendation_reason"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "summary": {"type": "string"},
        "milestone": {"type": "string"},
        "next_prompt": {"type": ["string", "null"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "description": {"type": "string"},
                },
                "required": ["severity", "description"],
                "additionalProperties": False,
            },
        },
        "human": _HUMAN,
        # Phase 6: plain-language progress for the owner's digests (what this iteration achieved, what comes next).
        "owner_update": {"type": "string"},
    },
    "required": ["status", "summary", "milestone", "next_prompt", "issues", "human", "owner_update"],
    "additionalProperties": False,
}
# Fields the CLI is asked for but older answers (and the test fakes) may lack; the runner treats them as empty.
TOLERATED_MISSING = {"owner_update", "task_complete"}


def validate_review(data: object) -> list[str]:
    """Return a list of problems; empty means the object matches the contract."""
    if not isinstance(data, dict):
        return ["output is not a JSON object"]
    problems = [f"missing key: {key}" for key in REVIEW_SCHEMA["required"] if key not in data and key not in TOLERATED_MISSING]
    if data.get("status") not in STATUSES:
        problems.append(f"status must be one of {STATUSES}, got {data.get('status')!r}")
    if not isinstance(data.get("issues", []), list):
        problems.append("issues must be a list")
    human = data.get("human")
    if data.get("status") == "needs_human":
        if not isinstance(human, dict):
            problems.append("human must be an object when status is needs_human")
        elif human.get("decision_type") not in DECISION_TYPES:
            problems.append(f"human.decision_type must be one of {DECISION_TYPES}")
    return problems


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(PLAN_STATUSES)},
        "summary": {"type": "string"},
        "milestones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "done_when": {"type": "string"},
                    "difficulty": {"type": "string", "enum": list(DIFFICULTIES)},
                },
                "required": ["title", "goal", "done_when", "difficulty"],
                "additionalProperties": False,
            },
        },
        "human": _HUMAN,
    },
    "required": ["status", "summary", "milestones", "human"],
    "additionalProperties": False,
}


def validate_plan(data: object) -> list[str]:
    """Return a list of problems; empty means the object matches the plan contract."""
    if not isinstance(data, dict):
        return ["output is not a JSON object"]
    problems = [f"missing key: {key}" for key in PLAN_SCHEMA["required"] if key not in data]
    status = data.get("status")
    if status not in PLAN_STATUSES:
        problems.append(f"status must be one of {PLAN_STATUSES}, got {status!r}")
    milestones = data.get("milestones")
    if not isinstance(milestones, list):
        problems.append("milestones must be a list")
        milestones = []
    if status == "plan" and not milestones:
        problems.append("a plan needs at least one milestone")
    if len(milestones) > MAX_MILESTONES:
        problems.append(f"at most {MAX_MILESTONES} milestones")
    for index, item in enumerate(milestones):
        if not isinstance(item, dict):
            problems.append(f"milestone {index} is not an object")
            continue
        for key in ("title", "goal", "done_when"):
            if not str(item.get(key, "")).strip():
                problems.append(f"milestone {index}: {key} is empty")
        if item.get("difficulty") not in DIFFICULTIES:
            problems.append(f"milestone {index}: difficulty must be one of {DIFFICULTIES}")
    human = data.get("human")
    if status == "needs_human":
        if not isinstance(human, dict):
            problems.append("human must be an object when status is needs_human")
        elif human.get("decision_type") not in DECISION_TYPES:
            problems.append(f"human.decision_type must be one of {DECISION_TYPES}")
    return problems


@dataclass(frozen=True)
class OutputContract:
    name: str
    schema: dict
    validate: Callable[[object], list[str]]


REVIEW_CONTRACT = OutputContract("review", REVIEW_SCHEMA, validate_review)
PLAN_CONTRACT = OutputContract("plan", PLAN_SCHEMA, validate_plan)
