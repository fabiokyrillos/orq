"""Phase 0 probe: search local Claude Code and Codex session history for real usage-limit errors.

Prints only distinct, truncated error strings and counts. No conversation content is printed.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

HOME = Path.home()
LIMIT_RE = re.compile(r"usage limit|limit reached|hit your|rate.?limit|too many requests|quota|overloaded|resets? (at|in)", re.I)
DIGITS = re.compile(r"\d")


def normalize(text: str) -> str:
    return DIGITS.sub("N", " ".join(text.split()))[:220]


def scan_claude() -> None:
    api_errors: Counter[str] = Counter()
    raw_examples: dict[str, str] = {}
    files = list((HOME / ".claude" / "projects").rglob("*.jsonl"))
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"isApiErrorMessage":true' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = entry.get("message", {}).get("content", [])
                    text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    key = normalize(text)
                    api_errors[key] += 1
                    if LIMIT_RE.search(text) and key not in raw_examples:
                        keep = {k: entry.get(k) for k in ("error", "apiError", "isApiErrorMessage", "type") if k in entry}
                        raw_examples[key] = text[:300] + "  || fields: " + json.dumps(keep)
        except OSError:
            continue
    print(f"CLAUDE: {len(files)} transcript files, {sum(api_errors.values())} API error messages")
    for key, count in api_errors.most_common(25):
        flag = "LIMIT" if LIMIT_RE.search(key) else "other"
        print(f"  [{flag}] x{count}: {key}")
    print("  raw limit examples:")
    for text in raw_examples.values():
        print("   ", text)


def scan_codex() -> None:
    errors: Counter[str] = Counter()
    rate_limit_sample = None
    files = list((HOME / ".codex" / "sessions").rglob("*.jsonl"))
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if rate_limit_sample is None and '"rate_limits"' in line and '"used_percent"' in line:
                        try:
                            payload = json.loads(line).get("payload", {})
                            rate_limit_sample = json.dumps(payload.get("rate_limits"))[:600]
                        except json.JSONDecodeError:
                            pass
                    if '"error"' not in line and "usage_limit" not in line:
                        continue
                    try:
                        payload = json.loads(line).get("payload", {})
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict) or payload.get("type") not in ("error", "stream_error", "turn_aborted"):
                        continue
                    message = str(payload.get("message") or payload.get("reason") or "")
                    extra = {k: v for k, v in payload.items() if k not in ("message", "type")}
                    errors[normalize(message) + "  || " + normalize(json.dumps(extra))] += 1
        except OSError:
            continue
    print(f"\nCODEX: {len(files)} session files, {sum(errors.values())} error events")
    for key, count in errors.most_common(25):
        flag = "LIMIT" if LIMIT_RE.search(key) else "other"
        print(f"  [{flag}] x{count}: {key}")
    print("  rate_limits snapshot sample:", rate_limit_sample)


if __name__ == "__main__":
    scan_claude()
    scan_codex()
