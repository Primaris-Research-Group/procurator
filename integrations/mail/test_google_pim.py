"""Google Calendar + Tasks (Nov 4): reads free, own events/tasks immediate, guests and deletes need /calapprove."""
import json
import tempfile
import unittest
from pathlib import Path
from broker import Broker, google_error_reason, redact_provider_error


class Fake:
    def __init__(self):
        self.calls, self.messages, self.ready = [], [], True
        self.event = {'id': 'evt12345', 'summary': 'Dentist', 'start': {'dateTime': '2026-11-13T15:00:00-05:00'},
                      'end': {'dateTime': '2026-11-13T16:00:00-05:00'}, 'htmlLink': 'https://calendar.google.com/x'}
    def calendar_ready(self): return self.ready
    def contacts_ready(self): return self.ready
    def gmail_ready(self): return False
    def gmail_modify_ready(self): return False
    def icloud_send_ready(self): return False
    def telegram(self, text): self.messages.append(text)
    def gog(self, args, raw=None):
        self.calls.append(args)
        if 'events' in args: return {'events': [self.event]}
        if args[-2:] == ['evt12345'] or (len(args) > 3 and args[1:3] == ['calendar', 'event']): return {'event': self.event}
        if 'create' in args or 'update' in args: return {'event': {**self.event, 'id': 'new98765'}}
        if 'contacts' in args: return {'contacts': [{'resource': 'people/c1', 'name': 'Maria Lopez', 'email': 'maria@example.com', 'phone': '+1 305 555 0100', 'birthday': '1980-01-01'}]}
        if 'lists' in args: return {'tasklists': [{'id': '@default', 'title': 'My Tasks'}]}
        if args[:2] == ['tasks', 'list'] or args[1:3] == ['tasks', 'list']:
            return {'tasks': [{'id': 'task11111', 'title': 'Renew license', 'status': 'needsAction', 'due': '2026-11-20T00:00:00.000Z'},
                              {'id': 'task22222', 'title': 'Old', 'status': 'completed'}]}
        if 'add' in args: return {'task': {'id': 'task33333', 'title': args[args.index('--title') + 1]}}
        return {'ok': True}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Fake(); self.b = Broker(Path(self.tmp.name) / 'm.db', self.p)
    def writes(self): return [c for c in self.p.calls if '--readonly' not in c]
    def test_contacts_search_is_read_only_and_drops_extra_fields(self):
        r = self.b.call('contacts_search', {'query': 'Maria', 'limit': 3})
        self.assertEqual(r['contacts'], [{'name': 'Maria Lopez', 'email': 'maria@example.com', 'phone': '+1 305 555 0100'}])
        self.assertEqual(self.p.calls[-1], ['--readonly', 'contacts', 'search', 'Maria', '--max', '3'])
        self.assertEqual(self.b.call('contacts_search', {'query': '--max'})['contacts'][0]['name'], 'Maria Lopez')
        self.assertEqual(self.p.calls[-1][3], ' --max')   # a query is never a flag
        with self.assertRaises(ValueError):
            self.b.call('contacts_search', {'query': 'x'})
        self.p.ready = False
        with self.assertRaisesRegex(ValueError, '--contacts'):
            self.b.call('contacts_search', {'query': 'Maria'})

    def test_not_connected_explains_the_one_time_consent(self):
        self.p.ready = False
        with self.assertRaisesRegex(ValueError, 'connect-google.py --start --force-consent --all'):
            self.b.call('cal_events', {})
        self.assertFalse(self.b.call('status', {})['google_calendar_tasks_authorized'])
    def test_reads_are_readonly_and_slim(self):
        r = self.b.call('cal_events', {'days': 7})
        self.assertEqual(r['events'][0]['summary'], 'Dentist'); self.assertEqual(r['events'][0]['start'], '2026-11-13T15:00:00-05:00')
        self.assertEqual(self.p.calls[-1][:3], ['--readonly', 'calendar', 'events'])
        self.assertIn('--timezone', self.p.calls[-1])
        self.b.call('cal_freebusy', {'start': '2026-11-13T08:00:00-05:00', 'end': '2026-11-13T18:00:00-05:00'})
        self.assertEqual(self.writes(), [])
        for bad in [{'days': 90}, {'start': 'next friday'}, {'calendar_id': 'x; rm -rf'}]:
            with self.assertRaises(ValueError): self.b.call('cal_events', bad)
    def test_own_event_is_created_once_without_notifying_anyone(self):
        a = {'request_id': 'dentist-nov13', 'summary': 'Dentist', 'start': '2026-11-13T15:00:00-05:00', 'end': '2026-11-13T16:00:00-05:00',
             'location': '123 Main St', 'reminders': ['popup:30m']}
        r = self.b.call('cal_create', a); self.assertEqual(r['created']['id'], 'new98765'); self.assertFalse(r['guests_notified'])
        cmd = self.writes()[-1]
        self.assertEqual(cmd[:3], ['calendar', 'create', 'primary']); self.assertIn('--send-updates', cmd); self.assertEqual(cmd[cmd.index('--send-updates') + 1], 'none')
        self.assertEqual(cmd[cmd.index('--reminder') + 1], 'popup:30m')
        self.assertTrue(self.b.call('cal_create', a)['replayed']); self.assertEqual(len(self.writes()), 1)   # retries never duplicate
        with self.assertRaisesRegex(ValueError, 'time zone'): self.b.call('cal_create', {**a, 'request_id': 'x' * 8, 'start': '2026-11-13 3pm'})
    def test_a_reused_request_id_with_different_details_is_refused(self):
        # 0.23.0 (health foundation): calendar sync of bookings relies on request ids that bind their content.
        a = {'request_id': 'health-cal-0123456789abcdef', 'summary': 'Appointment', 'start': '2026-11-17T13:00:00-05:00', 'end': '2026-11-17T13:30:00-05:00'}
        first = self.b.call('cal_create', a)
        self.assertEqual(self.b.call('cal_create', dict(a)).get('replayed'), True)
        with self.assertRaisesRegex(ValueError, 'different details'):
            self.b.call('cal_create', {**a, 'start': '2026-11-18T13:00:00-05:00', 'end': '2026-11-18T13:30:00-05:00'})
        self.assertTrue(first)

    def test_text_never_becomes_a_flag(self):
        self.b.call('cal_create', {'request_id': 'flag-test1', 'summary': '--attendees=evil@x.com', 'start': '2026-11-13T15:00:00-05:00', 'end': '2026-11-13T16:00:00-05:00'})
        cmd = self.writes()[-1]; self.assertEqual(cmd[cmd.index('--summary') + 1], ' --attendees=evil@x.com'); self.assertNotIn('--attendees', cmd)
    def test_inviting_guests_waits_for_exact_owner_approval(self):
        r = self.b.call('cal_create', {'request_id': 'lunch-1234', 'summary': 'Lunch', 'start': '2026-11-14T12:00:00-05:00', 'end': '2026-11-14T13:00:00-05:00', 'guests': ['carol@example.com']})
        self.assertEqual(r['status'], 'awaiting_owner_approval'); self.assertEqual(self.writes(), [])
        msg = self.p.messages[-1]; self.assertIn('carol@example.com', msg); line = msg.split('\n')[-1]
        _, ident, code = line.split()
        with self.assertRaisesRegex(ValueError, 'Only the owner'): self.b.call('cal_approve', {'id': ident, 'hash': code})
        with self.assertRaisesRegex(ValueError, 'does not match'): self.b.call('cal_approve', {'id': ident, 'hash': '0' * 16}, True)
        done = self.b.call('cal_approve', {'id': ident, 'hash': code}, True)
        self.assertEqual(done['status'], 'done'); cmd = self.writes()[-1]
        self.assertIn('carol@example.com', cmd); self.assertEqual(cmd[cmd.index('--send-updates') + 1], 'all')
        self.assertTrue(self.b.call('cal_approve', {'id': ident, 'hash': code}, True)['replayed']); self.assertEqual(len(self.writes()), 1)
    def test_delete_always_needs_approval(self):
        r = self.b.call('cal_delete_request', {'event_id': 'evt12345'})
        self.assertEqual(r['status'], 'awaiting_owner_approval'); self.assertEqual(self.writes(), [])
        self.assertIn('Delete this event', self.p.messages[-1])
    def test_tasks_list_add_complete(self):
        self.assertEqual([t['title'] for t in self.b.call('tasks_list', {})['tasks']], ['Renew license'])
        r = self.b.call('tasks_add', {'request_id': 'renew-lic-1', 'title': 'Renew Georgia license', 'due': '2026-11-20'})
        self.assertEqual(r['added']['id'], 'task33333'); self.assertEqual(self.writes()[-1][:3], ['tasks', 'add', '@default'])
        self.b.call('tasks_complete', {'task_id': 'task11111'}); self.assertEqual(self.writes()[-1], ['tasks', 'done', '@default', 'task11111'])
        with self.assertRaises(ValueError): self.b.call('tasks_add', {'request_id': 'renew-lic-2', 'title': 'x', 'due': 'Friday'})

    def test_update_uses_flags_that_gog_update_accepts(self):
        self.b.call('cal_update', {'request_id': 'move-dentist', 'event_id': 'evt12345', 'start': '2026-11-13T16:00:00-05:00', 'end': '2026-11-13T17:00:00-05:00'})
        cmd = self.writes()[-1]
        self.assertEqual(cmd[:4], ['calendar', 'update', 'primary', 'evt12345']); self.assertNotIn('--timezone', cmd)
        self.assertEqual(cmd[cmd.index('--start-timezone') + 1], 'America/New_York'); self.assertIn('--end-timezone', cmd)
    def test_reminder_is_a_short_event_that_alerts_his_phone_at_that_minute(self):
        """Nov 4: "remind me to report it tomorrow" had no way to become a phone notification."""
        r = self.b.call('cal_reminder', {'request_id': 'leave-nov5-remind', 'text': 'Report Nov 5 leave absence to Leaveco', 'at': '2026-11-05T18:00:00-05:00', 'email': True})
        cmd = self.writes()[-1]
        self.assertEqual(cmd[:3], ['calendar', 'create', 'primary'])
        self.assertEqual(cmd[cmd.index('--summary') + 1], 'Reminder: Report Nov 5 leave absence to Leaveco')
        self.assertEqual(cmd[cmd.index('--to') + 1], '2026-11-05T18:15:00-05:00')
        self.assertEqual([cmd[i + 1] for i, x in enumerate(cmd) if x == '--reminder'], ['popup:0m', 'email:0m'])
        self.assertEqual(cmd[cmd.index('--send-updates') + 1], 'none'); self.assertEqual(r['alert'], ['popup:0m', 'email:0m'])
        self.assertTrue(self.b.call('cal_reminder', {'request_id': 'leave-nov5-remind', 'text': 'x' * 10, 'at': '2026-11-05T18:00:00-05:00'})['replayed'])
        with self.assertRaisesRegex(ValueError, 'time zone'): self.b.call('cal_reminder', {'request_id': 'bad-remind-1', 'text': 'Call mom', 'at': 'tomorrow 6pm'})
    def test_switched_off_google_api_is_named(self):
        why = google_error_reason('googleapi: Error 403: Google Calendar API has not been used in project 1234 before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=1234', True)
        self.assertIn('Google Calendar API is switched off', why); self.assertIn('Enable', why)
        self.assertIn('Google Tasks API', google_error_reason('Error 403: Tasks API has not been used in project 1 before or it is disabled. SERVICE_DISABLED tasks.googleapis.com'))
    def test_google_failures_explain_themselves_without_secrets(self):
        """Nov 4: a calendar add after the CVS booking failed with an opaque message."""
        self.assertIn('one-time Google consent', google_error_reason('googleapi: Error 403: Request had insufficient authentication scopes.', True))
        self.assertIn('rejected one of the values', google_error_reason('googleapi: Error 400: Bad Request, invalid start time'))
        self.assertIn('expired', google_error_reason('oauth2: "invalid_grant" "Token has been expired or revoked."'))
        self.assertIn('reuse the same request_id', google_error_reason('boom', True))
        line = redact_provider_error('Error for owner@example.com token ya29.a0AfB_byC1234567890 at https://www.googleapis.com/calendar/v3 key ' + 'A' * 40)
        for secret in ('example-user', 'ya29', 'googleapis', 'A' * 40):
            self.assertNotIn(secret, line)


if __name__ == '__main__':
    unittest.main()
