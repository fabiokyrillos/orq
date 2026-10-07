"""Windows toast (SPEC 9.4) through winotify, which drives PowerShell's toast API without native dependencies."""

from __future__ import annotations

APP_ID = "orq"


def show_toast(title: str, message: str) -> bool:
    """Show a toast; return False instead of raising when the desktop cannot show one."""
    try:
        from winotify import Notification  # imported lazily: not every environment has a desktop

        Notification(app_id=APP_ID, title=title[:64], msg=message[:200]).show()
        return True
    except Exception:  # noqa: BLE001 - notifications must never break a run
        return False
