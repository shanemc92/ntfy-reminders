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

# ── Configuration ────────────────────────────────────────────────────────────
NTFY_BASE_URL = 'https://ntfy.sh'        # Change to your ntfy server URL
TOPIC         = 'your_topic_here'        # Change to your ntfy topic
NTFY_TITLE    = 'ntfy Scheduler'
DATA_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reminders.json')
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


def send_notification(reminder: dict) -> bool:
    url = f"{NTFY_BASE_URL}/{TOPIC}"
    headers = {
        'Title':    NTFY_TITLE,
        'Priority': str(reminder.get('priority', 3)),
        'Tags':     reminder.get('tag', 'mailbox_with_mail'),
    }
    try:
        resp = requests.post(url, data=reminder['message'].encode('utf-8'),
                             headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send reminder {reminder['id']}: {e}")
        return False


def main():
    if not os.path.exists(DATA_FILE):
        return

    with open(DATA_FILE) as f:
        reminders = json.load(f)

    now = int(time.time())
    kept = []

    for r in reminders:
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
        # else: one-time reminder, discard

    with open(DATA_FILE, 'w') as f:
        json.dump(kept, f, indent=2)


if __name__ == '__main__':
    main()
