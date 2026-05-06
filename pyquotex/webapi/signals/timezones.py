"""Parse signal-message timezone hints and convert local clock times
to UTC.

Signals quote times like ``13:30`` in a channel-specific UTC offset
(e.g. ``UTC -3``, ``UTC/GMT (+06:00)``). We turn those clock-times
plus a date plus an offset into a UTC ``datetime``.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

_RE_OFFSET = re.compile(r"^\s*([+-]?)(\d{1,2})(?::?(\d{2}))?\s*$")


def parse_tz_offset(text: str) -> float:
    """Return UTC offset in hours (float, may be fractional).

    Accepts ``"-3"``, ``"+5:30"``, ``"+06:00"``, ``"3"``, etc.
    Raises ``ValueError`` on unparseable input.
    """
    m = _RE_OFFSET.match(text or "")
    if not m:
        raise ValueError(f"Bad UTC offset: {text!r}")
    sign = -1 if m.group(1) == "-" else 1
    hours = int(m.group(2))
    minutes = int(m.group(3) or 0)
    return sign * (hours + minutes / 60.0)


def today_in_tz(offset_hours: float) -> date:
    """Return today's date as it appears in the given timezone."""
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.now(tz=tz).date()


def signal_to_utc(
    clock: time, signal_date: date, offset_hours: float
) -> datetime:
    """Convert a local clock-time + date in ``offset_hours`` zone to a
    timezone-aware UTC ``datetime``."""
    tz = timezone(timedelta(hours=offset_hours))
    local = datetime.combine(signal_date, clock, tzinfo=tz)
    return local.astimezone(timezone.utc)
