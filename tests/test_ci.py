from pathlib import Path

from orq.git.manager import GitError
from orq.verify.ci import CiWatcher

LINK = "https://github.com/o/r/actions/runs/123/job/456"


class ScriptedGh:
    """Answers gh calls from a per-command queue; records every call."""

    def __init__(self, checks: list, run_view: list | None = None) -> None:
        self.checks = list(checks)
        self.run_view = list(run_view or [])
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path) -> str:
        self.calls.append(args)
        if args[:2] == ["pr", "checks"]:
            item = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
            if isinstance(item, Exception):
                raise item
            return item
        if args[:2] == ["run", "view"] and "--log-failed" in args:
            return "x" * 10 + "tail of the failed log"
        if args[:2] == ["run", "view"]:
            return self.run_view.pop(0) if len(self.run_view) > 1 else self.run_view[0]
        if args[:2] == ["run", "rerun"]:
            return ""
        raise AssertionError(f"unexpected gh call {args}")


def watcher(gh, **overrides) -> tuple[CiWatcher, list[float], list[float]]:
    slept: list[float] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    kwargs = dict(poll_seconds=20, timeout_minutes=60, grace_minutes=5, max_reruns=3)
    kwargs.update(overrides)
    return CiWatcher(gh, sleep=sleep, clock=lambda: now[0], **kwargs), slept, now


def test_pending_then_success(tmp_path: Path) -> None:
    gh = ScriptedGh([GitError("no checks reported on the branch"), '[{"name":"check","state":"QUEUED","bucket":"pending","link":""}]',
                     '[{"name":"check","state":"SUCCESS","bucket":"pass","link":"%s"}]' % LINK])
    w, slept, _ = watcher(gh)
    status = w.wait(tmp_path, 7)
    assert status.state == "success" and status.ok and slept == [20, 20]
    assert gh.calls[0] == ["pr", "checks", "7", "--json", "name,state,bucket,link"]


def test_skipped_checks_count_as_passing(tmp_path: Path) -> None:
    gh = ScriptedGh(['[{"name":"a","bucket":"pass","link":""},{"name":"b","bucket":"skipping","link":""}]'])
    w, _, _ = watcher(gh)
    assert w.wait(tmp_path, 1).state == "success"


def test_no_checks_after_grace_period_is_none(tmp_path: Path) -> None:
    gh = ScriptedGh([GitError("no checks reported")])
    w, slept, _ = watcher(gh, grace_minutes=1)
    status = w.wait(tmp_path, 1)
    assert status.state == "none" and len(slept) == 4  # 0, 20, 40, 60 then > 60


def test_other_gh_errors_propagate(tmp_path: Path) -> None:
    gh = ScriptedGh([GitError("HTTP 500")])
    w, _, _ = watcher(gh)
    try:
        w.wait(tmp_path, 1)
    except GitError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("expected GitError")


def test_infra_failure_is_rerun_then_success(tmp_path: Path) -> None:
    failing = '[{"name":"check","state":"FAILURE","bucket":"fail","link":"%s"}]' % LINK
    passing = '[{"name":"check","state":"SUCCESS","bucket":"pass","link":"%s"}]' % LINK
    gh = ScriptedGh([failing, failing, passing], run_view=['{"conclusion":"failure","jobs":[{"name":"check","steps":[]}]}'])
    w, slept, _ = watcher(gh)
    status = w.wait(tmp_path, 1)
    assert status.state == "success" and status.reruns == 1
    assert ["run", "rerun", "123"] in gh.calls and gh.calls.count(["run", "rerun", "123"]) == 1


def test_real_failure_returns_log(tmp_path: Path) -> None:
    failing = '[{"name":"check","state":"FAILURE","bucket":"fail","link":"%s"}]' % LINK
    gh = ScriptedGh([failing], run_view=['{"conclusion":"failure","jobs":[{"name":"check","steps":[{"name":"pytest","conclusion":"failure"}]}]}'])
    w, slept, _ = watcher(gh)
    status = w.wait(tmp_path, 1)
    assert status.state == "failure" and status.failed_log.endswith("tail of the failed log") and slept == []
    assert ["run", "view", "123", "--log-failed"] in gh.calls


def test_reruns_are_bounded(tmp_path: Path) -> None:
    failing = '[{"name":"check","state":"FAILURE","bucket":"fail","link":"%s"}]' % LINK
    gh = ScriptedGh([failing], run_view=['{"conclusion":"failure","jobs":[]}'])
    w, _, _ = watcher(gh, max_reruns=1)
    status = w.wait(tmp_path, 1)
    assert status.state == "failure" and status.reruns == 1


def test_timeout(tmp_path: Path) -> None:
    gh = ScriptedGh(['[{"name":"check","state":"QUEUED","bucket":"pending","link":""}]'])
    w, slept, _ = watcher(gh, timeout_minutes=1)
    status = w.wait(tmp_path, 1)
    assert status.state == "timeout" and len(slept) == 3 and status.waited_seconds == 60
