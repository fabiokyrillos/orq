"""Prompt builders: each side gets its standing rules (a Phase 6 finding: the reviewer and planner rules were never sent)."""

from orq.core.prompts import HUMAN_GUIDE, PLANNER_RULES, REVIEWER_RULES, build_planner_prompt, build_reviewer_prompt
from orq.core.task import parse_task
from orq.verify.checks import CheckResult

TASK = parse_task("# Task: T\n## Repo\no/r\n## Goal\ng\n## Acceptance criteria\n- [ ] a\n## Check command\nc\n")


def test_reviewer_prompt_starts_with_the_reviewer_rules() -> None:
    prompt = build_reviewer_prompt(TASK, iteration=1, milestone=None, diff_stat="", diff_path="d", diff_excerpt=None,
                                   check=CheckResult(ok=True, exit_code=0, output="ok", timed_out=False), implementer_report="r",
                                   decisions="")
    assert prompt.startswith(REVIEWER_RULES) and HUMAN_GUIDE in prompt and "# Task: T" in prompt


def test_planner_prompt_starts_with_the_planner_rules() -> None:
    prompt = build_planner_prompt(TASK, decisions="", feedback=None)
    assert prompt.startswith(PLANNER_RULES) and "Never plan a milestone for verification" in prompt and "# Task: T" in prompt


def test_owner_facing_fields_are_asked_in_portuguese_with_stakes() -> None:
    from orq.adapters.schema import REVIEW_SCHEMA
    from orq.core.prompts import IMPLEMENTER_RULES
    assert "Brazilian Portuguese" in HUMAN_GUIDE and "stakes" in HUMAN_GUIDE and "never rate a business question" in HUMAN_GUIDE
    assert "Brazilian Portuguese" in REVIEWER_RULES  # owner_update
    assert "stakes" in IMPLEMENTER_RULES and REVIEW_SCHEMA["properties"]["human"]["properties"]["stakes"]["enum"] == ["low", "medium", "high"]
