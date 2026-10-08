"""Run with:  python -m unittest discover -s tests -v
Needs the packages in requirements.txt (flask, requests, python-dotenv); sends
nothing - the notification call is stubbed and reminders live in a temp dir.

Covers the weekday filter on repeating reminders: the schedule maths
(ntfy_schedule.py), the REST endpoints (ntfy_server.py) and the cron script
(ntfy_dispatch.py)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('NTFY_TOPIC', 'test-topic')   # ntfy_dispatch refuses to import without one

import ntfy_dispatch   # noqa: E402
import ntfy_schedule as sched   # noqa: E402
import ntfy_server   # noqa: E402

DUBLIN = ZoneInfo('Europe/Dublin')
UTC = ZoneInfo('UTC')
WEEKDAYS = [0, 1, 2, 3, 4]
WEEKENDS = [5, 6]


def ts(y, m, d, hh=9, mm=0, tz=DUBLIN):
    return int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


# 9 Oct 2026 is a Friday - see test_calendar_assumption.
FRI, SAT, SUN, MON, TUE = (ts(2026, 10, d) for d in (9, 10, 11, 12, 13))


class Advance(unittest.TestCase):
    def test_calendar_assumption(self):
        days = [datetime.fromtimestamp(t, DUBLIN).weekday() for t in (FRI, SAT, SUN, MON, TUE)]
        self.assertEqual(days, [4, 5, 6, 0, 1])

    def test_no_filter_is_the_plain_interval(self):
        for days in (None, []):
            self.assertEqual(sched.advance_time(FRI, 'days', 1, days, DUBLIN), SAT)
        self.assertEqual(sched.advance_time(FRI, 'weeks', 1), FRI + 7 * 86400)

    def test_daily_on_weekdays_skips_the_weekend(self):
        self.assertEqual(sched.advance_time(FRI, 'days', 1, WEEKDAYS, DUBLIN), MON)
        self.assertEqual(sched.advance_time(MON, 'days', 1, WEEKDAYS, DUBLIN), TUE)

    def test_daily_on_weekends_skips_the_week(self):
        self.assertEqual(sched.advance_time(SAT, 'days', 1, WEEKENDS, DUBLIN), SUN)
        self.assertEqual(sched.advance_time(SUN, 'days', 1, WEEKENDS, DUBLIN), SAT + 7 * 86400)

    def test_hourly_keeps_its_grid_across_the_gap(self):
        # Fri 21:00 + 3h is Sat 00:00 (masked) ... resumes Mon 00:00.
        self.assertEqual(sched.advance_time(ts(2026, 10, 9, 21), 'hours', 3, WEEKDAYS, DUBLIN), ts(2026, 10, 12, 0))

    def test_every_other_day_is_masked_not_shifted(self):
        self.assertEqual(sched.advance_time(FRI, 'days', 2, WEEKDAYS, DUBLIN), TUE)   # Sun is masked, Tue next

    def test_the_weekday_is_read_in_the_given_zone(self):
        late = ts(2026, 10, 9, 23, 30, UTC)   # Fri 23:30 UTC = Sat 00:30 in Dublin (BST)
        self.assertEqual(sched.first_allowed(late, 'days', 1, WEEKDAYS, UTC), late)
        self.assertEqual(sched.first_allowed(late, 'days', 1, WEEKDAYS, DUBLIN), ts(2026, 10, 12, 0, 30))

    def test_an_interval_that_never_hits_a_chosen_day_is_reported(self):
        self.assertIsNone(sched.first_allowed(MON, 'days', 7, [1], DUBLIN))   # every 7 days from a Monday
        self.assertEqual(sched.advance_time(MON, 'days', 7, [1], DUBLIN), MON + 7 * 86400)   # no spinning

    def test_zone_lookup(self):
        self.assertEqual(sched.zone('Europe/Dublin'), DUBLIN)
        for bad in (None, '', 'Not/AZone', '../../etc/passwd', 'Europe'):
            self.assertIsNone(sched.zone(bad), bad)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, fname in (('DATA_FILE', 'reminders.json'), ('TITLES_FILE', 'titles.json')):
            p = mock.patch.object(ntfy_server, name, os.path.join(self.tmp.name, fname))
            p.start()
            self.addCleanup(p.stop)
        self.client = ntfy_server.app.test_client()

    def create(self, **kw):
        body = {'message': 'm', 'next_fire': MON, 'recurring': True, 'interval_type': 'days', 'interval_value': 1}
        body.update(kw)
        return self.client.post('/schedule', json=body)

    def stored(self, rid):
        return next(r for r in self.client.get('/reminders').get_json() if r['id'] == rid)

    def test_weekday_filter_is_stored_sorted_and_deduped(self):
        resp = self.create(days=[4, 0, 0, 2], tz='Europe/Dublin')
        self.assertEqual(resp.status_code, 201)
        r = self.stored(resp.get_json()['id'])
        self.assertEqual((r['days'], r['tz']), ([0, 2, 4], 'Europe/Dublin'))

    def test_all_seven_days_means_no_filter(self):
        r = self.stored(self.create(days=list(range(7)), tz='Europe/Dublin').get_json()['id'])
        self.assertEqual((r['days'], r['tz']), (None, None))

    def test_bad_days_are_rejected(self):
        for bad in ([], [7], [-1], ['1'], [True], [1.0], 'mon', {'0': 1}):
            self.assertEqual(self.create(days=bad).status_code, 400, bad)
        self.assertEqual(self.client.get('/reminders').get_json(), [])

    def test_only_hourly_and_daily_repeats_take_a_filter(self):
        for kw in (dict(interval_type='weeks'), dict(interval_type='months'), dict(recurring=False)):
            r = self.stored(self.create(days=[1], **kw).get_json()['id'])
            self.assertEqual((r['days'], r['tz']), (None, None), kw)
        self.assertEqual(self.stored(self.create(days=[1], interval_type='hours').get_json()['id'])['days'], [1])

    def test_a_first_fire_on_an_excluded_day_moves_to_the_next_allowed_one(self):
        resp = self.create(days=WEEKDAYS, tz='Europe/Dublin', next_fire=SAT)
        self.assertEqual(resp.get_json()['next_fire'], MON)
        self.assertEqual(self.stored(resp.get_json()['id'])['next_fire'], MON)

    def test_an_allowed_first_fire_is_left_alone(self):
        self.assertEqual(self.create(days=WEEKDAYS, tz='Europe/Dublin').get_json()['next_fire'], MON)

    def test_an_interval_that_never_lands_on_the_days_is_rejected(self):
        self.assertEqual(self.create(days=[1], interval_value=7, tz='Europe/Dublin').status_code, 400)

    def test_an_unknown_timezone_is_dropped_but_the_filter_stays(self):
        r = self.stored(self.create(days=[0, 2], tz='../../etc/passwd').get_json()['id'])
        self.assertEqual((r['days'], r['tz']), ([0, 2], None))

    def test_editing_keeps_the_filter_unless_told_otherwise(self):
        rid = self.create(days=[0, 2], tz='UTC').get_json()['id']
        self.assertEqual(self.client.put(f'/reminders/{rid}', json={'message': 'renamed'}).status_code, 200)
        r = self.stored(rid)
        self.assertEqual((r['message'], r['days'], r['tz']), ('renamed', [0, 2], 'UTC'))
        self.assertEqual(self.client.put(f'/reminders/{rid}', json={'days': None}).status_code, 200)
        r = self.stored(rid)
        self.assertEqual((r['days'], r['tz']), (None, None))

    def test_editing_to_a_weekly_repeat_drops_the_filter(self):
        rid = self.create(days=[0, 2], tz='UTC').get_json()['id']
        self.client.put(f'/reminders/{rid}', json={'interval_type': 'weeks'})
        self.assertEqual(self.stored(rid)['days'], None)

    def test_a_rejected_edit_changes_nothing(self):
        rid = self.create(days=[0, 2], tz='UTC').get_json()['id']
        self.assertEqual(self.client.put(f'/reminders/{rid}', json={'message': 'x', 'days': [9]}).status_code, 400)
        self.assertEqual(self.stored(rid)['message'], 'm')


class Dispatcher(unittest.TestCase):
    def run_main(self, reminder, now):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'reminders.json')
            with open(path, 'w') as f:
                json.dump([reminder], f)
            with mock.patch.object(ntfy_dispatch, 'DATA_FILE', path), \
                    mock.patch.object(ntfy_dispatch, 'send_notification', return_value=True) as send, \
                    mock.patch.object(ntfy_dispatch.time, 'time', return_value=now):
                ntfy_dispatch.main()
            with open(path) as f:
                return json.load(f)[0], send

    def reminder(self, **kw):
        r = {'id': 'r', 'message': 'm', 'priority': 3, 'next_fire': FRI, 'recurring': True,
             'interval_type': 'days', 'interval_value': 1, 'days': WEEKDAYS, 'tz': 'Europe/Dublin'}
        r.update(kw)
        return r

    def test_friday_reminder_rolls_to_monday(self):
        r, send = self.run_main(self.reminder(), FRI + 60)
        send.assert_called_once()
        self.assertEqual(r['next_fire'], MON)

    def test_catching_up_after_downtime_fires_once_and_lands_on_an_allowed_day(self):
        r, send = self.run_main(self.reminder(), ts(2026, 10, 14, 8))   # down since Friday, now Wed morning
        send.assert_called_once()
        self.assertEqual(r['next_fire'], ts(2026, 10, 14, 9))

    def test_weekends_only(self):
        r, _ = self.run_main(self.reminder(next_fire=SAT, days=WEEKENDS), SAT + 60)
        self.assertEqual(r['next_fire'], SUN)

    def test_reminders_saved_before_the_filter_existed_still_work(self):
        old = self.reminder()
        del old['days'], old['tz']
        r, _ = self.run_main(old, FRI + 60)
        self.assertEqual(r['next_fire'], SAT)


if __name__ == '__main__':
    unittest.main()
