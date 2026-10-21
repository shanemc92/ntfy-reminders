"""End-to-end check of the container, run by .github/workflows/docker.yml.

Expects the image running with --network host and
    NTFY_TOPIC=ci-topic  NTFY_BASE_URL=http://127.0.0.1:9999
    NTFY_REMINDERS_URL=http://reminders.test:5000
Stands in for ntfy on :9999, books two reminders that are already due, then
waits for the dispatcher loop inside the container to deliver them. Standard
library only. Not named test*.py, so `unittest discover` does not pick it up."""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

APP = 'http://127.0.0.1:5000'
STUB_PORT = 9999
WAIT_SECONDS = 100          # the loop wakes at the top of every minute

received = []               # (path, headers, body) for each POST the stub gets


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode('utf-8')
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

    deadline = time.time() + WAIT_SECONDS
    while len(received) < 2 and time.time() < deadline:
        time.sleep(1)
    assert len(received) == 2, f'dispatcher delivered {len(received)} of 2 within {WAIT_SECONDS}s'

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
    assert state[titled].get('fired') and state[plain].get('fired'), 'reminders not marked fired'
    print('smoke test passed')


if __name__ == '__main__':
    main()
