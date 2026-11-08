"""Nov 5: the assistant's own address, mission inbox watches and the pending-approvals list."""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('mail_broker_assistant', Path(__file__).with_name('broker.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from test_broker import Fake


class GogFake(Fake):
    def __init__(self):
        super().__init__(); self.queries = []
    def gog(self, args, raw=None):
        self.queries.append(args)
        return {'messages': [{'id': 'abc1234567', 'from': 'Ethics <ethics@example.gov>', 'subject': 'Your grievance', 'date': 'Nov 5'}]}
    def gmail_headers(self, i): return {'from': 'x', 'subject': 'y', 'date': 'z'}
    def icloud(self, **kw):
        self.queries.append(kw); return {'messages': [{'uid': '77', 'from': 'a@b.c', 'subject': 'S', 'date': 'D'}]}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = GogFake(); self.b = m.Broker(Path(self.tmp.name) / 'mail.db', self.p)

    def test_only_owner_sets_address_and_plus_alias_can_send(self):
        with self.assertRaises(ValueError):
            self.b.call('assistant_address_set', {'address': 'owner+assistant@example.com'})
        for bad in ['owner@example.com', 'not an address', 'owner@icloud.example']:
            with self.assertRaises(ValueError):
                self.b.call('assistant_address_set', {'address': bad}, privileged=True)
        out = self.b.call('assistant_address_set', {'address': 'owner+assistant@example.com'}, privileged=True)
        self.assertEqual((out['kind'], out['send']), ('gmail_alias', True))
        d = self.b.call('create', {'request_id': 'assistant-01', 'account': 'assistant', 'to': ['office@example.org'], 'subject': 'Hi', 'body': 'B'})
        self.assertEqual(d['draft']['from'], 'owner+assistant@example.com')
        self.b.call('review', {'id': d['id']})
        self.assertIn('From: owner+assistant@example.com', self.p.reviews[-1])
        sent = self.b.call('send', {'id': d['id'], 'hash': d['hash']}, privileged=True)
        self.assertEqual(sent['status'], 'sent'); self.assertEqual(len(self.p.sent), 1)

    def test_custom_domain_is_receive_only_until_send_as_is_confirmed(self):
        out = self.b.call('assistant_address_set', {'address': 'assistant@assistant.example.com'}, privileged=True)
        self.assertFalse(out['send'])
        with self.assertRaisesRegex(ValueError, 'not set up for sending'):
            self.b.call('create', {'request_id': 'assistant-02', 'account': 'assistant', 'to': ['a@example.org'], 'subject': 'S', 'body': 'B'})
        self.assertTrue(self.b.call('assistant_address_set', {'address': 'assistant@assistant.example.com', 'send_as': True}, privileged=True)['send'])

    def test_inbox_and_watch_searches_are_read_only_and_scoped(self):
        with self.assertRaises(ValueError):
            self.b.call('assistant_inbox', {})
        self.b.call('assistant_address_set', {'address': 'owner+assistant@example.com'}, privileged=True)
        out = self.b.call('assistant_inbox', {'days': 7})
        self.assertEqual(out['messages'][0]['subject'], 'Your grievance')
        self.assertIn('--readonly', self.p.queries[-1]); self.assertIn('to:owner+assistant@example.com newer_than:7d', self.p.queries[-1])
        self.b.call('watch_search', {'account': 'gmail', 'query': 'from:ethics@example.gov'})
        self.assertIn('from:ethics@example.gov newer_than:14d', self.p.queries[-1])
        ic = self.b.call('watch_search', {'account': 'icloud', 'query': 'from:a@b.c subject:"grievance update" status'})
        self.assertEqual(ic['messages'][0]['id'], 'icloud:77')
        f = self.p.queries[-1]['filters']
        self.assertEqual((f['from'], f['subject'], f['text']), ('a@b.c', 'grievance update', 'status'))
        with self.assertRaises(ValueError):
            self.b.call('watch_search', {'account': 'gmail', 'query': 'x\ny'})

    def test_pending_lists_live_reviews_with_their_commands(self):
        d = self.b.call('create', {'request_id': 'pending-01', 'account': 'gmail', 'to': ['a@example.org'], 'subject': 'Follow-up', 'body': 'B'})
        self.assertEqual(self.b.call('pending', {})['pending'], [])
        self.b.call('review', {'id': d['id']})
        [p] = self.b.call('pending', {})['pending']
        self.assertEqual(p['command'], '/mailsend ' + d['id']); self.assertIn('Follow-up', p['what'])


if __name__ == '__main__':
    unittest.main()
