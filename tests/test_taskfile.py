import pytest

from orq.core.task import TaskError, parse_task
from orq.core.taskfile import TaskFields, fields_from_task, render_task


def full() -> TaskFields:
    return TaskFields(title="Add greeting", repo="owner/a", base_branch="develop", goal="Say hello.\nTwo lines.",
                      acceptance_criteria=["greet.py exists", "tests pass"], out_of_scope=["i18n"],
                      constraints=["no new deps"], check_command="uv run pytest -q", plan_approval="skip")


def test_render_round_trips_through_parse_task() -> None:
    task = parse_task(render_task(full()))

    assert (task.title, task.repo, task.base_branch, task.plan_approval) == ("Add greeting", "owner/a", "develop", "skip")
    assert task.goal == "Say hello.\nTwo lines."
    assert task.acceptance_criteria == ["greet.py exists", "tests pass"]
    assert task.out_of_scope == ["i18n"] and task.constraints == ["no new deps"]
    assert task.check_command == "uv run pytest -q"
    assert fields_from_task(task) == full()


def test_empty_optional_sections_are_left_out_and_blank_items_dropped() -> None:
    fields = full()
    fields.out_of_scope, fields.constraints = [], ["  ", ""]
    fields.acceptance_criteria = ["one", " "]

    text = render_task(fields)

    assert "## Out of scope" not in text and "## Constraints" not in text
    assert parse_task(text).acceptance_criteria == ["one"]


def test_missing_required_fields_fail_validation() -> None:
    fields = full()
    fields.acceptance_criteria = []
    with pytest.raises(TaskError):
        parse_task(render_task(fields))


def test_empty_title_is_rejected_not_taken_from_the_next_heading() -> None:
    fields = full()
    fields.title = "  "
    with pytest.raises(TaskError, match="Task"):
        parse_task(render_task(fields))
