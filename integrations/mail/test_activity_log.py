"""Activity log entries the mail broker sends to the browser broker's log (Nov 5)."""
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

import test_google_pim
spec = importlib.util.spec_from_file_location('mail_broker_for_log', Path(__file__).with_name('broker.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from test_broker import Fake as MailFake


class Logged:
    def activity(self, entry):
        self.logged.append(entry)


class PimFake(test_google_pim.Fake, Logged):
    def __init__(self):
        super().__init__(); self.logged = []


class SendFake(MailFake, Logged):
    def __init__(self):
        super().__init__(); self.logged = []


def wait(fake, n=1):
    for _ in range(100):
        if len(fake.logged) >= n:
            return fake.logged
        time.sleep(0.01)
    return fake.logged


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def test_sent_email_is_logged_with_recipients_and_subject(self):
        p = SendFake(); b = m.Broker(Path(self.tmp.name) / 'a.db', p)
        d = b.call('create', {'request_id': 'request-123', 'account': 'gmail', 'to': ['a@example.com'], 'subject': 'Follow-up', 'body': 'Hello'})
        b.call('review', {'id': d['id']})
        self.assertEqual(p.logged, [])
        b.call('send', {'id': d['id'], 'hash': d['hash']}, privileged=True)
        [e] = wait(p)
        self.assertEqual((e['kind'], e['status'], e['key']), ('email', 'completed', 'email-' + d['id']))
        self.assertIn('a@example.com', e['title']); self.assertIn('Follow-up', e['title']); self.assertIn('Hello', e['details'])

    def test_unconfirmed_send_is_logged_uncertain(self):
        p = SendFake(); p.fail = True; b = m.Broker(Path(self.tmp.name) / 'a.db', p)
        d = b.call('create', {'request_id': 'request-456', 'account': 'gmail', 'to': ['a@example.com'], 'subject': 'S', 'body': 'B'})
        b.call('review', {'id': d['id']}); b.call('send', {'id': d['id'], 'hash': d['hash']}, privileged=True)
        self.assertEqual(wait(p)[0]['status'], 'uncertain')

    def test_calendar_and_tasks_changes_are_logged_reads_are_not(self):
        p = PimFake(); b = m.Broker(Path(self.tmp.name) / 'p.db', p)
        b.call('cal_events', {}); b.call('tasks_list', {})
        b.call('cal_create', {'request_id': 'create-001', 'summary': 'Dentist', 'start': '2026-11-13T15:00:00-05:00', 'end': '2026-11-13T16:00:00-05:00'})
        b.call('tasks_add', {'request_id': 'task-add-01', 'title': 'Renew license'})
        logged = wait(p, 2)
        self.assertEqual([e['kind'] for e in logged], ['calendar', 'task_list'])
        self.assertIn('Added to your calendar: Dentist', logged[0]['title'])
        self.assertIn('Renew license', logged[1]['title'])

    def test_guest_invite_logged_only_after_owner_approval(self):
        p = PimFake(); b = m.Broker(Path(self.tmp.name) / 'p.db', p)
        r = b.call('cal_create', {'request_id': 'create-002', 'summary': 'Lunch', 'start': '2026-11-13T12:00:00-05:00', 'end': '2026-11-13T13:00:00-05:00', 'guests': ['x@example.com']})
        time.sleep(0.05); self.assertEqual(p.logged, [])
        h = p.messages[-1].split('/calapprove ')[1].split()[1]
        b.call('cal_approve', {'id': r['approval_id'], 'hash': h}, privileged=True)
        [e] = wait(p)
        self.assertIn('emailed invitations', e['title'])

    def test_log_failure_never_breaks_the_action(self):
        p = PimFake()
        def boom(entry): raise OSError('no socket')
        p.activity = boom
        b = m.Broker(Path(self.tmp.name) / 'p.db', p)
        out = b.call('tasks_add', {'request_id': 'task-add-02', 'title': 'Call bank'})
        self.assertEqual(out['added']['title'], 'Call bank')


if __name__ == '__main__':
    unittest.main()
