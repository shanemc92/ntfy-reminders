#!/usr/bin/env python3
"""
ntfy Scheduler Server
Serves the HTML UI and stores reminders to reminders.json.
Run with: python3 ntfy_server.py
"""

import json
import os
import time
import uuid
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'reminders.json')
HTML_FILE = 'ntfy.html'


def load_reminders():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE) as f:
        return json.load(f)


def save_reminders(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, HTML_FILE)


@app.route('/schedule', methods=['POST'])
def schedule():
    data = request.get_json()
    if not data or 'message' not in data or 'next_fire' not in data:
        return jsonify({'error': 'Missing required fields'}), 400

    reminder = {
        'id': str(uuid.uuid4()),
        'message': data['message'],
        'priority': int(data.get('priority', 3)),
        'tag': data.get('tag', 'mailbox_with_mail'),
        'next_fire': int(data['next_fire']),
        'recurring': bool(data.get('recurring', False)),
        'interval_type': data.get('interval_type'),   # hours/days/weeks/months
        'interval_value': int(data['interval_value']) if data.get('interval_value') else None,
        'created': int(time.time()),
    }
    reminders = load_reminders()
    reminders.append(reminder)
    save_reminders(reminders)
    return jsonify({'status': 'ok', 'id': reminder['id']}), 201


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
    reminders = load_reminders()
    for r in reminders:
        if r['id'] == reminder_id:
            r['message']        = data.get('message', r['message'])
            r['priority']       = int(data.get('priority', r['priority']))
            r['next_fire']      = int(data.get('next_fire', r['next_fire']))
            r['recurring']      = bool(data.get('recurring', r['recurring']))
            r['interval_type']  = data.get('interval_type', r['interval_type'])
            r['interval_value'] = int(data['interval_value']) if data.get('interval_value') else None
            r['tag']            = data.get('tag', r['tag'])
            save_reminders(reminders)
            return jsonify({'status': 'ok'})
    return jsonify({'error': 'Not found'}), 404


@app.route('/reminders/<reminder_id>', methods=['DELETE'])
def delete_reminder(reminder_id):
    reminders = [r for r in load_reminders() if r['id'] != reminder_id]
    save_reminders(reminders)
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    print(f"Starting ntfy Scheduler on http://localhost:5000")
    print(f"Reminders stored in: {DATA_FILE}")
    app.run(host='0.0.0.0', port=5000, debug=False)
