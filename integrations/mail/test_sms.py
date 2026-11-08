"""Text messages (0.25.0): immutable drafts, owner-only send bound to the exact text, off until enabled, no blind retries."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from broker import Broker
from sms import Sms, code_for


class Fake:
    def __init__(self):
        self.messages = []
    def telegram(self, text): self.messages.append(text)
    def calendar_ready(self): return False
    def gmail_ready(self): return False
    def gmail_modify_ready(self): return False
    def icloud_send_ready(self): return False


class FakeTwilio:
    def __init__(self, on=True, result=None, boom=False):
        self.on, self.sent, self.result, self.boom = on, [], result or {'status': 'accepted', 'provider_id': 'SM1'}, boom
    def enabled(self): return {'enabled': True, 'from': '+13055550100'} if self.on else None
    def send(self, to, body):
        self.sent.append((to, body))
        if self.boom:
            raise TimeoutError()
        return self.result


class SmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Fake(); self.b = Broker(Path(self.tmp.name) / 'm.db', self.p)

    def sms(self, **kw):
        return Sms(self.b, FakeTwilio(**kw))

    def test_draft_review_send_once(self):
        s, tw = self.sms(), None
        d = s.call('sms_draft', {'request_id': 'txt-0001', 'to': '+1 (305) 555-0142', 'body': 'Running 10 minutes late — the owner'})
        self.assertEqual((d['to'], d['status']), ('+13055550142', 'draft'))
        self.assertEqual(s.call('sms_draft', {'request_id': 'txt-0001', 'to': '+13055550142', 'body': 'Running 10 minutes late — the owner'})['id'], d['id'])
        with self.assertRaisesRegex(ValueError, 'different text'):
            s.call('sms_draft', {'request_id': 'txt-0001', 'to': '+13055550142', 'body': 'changed'})
        s.call('sms_review', {'id': d['id']})
        msg = self.p.messages[-1]
        code = code_for({'to': '+13055550142', 'body': 'Running 10 minutes late — the owner'})[:16]
        self.assertIn('/smssend %s %s' % (d['id'], code), msg)
        with self.assertRaisesRegex(ValueError, 'owner command'):
            s.call('sms_send', {'id': d['id'], 'hash': code}, False)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            s.call('sms_send', {'id': d['id'], 'hash': '0' * 16}, True)
        out = s.call('sms_send', {'id': d['id'], 'hash': code}, True)
        self.assertEqual(out['status'], 'sent')
        self.assertTrue(s.call('sms_send', {'id': d['id'], 'hash': code}, True)['already'])
        self.assertEqual(len(s.p.sent), 1)

    def test_off_until_enabled_and_validation(self):
        s = self.sms(on=False)
        d = s.call('sms_draft', {'request_id': 'txt-0002', 'to': '+13055550142', 'body': 'hi'})
        with self.assertRaisesRegex(ValueError, 'connect-sms.py'):
            s.call('sms_review', {'id': d['id']})
        for bad in ({'to': '+443055550142'}, {'to': '+19005550142'}, {'body': 'x' * 481}, {'body': 'my password: hunter2'}, {'body': 'card 4111 1111 1111 1111'}):
            with self.assertRaises(ValueError, msg=bad):
                s.call('sms_draft', {'request_id': 'txt-bad-' + str(len(str(bad))), 'to': '+13055550142', 'body': 'ok', **bad})

    def test_ambiguous_result_is_uncertain_and_never_retried(self):
        s = self.sms(boom=True)
        d = s.call('sms_draft', {'request_id': 'txt-0003', 'to': '+13055550142', 'body': 'See you at 3'})
        s.call('sms_review', {'id': d['id']})
        code = code_for({'to': '+13055550142', 'body': 'See you at 3'})[:16]
        self.assertEqual(s.call('sms_send', {'id': d['id'], 'hash': code}, True)['status'], 'uncertain')
        self.assertTrue(s.call('sms_send', {'id': d['id'], 'hash': code}, True)['already'])
        self.assertEqual(len(s.p.sent), 1)

    def test_expired_review_needs_a_new_one(self):
        s = self.sms()
        d = s.call('sms_draft', {'request_id': 'txt-0004', 'to': '+13055550142', 'body': 'ok'})
        s.call('sms_review', {'id': d['id']})
        with self.b.db() as db:
            db.execute('UPDATE sms SET reviewed=? WHERE id=?', (time.time() - 1900, d['id']))
        with self.assertRaisesRegex(ValueError, 'fresh review'):
            s.call('sms_send', {'id': d['id'], 'hash': code_for({'to': '+13055550142', 'body': 'ok'})[:16]}, True)

    def test_broker_routes_sms_and_the_model_socket_cannot_send(self):
        with self.assertRaisesRegex(ValueError, 'owner command'):
            self.b.call('sms_send', {'id': 'x'}, privileged=False)


if __name__ == '__main__':
    unittest.main()
