from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from orq.adapters.base import AgentResult
from orq.core.ratelimit import claude_reset_time, parse_claude_reset

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)  # 12:00 in America/Cayenne (UTC-3, no DST)


def test_parse_pm_time_today() -> None:
    reset = parse_claude_reset("You've hit your session limit · resets 10pm (America/Cayenne)", now=NOW)
    assert reset == datetime(2026, 10, 6, 22, 0, tzinfo=ZoneInfo("America/Cayenne"))


def test_parse_minutes_and_wrap_to_tomorrow() -> None:
    reset = parse_claude_reset("resets 5:10am (America/Cayenne)", now=NOW)
    assert reset.astimezone(timezone.utc) == datetime(2026, 10, 7, 8, 10, tzinfo=timezone.utc)


def test_parse_noon_and_midnight() -> None:
    assert parse_claude_reset("resets 12pm (UTC)", now=NOW).hour == 12
    assert parse_claude_reset("resets 12am (UTC)", now=NOW) == datetime(2026, 10, 7, 0, 0, tzinfo=ZoneInfo("UTC"))


def test_parse_unknown_returns_none() -> None:
    assert parse_claude_reset("API Error: 529 Overloaded", now=NOW) is None
    assert parse_claude_reset("resets 10pm (Mars/Olympus)", now=NOW) is None


def test_reset_time_prefers_stream_event() -> None:
    result = AgentResult(ok=False, error="resets 10pm (America/Cayenne)", error_kind="rate_limit",
                         rate_limit={"status": "rejected", "resetsAt": int(NOW.timestamp()) + 600})
    assert claude_reset_time(result, now=NOW) == NOW + timedelta(seconds=600)


def test_reset_time_falls_back_to_text_then_default() -> None:
    result = AgentResult(ok=False, error="You've hit your session limit · resets 10pm (America/Cayenne)", error_kind="rate_limit")
    assert claude_reset_time(result, now=NOW).astimezone(timezone.utc) == datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    assert claude_reset_time(AgentResult(ok=False, error="rate limit", error_kind="rate_limit"), now=NOW) == NOW + timedelta(minutes=15)


def test_stale_stream_event_in_the_past_is_ignored() -> None:
    result = AgentResult(ok=False, error="rate limit", error_kind="rate_limit", rate_limit={"resetsAt": int(NOW.timestamp()) - 5})
    assert claude_reset_time(result, now=NOW) == NOW + timedelta(minutes=15)
