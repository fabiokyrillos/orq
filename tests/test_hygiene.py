from orq.core.hygiene import drop_orchestrator_milestones, is_sandbox_complaint, review_complains_about_sandbox, strip_sandbox_complaints


def m(title: str, goal: str = "") -> dict:
    return {"title": title, "goal": goal, "done_when": "x", "difficulty": "mechanical"}


def test_drop_orchestrator_milestones() -> None:
    kept, dropped = drop_orchestrator_milestones([
        m("Add greeting module", "Create greet.py"),
        m("Add unittest coverage", "Create test_greet.py using unittest"),
        m("Run check command", "Verify with python -m unittest discover -q"),
        m("Open and merge PR", "Commit the two files, open a PR and merge it"),
        m("Run verification", "Execute the required check command"),
    ])
    assert [x["title"] for x in kept] == ["Add greeting module", "Add unittest coverage"]
    assert len(dropped) == 3


def test_drop_keeps_everything_when_all_would_go() -> None:
    only = [m("Run the tests", "Execute pytest")]
    assert drop_orchestrator_milestones(only) == (only, [])


def test_sandbox_complaint_detection() -> None:
    assert is_sandbox_complaint("`python -m unittest discover -q` fails to start because PowerShell cannot find the `python` command.")
    assert is_sandbox_complaint("Required check command `python -m unittest discover -q` fails because `python` is not recognized on PATH.")
    assert is_sandbox_complaint("I could not run the check command in this environment")
    assert not is_sandbox_complaint("greet('') does not raise ValueError")
    assert not is_sandbox_complaint("test_greet.py is missing the empty-name case")


def test_review_complaint_and_strip() -> None:
    review = {"status": "needs_human", "summary": "s", "milestone": "m", "next_prompt": None,
              "issues": [{"severity": "blocker", "description": "python is not recognized on PATH, so the check could not be verified"},
                         {"severity": "minor", "description": "docstring missing"}],
              "human": {"decision_type": "blocked", "question": "Should the environment be fixed to provide `python`? It cannot be found.",
                        "options": ["a"], "recommendation": 0}}
    assert review_complains_about_sandbox(review)
    stripped, changed = strip_sandbox_complaints(review)
    assert changed and stripped["status"] == "continue" and stripped["human"] is None
    assert [i["description"] for i in stripped["issues"]] == ["docstring missing"]
    assert "orchestrator ran the check" in stripped["next_prompt"]

    clean = {"status": "done", "summary": "s", "milestone": "m", "next_prompt": None, "issues": [], "human": None}
    assert not review_complains_about_sandbox(clean) and strip_sandbox_complaints(clean) == (clean, False)
