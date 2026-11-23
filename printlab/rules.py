"""Approval rules from the spec.

- Prints of `short_print_minutes` or less are approved automatically.
- Anything submitted at or after `auto_approve_after` (time of day) is approved automatically.
- Everything else waits for a teacher.
- Prints longer than `max_print_hours` are rejected.
"""
from dataclasses import dataclass
from datetime import datetime, time


@dataclass
class Decision:
    status: str   # approved | pending_approval | rejected
    reason: str


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{max(m, 1)} min"


def decide(est_seconds: int, submitted_at: datetime, settings: dict) -> Decision:
    short_min = int(settings.get("short_print_minutes", 30))
    max_hours = float(settings.get("max_print_hours", 8))
    cutoff = parse_hhmm(settings.get("auto_approve_after", "13:30"))

    if max_hours > 0 and est_seconds > max_hours * 3600:
        return Decision("rejected", f"Longer than the {max_hours:g} h limit")
    if est_seconds <= short_min * 60:
        return Decision("approved", f"Auto-approved: {short_min} min or shorter")
    if submitted_at.time() >= cutoff:
        return Decision("approved", f"Auto-approved: submitted after {cutoff.strftime('%-I:%M %p')}")
    return Decision("pending_approval", f"Longer than {short_min} min, needs a teacher")
