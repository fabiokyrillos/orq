from orq.core.models import Decision, RunRecord, RunState
from orq.notify.messages import DESTRUCTIVE_HINT, HINT, format_decision, format_run_state, format_status, parse_reply


def decision(**overrides) -> Decision:
    base = dict(decision_id="D7K2", run_id="RAB2CD", source="reviewer", decision_type="business",
                question="Is the discount applied before or after tax?", options=["Before", "After"], recommendation=0)
    base.update(overrides)
    return Decision(**base)


def run(**overrides) -> RunRecord:
    base = dict(run_id="RAB2CD", repo="owner/repo-x", task_title="Discounts", branch="orq/discounts", iteration=6)
    base.update(overrides)
    return RunRecord(**base)


def test_format_decision_matches_spec_shape() -> None:
    text = format_decision(decision(), run())
    assert text.splitlines() == [
        "*[orq] D7K2 · repo-x · iteration 6*",
        "Business decision (reviewer): Is the discount applied before or after tax?",
        "1. Before (recommended)",
        "2. After",
        "Reply: D7K2 <number>  or  D7K2 <free text>",
    ]


def test_format_destructive_decision_asks_for_approve_or_deny() -> None:
    text = format_decision(decision(source="guard", decision_type="risk", destructive=True, options=["approve", "deny"], recommendation=1,
                                    question="The implementer tried: rm -rf build. Allow it once?"), run())
    assert text.endswith("Reply: APPROVE D7K2  or  DENY D7K2") and "Risk (guard)" in text


def test_format_run_state_and_status() -> None:
    done = run(state=RunState.DONE)
    assert format_run_state(done, pr_url="https://x/pull/4").splitlines() == ["*[orq] RAB2CD · repo-x · DONE*", "Discounts", "PR: https://x/pull/4"]
    failed = run(state=RunState.FAILED)
    assert "max iterations" in format_run_state(failed, reason="max iterations (3) reached")
    status = format_status([run(state=RunState.AWAITING_HUMAN)], {"RAB2CD": [decision()]})
    assert "RAB2CD AWAITING_HUMAN iter 6" in status and "pending D7K2" in status
    assert format_status([], {}) == "*[orq] STATUS*\nno runs"


def test_parse_answers() -> None:
    assert parse_reply("D7K2 1") == parse_reply("d7k2 1")
    r = parse_reply("D7K2 2")
    assert r.kind == "answer_index" and r.decision_id == "D7K2" and r.index == 2
    r = parse_reply("D7K2 after taxes, always")
    assert r.kind == "answer_text" and r.text == "after taxes, always"
    assert parse_reply("APPROVE D7K2").kind == "approve" and parse_reply("deny d7k2").kind == "deny"
    assert parse_reply("D7K2 approve").kind == "approve" and parse_reply("D7K2 DENY").decision_id == "D7K2"


def test_parse_commands() -> None:
    assert parse_reply("STATUS").kind == "status" and parse_reply("STATUS").run_id is None
    assert parse_reply("status rab2cd") == parse_reply("STATUS RAB2CD")
    assert parse_reply("STATUS RAB2CD").run_id == "RAB2CD"
    for word in ("PAUSE", "RESUME", "ABORT"):
        r = parse_reply(f"{word} RAB2CD")
        assert r.kind == word.lower() and r.run_id == "RAB2CD"
    assert parse_reply("PAUSE").kind == "unknown" and parse_reply("ABORT nope").kind == "unknown"


def test_parse_unknown_shapes() -> None:
    assert parse_reply("").kind == "unknown"
    assert parse_reply("yes").kind == "unknown"
    assert parse_reply("1").kind == "unknown"
    assert parse_reply("D7K2").kind == "unknown" and parse_reply("D7K2").decision_id == "D7K2"
    assert parse_reply("APPROVE").kind == "unknown"
    assert "D7K2 1" in HINT and "APPROVE D7K2" in DESTRUCTIVE_HINT.format(id="D7K2")


def test_status_groups_by_project_and_shows_slots() -> None:
    runs = [run(run_id="RA1", repo="owner/a", state=RunState.IMPLEMENTING), run(run_id="RB1", repo="owner/b", state=RunState.QUEUED),
            run(run_id="RA2", repo="owner/a", state=RunState.DONE)]

    status = format_status(runs, {}, slots=(1, 2), queued=1)

    lines = status.splitlines()
    assert lines[0] == "*[orq] STATUS* · slots 1/2 · 1 queued"
    assert lines[1] == "*a*" and lines[2].startswith("RA1 IMPLEMENTING") and lines[3].startswith("RA2 DONE")
    assert lines[4] == "*b*" and lines[5].startswith("RB1 QUEUED")
