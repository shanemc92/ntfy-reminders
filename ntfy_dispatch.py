#!/usr/bin/env python3
"""
ntfy Dispatcher — run every minute via cron.
Fires due reminders, reschedules recurring ones, removes one-time ones.
A reminder that fails to send stays due and is retried every run (see
RETRY_WINDOW_SECONDS), so an ntfy outage doesn't swallow it.

Cron setup (edit with `crontab -e`):
  * * * * * /usr/bin/python3 /path/to/ntfy_dispatch.py >> /path/to/ntfy_dispatch.log 2>&1
"""

import json
import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

from ntfy_schedule import advance_time, zone
from ntfy_store import locked, write_json

# ── Configuration ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

NTFY_BASE_URL   = os.environ.get('NTFY_BASE_URL', 'https://ntfy.sh')
TOPIC           = os.environ['NTFY_TOPIC']       # required — no fallback
NTFY_TITLE      = 'ntfy Reminders'                # fallback if reminder has no title
REMINDERS_URL   = os.environ.get('NTFY_REMINDERS_URL', '').rstrip('/')
ICON_URL        = os.environ.get('NTFY_ICON_URL', '').strip()
DATA_FILE       = os.path.join(BASE_DIR, 'data', 'reminders.json')
# ─────────────────────────────────────────────────────────────────────────────


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
    if REMINDERS_URL:
        headers['Actions'] = f"view, Reschedule, {REMINDERS_URL}/?reschedule={reminder['id']}"
    try:
        resp = requests.post(url, data=reminder['message'].encode('utf-8'),
                             headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send reminder {reminder['id']}: {e}")
        return False


RESCHEDULE_GRACE_SECONDS = 24 * 3600  # how long a fired one-time reminder stays reschedulable
RETRY_WINDOW_SECONDS = 6 * 3600       # how long a reminder that fails to send is retried, every run, before giving up


def is_pending(r):
    """False for a one-time reminder that has already fired."""
    return not (r.get('fired') and not r.get('recurring'))


def settle(r, now):
    """A due reminder has been dealt with (sent, or given up on): move it on."""
    if r.get('recurring') and r.get('interval_type') and r.get('interval_value'):
        # Advance to next occurrence (skip any that are also in the past, or
        # that land on a weekday the reminder is switched off for)
        step = (r['interval_type'], r['interval_value'], r.get('days'), zone(r.get('tz')))
        next_ts = advance_time(r['next_fire'], *step)
        while next_ts <= now:
            next_ts = advance_time(next_ts, *step)
        r['next_fire'] = next_ts
    else:
        # One-time reminder: keep briefly (see above) instead of discarding
        r['fired'] = True
        r['fired_at'] = now


def main():
    if not os.path.exists(DATA_FILE):
        return

    # 1. Read, and pick out what is due. No lock needed: writes are atomic, so
    #    this is always a whole file.
    with open(DATA_FILE) as f:
        reminders = json.load(f)
    now = int(time.time())
    due = [r for r in reminders if is_pending(r) and r['next_fire'] <= now]

    # 2. Send. This is the slow part (a timeout per unreachable send), so the
    #    lock is not held. Outcomes are keyed by id *and* fire time, so an edit
    #    made while we were sending doesn't get settled as if it had been sent.
    outcomes = {}
    for r in due:
        sent = send_notification(r)
        ts = datetime.fromtimestamp(r['next_fire']).strftime('%Y-%m-%d %H:%M')
        print(f"[{'SENT' if sent else 'FAILED'}] [{ts}] {r['message'][:60]}")
        outcomes[(r['id'], r['next_fire'])] = sent

    # 3. Apply the outcomes to the file as it is *now*, under the lock. Whatever
    #    the web UI added, edited or deleted during step 2 is left as it left it.
    if not os.path.exists(DATA_FILE):
        return
    with locked(DATA_FILE):
        with open(DATA_FILE) as f:
            current = json.load(f)
        now = int(time.time())
        kept = []
        changed = False

        for r in current:
            if not is_pending(r):
                # Already fired, one-time — keep briefly so the Reschedule
                # action button still resolves, but never re-fire it.
                if now - r.get('fired_at', 0) < RESCHEDULE_GRACE_SECONDS:
                    kept.append(r)
                else:
                    changed = True
                continue

            sent = outcomes.get((r['id'], r['next_fire']))
            if sent is None:
                kept.append(r)      # not due when we looked, or changed since
                continue
            if not sent:
                if now - r['next_fire'] < RETRY_WINDOW_SECONDS:
                    kept.append(r)  # still due, so the next run tries again
                    continue
                ts = datetime.fromtimestamp(r['next_fire']).strftime('%Y-%m-%d %H:%M')
                print(f"[GAVE UP] [{ts}] {r['message'][:60]}")
            settle(r, now)
            changed = True
            kept.append(r)

        if changed:
            write_json(DATA_FILE, kept)


if __name__ == '__main__':
    main()
