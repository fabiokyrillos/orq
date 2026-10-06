"""Rate-limit reset times for Claude (SPEC 12, Phase 0 section 5).

The CLI reports `resets 10pm (America/Cayenne)`: a clock time plus zone, no date. Every stream-json run
also emits a `rate_limit_event` with an epoch `resetsAt`, which is preferred when it points to the future.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orq.adapters.base import AgentResult

_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.IGNORECASE)
DEFAULT_BACKOFF = timedelta(minutes=15)


def parse_claude_reset(text: str, *, now: datetime) -> datetime | None:
    match = _RESET_RE.search(text or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem, zone = match.group(3).lower(), match.group(4).strip()
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def claude_reset_time(result: AgentResult, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    info = result.rate_limit or {}
    resets_at = info.get("resetsAt")
    if isinstance(resets_at, (int, float)):
        candidate = datetime.fromtimestamp(resets_at, tz=timezone.utc)
        if candidate > now:
            return candidate
    parsed = parse_claude_reset(result.error or result.text or "", now=now)
    return parsed if parsed else now + DEFAULT_BACKOFF
