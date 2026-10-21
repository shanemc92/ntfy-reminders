"""End-to-end check of the container, run by .github/workflows/docker.yml.

Expects the image running with --network host and
    NTFY_TOPIC=ci-topic  NTFY_BASE_URL=http://127.0.0.1:9999
    NTFY_REMINDERS_URL=http://reminders.test:5000
Stands in for ntfy on :9999, books three reminders that are already due, then
waits for the dispatcher loop inside the container to deliver them. One of the
three is refused (HTTP 500) the first time, to check that it is retried on the
next run instead of being lost. Standard library only. Not named test*.py, so
`unittest discover` does not pick it up."""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

APP = 'http://127.0.0.1:5000'
STUB_PORT = 9999
WAIT_SECONDS = 150          # the loop wakes at the top of every minute; the retry needs a second one

received = []               # (path, headers, body) for each POST the stub accepts
attempts = {}               # body -> how many times the stub was asked, accepted or not


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode('utf-8')
        attempts[body] = attempts.get(body, 0) + 1
        if body == 'smoke retry' and attempts[body] == 1:
            self.send_response(500)
            self.end_headers()
            return
        received.append((self.path, self.headers, body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *args):
        pass


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(APP + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode('utf-8')


def book(message, **extra):
    status, body = call('POST', '/schedule',
                        {'message': message, 'next_fire': int(time.time()) - 5, **extra})
    assert status == 201, (status, body)
    return json.loads(body)['id']


def main():
    stub = HTTPServer(('127.0.0.1', STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    status, page = call('GET', '/')
    assert status == 200 and '<title>ntfy-reminders</title>' in page, 'UI page not served'

    titled = book('smoke titled', title='CI', priority=4)
    plain = book('smoke plain')
    retried = book('smoke retry')

    deadline = time.time() + WAIT_SECONDS
    while len(received) < 3 and time.time() < deadline:
        time.sleep(1)
    assert len(received) == 3, f'dispatcher delivered {len(received)} of 3 within {WAIT_SECONDS}s: {attempts}'
    assert attempts == {'smoke titled': 1, 'smoke plain': 1, 'smoke retry': 2}, attempts   # no repeats, one retry

    by_body = {body: (path, headers) for path, headers, body in received}
    path, headers = by_body['smoke titled']
    assert path == '/ci-topic', path
    assert headers['Title'] == 'CI' and headers['Priority'] == '4', dict(headers)
    assert headers['Actions'] == f'view, Reschedule, http://reminders.test:5000/?reschedule={titled}', headers['Actions']

    path, headers = by_body['smoke plain']
    assert headers['Title'] == 'ntfy Reminders', headers['Title']      # fallback when a reminder has none
    assert headers['Actions'].endswith(f'?reschedule={plain}'), headers['Actions']

    _, body = call('GET', '/reminders')
    state = {r['id']: r for r in json.loads(body)}
    assert all(state[i].get('fired') for i in (titled, plain, retried)), 'reminders not marked fired'
    print('smoke test passed')


if __name__ == '__main__':
    main()
