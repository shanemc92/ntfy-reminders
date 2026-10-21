#!/usr/bin/env python3
"""
ntfy Reminders Server
Serves the HTML UI and stores reminders to reminders.json.
Run with: python3 ntfy_server.py
"""

import json
import os
import time
import uuid
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

from ntfy_schedule import first_allowed, zone
from ntfy_store import locked, write_json

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

STATIC_DIR  = os.path.join(BASE_DIR, 'static')
DATA_DIR    = os.path.join(BASE_DIR, 'data')
DATA_FILE   = os.path.join(DATA_DIR, 'reminders.json')
TITLES_FILE = os.path.join(DATA_DIR, 'titles.json')
HTML_FILE   = 'ntfy.html'
ICON_URL    = os.environ.get('NTFY_ICON_URL', '').strip()

os.makedirs(DATA_DIR, exist_ok=True)


def locked_data():
    """Held across every load-modify-save below, so two requests, or a request
    and the dispatcher's write-back, can't overwrite each other's change. One
    lock covers titles.json too: it sits beside reminders.json.
    Not re-entrant, so take it in the route and never inside load_*/save_*."""
    return locked(DATA_FILE)


def load_reminders():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE) as f:
        return json.load(f)


def save_reminders(data):
    write_json(DATA_FILE, data)


def load_titles():
    if not os.path.exists(TITLES_FILE):
        # First run: seed from .env if present, then never touch .env again
        seeded = [{'name': t.strip(), 'tag': ''} for t in os.environ.get('NTFY_TITLES', '').split(',') if t.strip()]
        save_titles(seeded)
        return seeded
    with open(TITLES_FILE) as f:
        titles = json.load(f)
    # Migrate legacy plain-string entries to {name, tag} objects
    return [t if isinstance(t, dict) else {'name': t, 'tag': ''} for t in titles]


def save_titles(titles):
    write_json(TITLES_FILE, titles)


def parse_int(value, default=None, lo=None, hi=None):
    """Safely coerce to int, clamping to [lo, hi]. Returns default on missing/invalid input."""
    if value is None or value == '':
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def apply_days(r, data):
    """Set the weekday filter (r['days'], r['tz']) on reminder `r` from the
    request body, and move r['next_fire'] onto an allowed day. Returns an
    error message, or None.

    days is a sorted list of 0 (Mon) .. 6 (Sun), or None for every day, which
    is also what all seven collapse to. Only hourly/daily repeats take one:
    weekly and monthly repeats already land on a single fixed day. When
    editing, `r` still holds the stored filter, which is kept if `data`
    doesn't mention one; an explicit null clears it.

    tz is the browser's IANA zone, because "which weekday is it" is the user's
    answer rather than the server's. Unknown zones are dropped (-> server time).
    A first fire on an excluded day moves to the next allowed one.
    """
    days = tz = None
    if r['recurring'] and r['interval_type'] in ('hours', 'days') and r['interval_value']:
        days = data.get('days', r.get('days'))
        if days is not None:
            if not (isinstance(days, list) and days and all(type(d) is int and 0 <= d <= 6 for d in days)):
                return 'days must be a list of weekdays, 0 (Mon) to 6 (Sun)'
            days = sorted(set(days))
            if len(days) == 7:
                days = None
    if days:
        tz = data.get('tz', r.get('tz'))
        tz = tz if isinstance(tz, str) and zone(tz) else None
        first = first_allowed(r['next_fire'], r['interval_type'], r['interval_value'], days, zone(tz))
        if first is None:
            return 'That repeat interval never lands on the selected days'
        r['next_fire'] = first
    r['days'], r['tz'] = days, tz
    return None


@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, HTML_FILE)


@app.route('/titles', methods=['GET'])
def get_titles():
    with locked_data():     # the first call seeds titles.json, which is a write
        return jsonify(load_titles())


@app.route('/titles', methods=['POST'])
def add_title():
    data = request.get_json()
    name = (data or {}).get('title', '').strip()
    tag = (data or {}).get('tag', '').strip()
    if not name:
        return jsonify({'error': 'Title required'}), 400
    with locked_data():
        titles = load_titles()
        titles.append({'name': name, 'tag': tag})
        save_titles(titles)
    return jsonify(titles), 201


@app.route('/titles/<int:idx>', methods=['PUT'])
def edit_title(idx):
    data = request.get_json()
    name = (data or {}).get('title', '').strip()
    tag = (data or {}).get('tag', '').strip()
    if not name:
        return jsonify({'error': 'Title required'}), 400
    with locked_data():
        titles = load_titles()
        if idx < 0 or idx >= len(titles):
            return jsonify({'error': 'Not found'}), 404
        titles[idx] = {'name': name, 'tag': tag}
        save_titles(titles)
    return jsonify(titles)


@app.route('/titles/<int:idx>', methods=['DELETE'])
def delete_title(idx):
    with locked_data():
        titles = load_titles()
        if idx < 0 or idx >= len(titles):
            return jsonify({'error': 'Not found'}), 404
        titles.pop(idx)
        save_titles(titles)
    return jsonify(titles)


@app.route('/schedule', methods=['POST'])
def schedule():
    data = request.get_json()
    if not data or 'message' not in data or 'next_fire' not in data:
        return jsonify({'error': 'Missing required fields'}), 400

    next_fire = parse_int(data['next_fire'])
    if next_fire is None:
        return jsonify({'error': 'Invalid next_fire'}), 400

    reminder = {
        'id': str(uuid.uuid4()),
        'message': data['message'],
        'priority': parse_int(data.get('priority'), default=3, lo=1, hi=5),
        'title': data.get('title', ''),
        'tag': data.get('tag', ''),
        'next_fire': next_fire,
        'recurring': bool(data.get('recurring', False)),
        'interval_type': data.get('interval_type'),   # hours/days/weeks/months
        'interval_value': parse_int(data.get('interval_value'), lo=1),
        'created': int(time.time()),
    }
    err = apply_days(reminder, data)
    if err:
        return jsonify({'error': err}), 400
    with locked_data():
        reminders = load_reminders()
        reminders.append(reminder)
        save_reminders(reminders)
    return jsonify({'status': 'ok', 'id': reminder['id'], 'next_fire': reminder['next_fire']}), 201


@app.route('/reminders', methods=['GET'])
def get_reminders():
    reminders = load_reminders()
    # Sort by next_fire ascending
    reminders.sort(key=lambda r: r['next_fire'])
    return jsonify(reminders)


@app.route('/reminders/<reminder_id>', methods=['PUT'])
def update_reminder(reminder_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    with locked_data():
        reminders = load_reminders()
        for r in reminders:
            if r['id'] == reminder_id:
                r['message']        = data.get('message', r['message'])
                r['priority']       = parse_int(data.get('priority'), default=r['priority'], lo=1, hi=5)
                r['next_fire']      = parse_int(data.get('next_fire'), default=r['next_fire'])
                r['recurring']      = bool(data.get('recurring', r['recurring']))
                r['interval_type']  = data.get('interval_type', r['interval_type'])
                r['interval_value'] = parse_int(data.get('interval_value'), default=r.get('interval_value'), lo=1)
                r['title']          = data.get('title', r.get('title', ''))
                r['tag']            = data.get('tag', r.get('tag', ''))
                r['fired']          = False
                err = apply_days(r, data)
                if err:
                    return jsonify({'error': err}), 400
                save_reminders(reminders)
                return jsonify({'status': 'ok', 'next_fire': r['next_fire']})
    return jsonify({'error': 'Not found'}), 404


@app.route('/reminders/<reminder_id>', methods=['DELETE'])
def delete_reminder(reminder_id):
    with locked_data():
        reminders = [r for r in load_reminders() if r['id'] != reminder_id]
        save_reminders(reminders)
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    # Loopback by default. Only widen this to a LAN address once a firewall rule
    # limits port 5000 to the reverse proxy - the app has no login of its own.
    bind_host = os.environ.get('BIND_HOST', '127.0.0.1')
    print(f"Starting ntfy Reminders on http://{bind_host}:5000")
    print(f"Reminders stored in: {DATA_FILE}")
    app.run(host=bind_host, port=5000, debug=False)
