import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from orq.adapters.base import AgentResult
from orq.adapters.router import DEFAULT_FALLBACK_SECONDS, ReviewerRouter

GOOD = {"status": "continue", "summary": "s", "milestone": "m", "next_prompt": "n", "issues": [], "human": None}


@dataclass
class Scripted:
    name: str
    results: list[AgentResult]
    calls: int = 0
    sessions: list = field(default_factory=list)
    models: list = field(default_factory=list)

    async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, **kwargs):
        self.calls += 1
        self.sessions.append(session_id)
        self.models.append(kwargs.get("model"))
        return self.results.pop(0)


def run(router: ReviewerRouter, tmp_path: Path) -> AgentResult:
    return asyncio.run(router.run("p", cwd=tmp_path, log_path=tmp_path / "l.jsonl", session_id="codex-thread"))


def test_primary_used_by_default(tmp_path: Path) -> None:
    primary, fallback = Scripted("codex", [AgentResult(ok=True, structured=GOOD)]), Scripted("claude", [])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    assert run(router, tmp_path).ok and primary.calls == 1 and fallback.calls == 0
    assert router.fallback_until is None and router.name == "codex"


def test_reactive_switch_retries_on_fallback_without_the_primary_session(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="usage limit", error_kind="rate_limit",
                                             rate_limit={"primary": {"resets_at": 5000, "used_percent": 100.0}})])
    fallback = Scripted("claude", [AgentResult(ok=True, structured=GOOD)])
    events: list = []
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0, on_switch=events.append)

    result = run(router, tmp_path)

    assert result.ok and fallback.calls == 1 and fallback.sessions == [None]
    assert router.fallback_until == 5000 and events == [("switched", "claude", 5000.0)]


def test_proactive_switch_after_threshold(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=True, structured=GOOD, rate_limit={"primary": {"used_percent": 92.0, "resets_at": 5000}})])
    fallback = Scripted("claude", [AgentResult(ok=True, structured=GOOD)])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)

    assert run(router, tmp_path).ok and primary.calls == 1 and router.fallback_until == 5000
    assert run(router, tmp_path).ok and fallback.calls == 1 and router.name == "claude"


def test_below_threshold_stays_on_primary(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=True, structured=GOOD, rate_limit={"primary": {"used_percent": 50.0, "resets_at": 5000}})])
    router = ReviewerRouter(primary, Scripted("claude", []), switch_at_used_percent=90, clock=lambda: 1000.0)
    assert run(router, tmp_path).ok and router.fallback_until is None


def test_restore_after_reset(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=True, structured=GOOD)])
    events: list = []
    router = ReviewerRouter(primary, Scripted("claude", []), switch_at_used_percent=90, clock=lambda: 6000.0,
                            fallback_until=5000, on_switch=events.append)
    assert run(router, tmp_path).ok and primary.calls == 1 and router.fallback_until is None
    assert events == [("restored", "codex", None)]


def test_switch_without_reset_time_uses_default_window(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="usage limit", error_kind="rate_limit")])
    fallback = Scripted("claude", [AgentResult(ok=True, structured=GOOD)])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    assert run(router, tmp_path).ok and router.fallback_until == 1000.0 + DEFAULT_FALLBACK_SECONDS


def test_fallback_rate_limit_is_returned_as_is(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="usage limit", error_kind="rate_limit")])
    fallback = Scripted("claude", [AgentResult(ok=False, error="hit your limit", error_kind="rate_limit")])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    result = run(router, tmp_path)
    assert not result.ok and result.error_kind == "rate_limit"


def test_other_primary_errors_are_not_rerouted(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="boom", error_kind="error")])
    fallback = Scripted("claude", [])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    result = run(router, tmp_path)
    assert not result.ok and fallback.calls == 0 and router.fallback_until is None


def test_router_forwards_effort_and_contract(tmp_path: Path) -> None:
    seen: list[dict] = []

    @dataclass
    class Recording:
        name: str
        result: AgentResult

        async def run(self, prompt, *, cwd, log_path, session_id=None, run_dir=None, on_event=None, model=None, effort=None, contract=None):
            seen.append({"name": self.name, "effort": effort, "contract": contract, "session": session_id})
            return self.result

    primary = Recording("codex", AgentResult(ok=False, error="usage limit", error_kind="rate_limit"))
    fallback = Recording("claude", AgentResult(ok=True, structured=GOOD))
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)
    asyncio.run(router.run("p", cwd=tmp_path, log_path=tmp_path / "l.jsonl", session_id="s", effort="high", contract="PLAN"))
    assert seen == [{"name": "codex", "effort": "high", "contract": "PLAN", "session": "s"},
                    {"name": "claude", "effort": "high", "contract": "PLAN", "session": None}]


def test_models_are_routed_to_the_agent_that_runs(tmp_path: Path) -> None:
    primary = Scripted("codex", [AgentResult(ok=False, error="usage limit", error_kind="rate_limit")])
    fallback = Scripted("claude", [AgentResult(ok=True, structured=GOOD)])
    router = ReviewerRouter(primary, fallback, switch_at_used_percent=90, clock=lambda: 1000.0)

    asyncio.run(router.run("p", cwd=tmp_path, log_path=tmp_path / "l.jsonl", model="gpt-6.1-sol", fallback_model="sonnet"))

    assert primary.models == ["gpt-6.1-sol"] and fallback.models == ["sonnet"]
    assert router.kind == "router"
