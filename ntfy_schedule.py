"""
Schedule arithmetic shared by the cron dispatcher (ntfy_dispatch.py) and the
web server (ntfy_server.py): stepping a recurring reminder forward, and the
optional weekday filter ("daily, but only Mon-Fri").

Kept free of any env/config so either side can import it.
"""

import calendar
from datetime import datetime

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError   # Python 3.9+
except ImportError:                                        # 3.8: weekdays fall back to server time
    ZoneInfo = ZoneInfoNotFoundError = None

MAX_DAY_SKIPS = 500   # > the 168 hourly steps in a week, so any satisfiable filter resolves


def zone(name):
    """The ZoneInfo for an IANA name, or None (-> the server's local zone)."""
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _step(ts: int, interval_type: str, interval_value: int) -> int:
    """One interval after ts, ignoring any weekday filter."""
    dt = datetime.fromtimestamp(ts)

    if interval_type == 'hours':
        new_ts = ts + interval_value * 3600
    elif interval_type == 'days':
        new_ts = ts + interval_value * 86400
    elif interval_type == 'weeks':
        new_ts = ts + interval_value * 7 * 86400
    elif interval_type == 'months':
        month = dt.month - 1 + interval_value
        year  = dt.year + month // 12
        month = month % 12 + 1
        day   = min(dt.day, calendar.monthrange(year, month)[1])
        new_dt = dt.replace(year=year, month=month, day=day)
        new_ts = int(new_dt.timestamp())
    else:
        # Fallback: 24 hours
        new_ts = ts + 86400

    return new_ts


def first_allowed(ts, interval_type, interval_value, days, tz=None):
    """ts itself if it falls on one of `days` (0=Mon .. 6=Sun, read in `tz`),
    else the first later occurrence of the schedule that does. The schedule
    keeps its own rhythm and the excluded days are just masked out of it, so
    "every 3 hours, weekdays" resumes Monday on the same 3-hour grid.

    None if the interval never lands on a selected day (e.g. every 7 days
    from a Monday, Tuesdays only). The server rejects that when a reminder is
    saved; the cap just keeps the dispatcher from ever spinning on it.
    """
    if not days:
        return ts
    for _ in range(MAX_DAY_SKIPS):
        if datetime.fromtimestamp(ts, tz).weekday() in days:
            return ts
        ts = _step(ts, interval_type, interval_value)
    return None


def advance_time(ts: int, interval_type: str, interval_value: int, days=None, tz=None) -> int:
    """Return the next fire Unix timestamp after advancing by the given interval,
    skipping any occurrence that lands on a day outside `days`."""
    nxt = _step(ts, interval_type, interval_value)
    allowed = first_allowed(nxt, interval_type, interval_value, days, tz)
    return nxt if allowed is None else allowed
