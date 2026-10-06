from orq.core.progress import ProgressTracker


def test_different_iterations_do_not_fire() -> None:
    t = ProgressTracker()
    assert t.record(1, diff_hash="a", failure_signature=None, next_prompt="do x") is None
    assert t.record(2, diff_hash="b", failure_signature=None, next_prompt="do y") is None
    assert t.record(3, diff_hash="c", failure_signature="f", next_prompt="do z") is None


def test_same_diff_twice_targets_iteration_before_streak() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="p1")
    t.record(2, diff_hash="b", failure_signature=None, next_prompt="p2")
    rule = t.record(3, diff_hash="b", failure_signature=None, next_prompt="p3")
    assert rule is not None and rule.rule == "same_diff" and rule.rollback_to == 1


def test_same_diff_from_the_start_targets_base() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="p1")
    rule = t.record(2, diff_hash="a", failure_signature=None, next_prompt="p2")
    assert rule.rule == "same_diff" and rule.rollback_to == 0


def test_same_failure_three_times() -> None:
    t = ProgressTracker()
    assert t.record(1, diff_hash="a", failure_signature="f", next_prompt="p1") is None
    assert t.record(2, diff_hash="b", failure_signature="f", next_prompt="p2") is None
    rule = t.record(3, diff_hash="c", failure_signature="f", next_prompt="p3")
    assert rule.rule == "same_failure" and rule.rollback_to == 0


def test_passing_checks_do_not_count_as_same_failure() -> None:
    t = ProgressTracker()
    prompts = ["add the parser tests", "fix the lint errors in cli.py", "document the config keys"]
    for i, prompt in enumerate(prompts, start=1):
        assert t.record(i, diff_hash=str(i), failure_signature=None, next_prompt=prompt) is None


def test_near_identical_prompt_fires() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="Please add tests for the parser module and fix lint")
    rule = t.record(2, diff_hash="b", failure_signature=None, next_prompt="Please add tests for the parser module and fix lint.")
    assert rule.rule == "same_prompt" and rule.rollback_to == 0


def test_empty_prompts_do_not_fire_same_prompt() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt=None)
    assert t.record(2, diff_hash="b", failure_signature=None, next_prompt=None) is None


def test_reset_clears_streak_and_history_roundtrips() -> None:
    t = ProgressTracker()
    t.record(1, diff_hash="a", failure_signature=None, next_prompt="p")
    assert t.record(2, diff_hash="a", failure_signature=None, next_prompt="q") is not None
    t.reset()
    assert t.record(3, diff_hash="a", failure_signature=None, next_prompt="r") is None
    again = ProgressTracker(t.history)
    assert again.record(4, diff_hash="a", failure_signature=None, next_prompt="s").rule == "same_diff"


def test_discard_after_rollback_drops_later_iterations() -> None:
    t = ProgressTracker()
    for i in range(1, 5):
        t.record(i, diff_hash=str(i), failure_signature=None, next_prompt=f"prompt {i} {'y' * i}")
    t.discard_after(2)
    assert [h["iteration"] for h in t.history] == [1, 2]
