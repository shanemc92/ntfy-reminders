#!/usr/bin/env python3
"""
ntfy Dispatcher — run every minute via cron.
Fires due reminders, reschedules recurring ones, removes one-time ones.

Cron setup (edit with `crontab -e`):
  * * * * * /usr/bin/python3 /path/to/ntfy_dispatch.py >> /path/to/ntfy_dispatch.log 2>&1
"""

import json
import os
import time
import calendar
import requests
from datetime import datetime
from dotenv import load_dotenv

# ── Configuration ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

NTFY_BASE_URL   = os.environ.get('NTFY_BASE_URL', 'https://ntfy.sh')
TOPIC           = os.environ['NTFY_TOPIC']       # required — no fallback
NTFY_TITLE      = 'ntfy Scheduler'                # fallback if reminder has no title
SCHEDULER_URL   = os.environ.get('NTFY_SCHEDULER_URL', '').rstrip('/')
ICON_URL        = os.environ.get('NTFY_ICON_URL', '').strip()
DATA_FILE       = os.path.join(BASE_DIR, 'data', 'reminders.json')
# ─────────────────────────────────────────────────────────────────────────────


def advance_time(ts: int, interval_type: str, interval_value: int) -> int:
    """Return the next fire Unix timestamp after advancing by the given interval."""
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


def ascii_safe(s: str) -> str:
    """HTTP headers must be Latin-1; strip anything outside that range (e.g. emoji)
    so a title with non-Latin characters can't silently break the request."""
    return s.encode('latin-1', errors='ignore').decode('latin-1').strip()


def send_notification(reminder: dict) -> bool:
    url = f"{NTFY_BASE_URL}/{TOPIC}"
    headers = {
        'Title':    ascii_safe(reminder.get('title') or NTFY_TITLE) or NTFY_TITLE,
        'Priority': str(reminder.get('priority', 3)),
    }
    if reminder.get('tag'):
        headers['Tags'] = reminder['tag']
    if ICON_URL:
        headers['Icon'] = ICON_URL
    if SCHEDULER_URL:
        headers['Actions'] = f"view, Reschedule, {SCHEDULER_URL}/?reschedule={reminder['id']}"
    try:
        resp = requests.post(url, data=reminder['message'].encode('utf-8'),
                             headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send reminder {reminder['id']}: {e}")
        return False


RESCHEDULE_GRACE_SECONDS = 24 * 3600  # how long a fired one-time reminder stays reschedulable


def main():
    if not os.path.exists(DATA_FILE):
        return

    with open(DATA_FILE) as f:
        reminders = json.load(f)

    now = int(time.time())
    kept = []

    for r in reminders:
        if r.get('fired') and not r.get('recurring'):
            # Already fired, one-time — keep briefly so the Reschedule
            # action button still resolves, but never re-fire it.
            if now - r.get('fired_at', 0) < RESCHEDULE_GRACE_SECONDS:
                kept.append(r)
            continue

        if r['next_fire'] > now:
            kept.append(r)
            continue

        # Due — fire it
        fired = send_notification(r)
        ts = datetime.fromtimestamp(r['next_fire']).strftime('%Y-%m-%d %H:%M')
        status = 'sent' if fired else 'failed'
        print(f"[{status.upper()}] [{ts}] {r['message'][:60]}")

        if r.get('recurring') and r.get('interval_type') and r.get('interval_value'):
            # Advance to next occurrence (skip any that are also in the past)
            next_ts = advance_time(r['next_fire'], r['interval_type'], r['interval_value'])
            while next_ts <= now:
                next_ts = advance_time(next_ts, r['interval_type'], r['interval_value'])
            r['next_fire'] = next_ts
            kept.append(r)
        else:
            # One-time reminder: keep briefly (see above) instead of discarding
            r['fired'] = True
            r['fired_at'] = now
            kept.append(r)

    with open(DATA_FILE, 'w') as f:
        json.dump(kept, f, indent=2)


if __name__ == '__main__':
    main()
