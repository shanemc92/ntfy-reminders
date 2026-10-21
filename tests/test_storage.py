"""Run with:  python -m unittest discover -s tests -v
Needs the packages in requirements.txt; sends nothing - the notification call is
stubbed and reminders live in a temp dir.

Covers what keeps reminders.json intact: the atomic write and the lock in
ntfy_store.py, the server taking that lock on every change, and the cron script
(ntfy_dispatch.py) retrying a failed send and merging its result into whatever
the server wrote while it was sending."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
os.environ.setdefault('NTFY_TOPIC', 'test-topic')   # ntfy_dispatch refuses to import without one

import ntfy_dispatch   # noqa: E402
import ntfy_store      # noqa: E402

NOW = 1_800_000_000   # an arbitrary fixed "now"
HOUR = 3600
RETRY = ntfy_dispatch.RETRY_WINDOW_SECONDS


def reminder(rid='r', **kw):
    r = {'id': rid, 'message': 'm', 'priority': 3, 'next_fire': NOW - 60, 'recurring': False,
         'interval_type': None, 'interval_value': None}
    r.update(kw)
    return r


def daily(rid='r', **kw):
    return reminder(rid, recurring=True, interval_type='days', interval_value=1, **kw)


class DispatcherCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'reminders.json')
        self.clock = NOW
        for p in (mock.patch.object(ntfy_dispatch, 'DATA_FILE', self.path),
                  mock.patch.object(ntfy_dispatch.time, 'time', side_effect=lambda: self.clock)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, *reminders):
        with open(self.path, 'w') as f:
            json.dump(list(reminders), f)

    def read(self):
        with open(self.path) as f:
            return {r['id']: r for r in json.load(f)}

    def run_main(self, send):
        """Run the dispatcher once. `send` is a bool, or a function of the reminder."""
        fn = send if callable(send) else (lambda r: send)
        with mock.patch.object(ntfy_dispatch, 'send_notification', side_effect=fn) as m:
            ntfy_dispatch.main()
        return m


class FailedSends(DispatcherCase):
    def test_a_failed_one_time_send_stays_due_and_is_retried(self):
        self.write(reminder())
        self.run_main(False)
        r = self.read()['r']
        self.assertFalse(r.get('fired'))
        self.assertEqual(r['next_fire'], NOW - 60)
        self.run_main(True)                      # the next run, once ntfy is back
        self.assertTrue(self.read()['r']['fired'])

    def test_a_failed_recurring_send_does_not_skip_the_occurrence(self):
        self.write(daily())
        self.run_main(False)
        self.assertEqual(self.read()['r']['next_fire'], NOW - 60)
        self.run_main(True)
        self.assertEqual(self.read()['r']['next_fire'], NOW - 60 + 86400)

    def test_still_retried_just_inside_the_window(self):
        self.write(reminder(next_fire=NOW - RETRY + 60))
        self.run_main(False)
        self.assertFalse(self.read()['r'].get('fired'))

    def test_gives_up_once_the_window_has_passed(self):
        self.write(reminder('once', next_fire=NOW - RETRY - 1), daily('daily', next_fire=NOW - RETRY - 1))
        self.run_main(False)
        state = self.read()
        self.assertTrue(state['once']['fired'])              # not retried for ever
        self.assertGreater(state['daily']['next_fire'], NOW)  # moves on to its next occurrence

    def test_one_failure_does_not_hold_back_the_others(self):
        self.write(reminder('bad'), reminder('good'))
        self.run_main(lambda r: r['id'] == 'good')
        state = self.read()
        self.assertFalse(state['bad'].get('fired'))
        self.assertTrue(state['good']['fired'])

    def test_a_quiet_run_does_not_rewrite_the_file(self):
        self.write(reminder(next_fire=NOW + HOUR))
        with mock.patch.object(ntfy_dispatch, 'write_json') as w:
            self.run_main(True)
        w.assert_not_called()


class ChangesWhileSending(DispatcherCase):
    """The web UI keeps working while the dispatcher is out sending."""

    def test_added_edited_and_deleted_reminders_survive(self):
        self.write(reminder('sent'), reminder('edited'), reminder('gone'), reminder('later', next_fire=NOW + HOUR))
        edits = []

        def send(r):
            if not edits:     # the user acts in the web UI during the first send
                edits.append(True)
                self.write(reminder('sent'),
                           reminder('edited', next_fire=NOW + 2 * HOUR),      # rescheduled
                           reminder('later', next_fire=NOW + HOUR),            # ('gone' deleted)
                           reminder('new', next_fire=NOW + 3 * HOUR))          # added
            return True

        self.run_main(send)
        state = self.read()
        self.assertTrue(state['sent']['fired'])
        self.assertEqual(state['edited']['next_fire'], NOW + 2 * HOUR)   # not clobbered, not marked fired
        self.assertFalse(state['edited'].get('fired'))
        self.assertNotIn('gone', state)                                   # not resurrected
        self.assertIn('new', state)                                       # not lost
        self.assertEqual(state['later']['next_fire'], NOW + HOUR)

    def test_a_reminder_added_already_due_waits_for_the_next_run(self):
        self.write(reminder('first'))

        def send(r):
            self.write(reminder('first'), reminder('late'))
            return True

        m = self.run_main(send)
        self.assertEqual(m.call_count, 1)
        self.assertFalse(self.read()['late'].get('fired'))
        self.run_main(True)
        self.assertTrue(self.read()['late']['fired'])


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, 'x.json')

    def test_write_replaces_the_whole_file_and_leaves_nothing_behind(self):
        ntfy_store.write_json(self.path, [1, 2, 3])
        ntfy_store.write_json(self.path, {'a': 1})
        with open(self.path) as f:
            self.assertEqual(json.load(f), {'a': 1})
        self.assertEqual(os.listdir(self.dir), ['x.json'])

    def test_a_failed_write_keeps_the_old_file(self):
        ntfy_store.write_json(self.path, ['old'])
        with self.assertRaises(TypeError):
            ntfy_store.write_json(self.path, [object()])      # not JSON-serialisable, fails mid-write
        with open(self.path) as f:
            self.assertEqual(json.load(f), ['old'])
        self.assertEqual(os.listdir(self.dir), ['x.json'])

    @unittest.skipUnless(os.name == 'posix', 'file modes')
    def test_the_file_keeps_its_mode(self):
        ntfy_store.write_json(self.path, [])
        os.chmod(self.path, 0o640)
        ntfy_store.write_json(self.path, [1])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)

    def test_the_lock_holds_other_processes_out(self):
        marker = os.path.join(self.dir, 'acquired')
        child = textwrap.dedent('''
            import sys
            sys.path.insert(0, sys.argv[1])
            import ntfy_store
            with ntfy_store.locked(sys.argv[2]):
                open(sys.argv[3], 'w').close()
        ''')
        with ntfy_store.locked(self.path):
            proc = subprocess.Popen([sys.executable, '-c', child, ROOT, self.path, marker])
            time.sleep(1.5)
            self.assertFalse(os.path.exists(marker), 'child got the lock while it was held')
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertTrue(os.path.exists(marker), 'child never got the lock after release')


class ServerLocking(unittest.TestCase):
    def test_changes_from_many_processes_at_once_are_all_kept(self):
        """Without the lock these load-modify-save cycles overwrite each other."""
        procs, per_proc = 6, 20
        client = textwrap.dedent('''
            import os, sys, time
            sys.path.insert(0, sys.argv[1])
            os.environ.setdefault('NTFY_TOPIC', 'x')
            import ntfy_server
            ntfy_server.DATA_FILE = sys.argv[2]
            c = ntfy_server.app.test_client()
            time.sleep(max(0, float(sys.argv[3]) - time.time()))     # start together
            for _ in range(int(sys.argv[4])):
                assert c.post('/schedule', json={'message': 'm', 'next_fire': 1800000000}).status_code == 201
        ''')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'reminders.json')
            start = time.time() + 5       # imports take a while; leave room
            running = [subprocess.Popen([sys.executable, '-c', client, ROOT, path, str(start), str(per_proc)])
                       for _ in range(procs)]
            for p in running:
                self.assertEqual(p.wait(timeout=120), 0)
            with open(path) as f:
                self.assertEqual(len(json.load(f)), procs * per_proc)


if __name__ == '__main__':
    unittest.main()
