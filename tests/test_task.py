import pytest

from orq.core.task import Task, TaskError, parse_task

FULL = """# Task: Add greeting endpoint
## Repo
owner/sandbox, base branch develop
## Goal
Expose GET /hello that returns a greeting.
Second paragraph of the goal.
## Acceptance criteria
- [ ] GET /hello returns 200
- [x] response body is JSON
## Out of scope
- authentication
## Constraints
- no new dependencies
## Check command
uv run pytest -q
## Plan approval
skip
"""


def test_parses_every_section() -> None:
    task = parse_task(FULL)

    assert isinstance(task, Task)
    assert task.title == "Add greeting endpoint"
    assert task.repo == "owner/sandbox"
    assert task.base_branch == "develop"
    assert task.goal == "Expose GET /hello that returns a greeting.\nSecond paragraph of the goal."
    assert task.acceptance_criteria == ["GET /hello returns 200", "response body is JSON"]
    assert task.out_of_scope == ["authentication"]
    assert task.constraints == ["no new dependencies"]
    assert task.check_command == "uv run pytest -q"
    assert task.plan_approval == "skip"


def test_slug_is_derived_from_title() -> None:
    task = parse_task(FULL.replace("Add greeting endpoint", "Fix: the /Hello  endpoint (v2)!"))

    assert task.slug == "fix-the-hello-endpoint-v2"


def test_slug_is_capped_at_40_chars() -> None:
    task = parse_task(FULL.replace("Add greeting endpoint", "a" * 60))

    assert len(task.slug) == 40


def test_base_branch_defaults_to_main() -> None:
    task = parse_task(FULL.replace("owner/sandbox, base branch develop", "owner/sandbox"))

    assert task.base_branch == "main"


def test_optional_sections_default_to_empty() -> None:
    text = FULL.replace("## Out of scope\n- authentication\n", "").replace("## Constraints\n- no new dependencies\n", "")

    task = parse_task(text)

    assert task.out_of_scope == []
    assert task.constraints == []


def test_plan_approval_defaults_to_required() -> None:
    task = parse_task(FULL.replace("## Plan approval\nskip\n", ""))

    assert task.plan_approval == "required"


@pytest.mark.parametrize(
    ("broken", "section"),
    [
        (FULL.replace("## Goal\nExpose GET /hello that returns a greeting.\nSecond paragraph of the goal.\n", ""), "Goal"),
        (FULL.replace("## Check command\nuv run pytest -q\n", ""), "Check command"),
        (FULL.replace("- [ ] GET /hello returns 200\n- [x] response body is JSON\n", ""), "Acceptance criteria"),
        (FULL.replace("# Task: Add greeting endpoint", "# Something else"), "Task"),
        (FULL.replace("owner/sandbox, base branch develop", "not-a-slug"), "Repo"),
    ],
)
def test_missing_or_invalid_required_section_raises(broken: str, section: str) -> None:
    with pytest.raises(TaskError, match=section):
        parse_task(broken)


def test_invalid_plan_approval_raises() -> None:
    with pytest.raises(TaskError, match="Plan approval"):
        parse_task(FULL.replace("skip", "maybe"))
