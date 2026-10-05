"""Reviewer output contract (SPEC section 8.2), in the strict form both CLIs accept."""

from __future__ import annotations

STATUSES = ("continue", "done", "needs_human")
DECISION_TYPES = ("business", "ambiguity", "risk", "blocked")
SEVERITIES = ("blocker", "major", "minor")

_HUMAN = {
    "type": ["object", "null"],
    "properties": {
        "decision_type": {"type": "string", "enum": list(DECISION_TYPES)},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "integer"},
    },
    "required": ["decision_type", "question", "options", "recommendation"],
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
    },
    "required": ["status", "summary", "milestone", "next_prompt", "issues", "human"],
    "additionalProperties": False,
}


def validate_review(data: object) -> list[str]:
    """Return a list of problems; empty means the object matches the contract."""
    if not isinstance(data, dict):
        return ["output is not a JSON object"]
    problems = [f"missing key: {key}" for key in REVIEW_SCHEMA["required"] if key not in data]
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
