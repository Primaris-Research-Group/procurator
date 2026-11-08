import tempfile
import time
import unittest
from pathlib import Path

from activity import Activity
import test_tasks
from test_tasks import CONTEXT


class ActivityStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1_800_000_000.0]
        self.a = Activity(str(Path(self.tmp.name) / 'activity.sqlite3'), now=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_by_key_and_no_duplicates(self):
        e = {'key': 'call-abc123', 'kind': 'call', 'title': 'Called the NJ bar office', 'status': 'in_progress'}
        first = self.a.record(e, 'voice')
        self.assertTrue(first['created'])
        self.assertTrue(self.a.record(e, 'voice')['unchanged'])
        self.clock[0] += 300
        upd = self.a.record({**e, 'status': 'completed', 'details': 'Summary: they confirmed receipt.'}, 'voice')
        self.assertEqual(upd['id'], first['id'])
        got = self.a.get(first['id'])
        self.assertEqual(got['status'], 'completed')
        self.assertIn('confirmed receipt', got['details'])
        self.assertIn('last_update', got)
        self.assertEqual(len(self.a.search({'days': 1})['entries']), 1)

    def test_assistant_reports_never_overwrite_service_records(self):
        self.a.record({'key': 'x-12345', 'kind': 'email', 'title': 'Email sent', 'status': 'completed'}, 'mail')
        self.a.record({'key': 'x-12345', 'kind': 'report', 'title': 'My summary', 'status': 'completed'}, 'assistant')
        entries = self.a.search({'days': 1})['entries']
        self.assertEqual(len(entries), 2)
        self.assertEqual({e['by'] for e in entries}, {'assistant report', 'system record (mail)'})
        self.assertEqual(len(self.a.search({'days': 1, 'source': 'assistant'})['entries']), 1)

    def test_validation(self):
        for bad in [{'key': 'ok-key', 'kind': 'nope', 'title': 't'}, {'key': 'a', 'kind': 'call', 'title': 't'},
                    {'key': 'ok-key', 'kind': 'call', 'title': ''}, {'key': 'ok-key', 'kind': 'call', 'title': 't', 'status': 'done'},
                    {'key': 'ok key', 'kind': 'call', 'title': 't'}]:
            with self.assertRaises(ValueError):
                self.a.record(bad, 'web')
        with self.assertRaises(ValueError):
            self.a.record({'key': 'ok-key', 'kind': 'call', 'title': 't'}, 'model')
        r = self.a.record({'key': 'ok-key', 'kind': 'call', 'title': 'A‮B\x07', 'details': 'x' * 9000}, 'web')
        e = self.a.get(r['id'])
        self.assertEqual(e['title'], 'AB')
        self.assertEqual(len(e['details']), 6000)

    def test_search_filters_and_window(self):
        self.a.record({'key': 'old-1', 'kind': 'email', 'title': 'Old email', 'status': 'completed'}, 'mail')
        self.clock[0] += 10 * 86400
        self.a.record({'key': 'new-1', 'kind': 'call', 'title': 'Called CVS pharmacy', 'details': 'Flu shot', 'mission': 'BG'}, 'voice')
        self.assertEqual([e['title'] for e in self.a.search({'days': 7})['entries']], ['Called CVS pharmacy'])
        self.assertEqual(len(self.a.search({'days': 30})['entries']), 2)
        self.assertEqual(len(self.a.search({'days': 30, 'kind': 'email'})['entries']), 1)
        self.assertEqual(len(self.a.search({'days': 30, 'query': 'flu'})['entries']), 1)
        self.assertEqual(len(self.a.search({'days': 30, 'mission': 'bg'})['entries']), 1)

    def test_telegram_text_is_plain(self):
        r = self.a.record({'key': 'call-1x', 'kind': 'call', 'title': 'Called the NJ bar office', 'status': 'completed',
                           'details': 'Pressed 2, then spoke with intake.', 'refs': ['call reference CA123']}, 'voice')
        listing = self.a.telegram_list(3)
        self.assertIn('Phone call: Called the NJ bar office', listing)
        self.assertNotIn('`', listing)
        self.assertNotIn('{', listing)
        entry = self.a.telegram_entry(r['id'])
        self.assertIn('Pressed 2', entry)
        self.assertIn('• call reference CA123', entry)
        self.assertEqual(self.a.telegram_list(1, 'email'), 'Nothing recorded in the last 1 day.')

    def test_view_link_expires_and_page_escapes(self):
        link = self.a.view_link('https://example.org')
        token = link.rsplit('/', 1)[1]
        self.assertTrue(link.startswith('https://example.org/assistant/activity/'))
        self.assertTrue(self.a.view_ok(token))
        self.assertFalse(self.a.view_ok('A' * 43))
        self.a.record({'key': 'x-99999', 'kind': 'browser_task', 'title': '<script>alert(1)</script>', 'details': '<b>hi</b>'}, 'web')
        page = self.a.page(30, None, 'NONCE')
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('nonce="NONCE"', page)
        self.clock[0] += 46 * 60
        self.assertFalse(self.a.view_ok(token))


class BrokerActivityTests(unittest.TestCase):
    """Uses the same practice browser setup as the task tests (helpers borrowed, tests not re-run)."""
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    task = test_tasks.TaskTests.task
    step = test_tasks.TaskTests.step
    final = test_tasks.TaskTests.final

    def entries(self, **kw):
        return self.b.activity.search({'days': 1, **kw})['entries']

    def test_task_lifecycle_is_logged_once_and_updated(self):
        t, _ = self.task()
        [e] = self.entries(kind='browser_task')
        self.assertEqual(e['status'], 'in_progress')
        self.step(t)
        args = self.final(t)
        self.b.call('execute', args, True)
        self.provider.snap['text'] = 'PRACTICE CONFIRMED: Practice Visitor'
        self.b.call('task_complete', {'id': t, 'runtime_context': CONTEXT})
        [e] = self.entries(kind='browser_task')
        self.assertEqual(e['status'], 'completed')
        full = self.b.activity.get(e['id'])
        self.assertIn('Finished', full['details'])
        self.assertIn('PRACTICE CONFIRMED', full['details'])
        self.assertIn('browser task ' + t, full['refs'])
        [a] = self.entries(kind='browser_action')
        self.assertEqual(a['status'], 'completed')
        self.assertIn('You approved it', self.b.activity.get(a['id'])['details'])

    def test_stopped_task_is_logged_cancelled(self):
        t, _ = self.task()
        self.b.call('task_stop', {'id': t, 'context': CONTEXT}, True)
        [e] = self.entries(kind='browser_task')
        self.assertEqual(e['status'], 'cancelled')

    def test_report_record_and_owner_access(self):
        out = self.b.call('activity_report', {'request_id': 'rep-001', 'title': 'Booked flu shot at CVS', 'details': 'Fri 10:30 AM, confirmation seen.',
                                              'refs': ['browser task abc']})
        self.assertTrue(out['saved'])
        again = self.b.call('activity_report', {'request_id': 'rep-001', 'title': 'Booked flu shot at CVS', 'details': 'Fri 10:30 AM, confirmation seen.',
                                                'refs': ['browser task abc']})
        self.assertTrue(again.get('unchanged'))
        with self.assertRaises(ValueError):
            self.b.call('activity_record', {'source': 'assistant', 'entry': {'key': 'k-12345', 'kind': 'call', 'title': 'fake'}})
        with self.assertRaises(ValueError):
            self.b.call('activity_record', {'source': 'web', 'entry': {'key': 'k-12345', 'kind': 'call', 'title': 'fake'}})
        self.b.call('activity_record', {'source': 'mail', 'entry': {'key': 'email-1', 'kind': 'email', 'title': 'Email sent', 'status': 'completed'}})
        self.assertEqual(len(self.entries()), 2)
        with self.assertRaises(ValueError):
            self.b.call('activity_owner', {'page': True})
        self.assertTrue(self.b.call('activity_owner', {'page': True}, True)['sent'])
        text = self.b.call('activity_owner', {'days': 3}, True)['text']
        self.assertIn('Booked flu shot at CVS', text)
        with self.assertRaises(ValueError):
            self.b.call('activity_view', {'token': 'A' * 43}, True)


if __name__ == '__main__':
    unittest.main()


class UpcomingTests(unittest.TestCase):
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    task = test_tasks.TaskTests.task
    step = test_tasks.TaskTests.step
    final = test_tasks.TaskTests.final

    def test_waiting_running_and_scheduled_without_approval_codes(self):
        t, _ = self.task()
        args = self.final(t)
        up = self.b.call('activity_upcoming', {})
        whats = [(x['group'], x['what']) for x in up['items']]
        self.assertIn(('Waiting for you', 'Browser task: Practice task'), [(g, w.split(' — ')[0]) for g, w in whats])
        self.assertTrue(any(x['kind'] == 'browser_action' for x in up['items']))
        self.assertNotIn(args['hash'], str(up))
        self.assertIn('note', up, 'gateway items not pushed yet')
        self.b.call('activity_upcoming_push', {'items': [{'group': 'Waiting for you', 'kind': 'email', 'what': 'Email to a@b.c — "Hi"', 'how': 'Send the /mailsend line'},
                                                         {'group': 'Scheduled', 'kind': 'calendar', 'what': 'Reminder: call the bank', 'when': time.time() + 3600},
                                                         {'group': 'bogus', 'what': 'dropped'}]})
        up = self.b.call('activity_upcoming', {})
        self.assertNotIn('note', up)
        self.assertEqual(sum(1 for x in up['items'] if x.get('source') == 'gateway'), 2)
        text = self.b.call('activity_owner', {'upcoming': True}, True)['text']
        self.assertTrue(text.startswith('Waiting for you:'))
        self.assertIn('Scheduled:\n• Reminder: call the bank', text)
        self.assertNotIn('`', text)
