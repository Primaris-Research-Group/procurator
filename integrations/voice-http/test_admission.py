import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from admission import Admission
from bridge import process, PATH
import urllib.parse


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = str(Path(self.tmp.name)/'admissions.sqlite3')
        self.clock = 1793721600
        self.admission = Admission(self.file, daily=4, per_caller=2, now=lambda: self.clock)

    def test_replay_does_not_consume_another_slot_and_restart_preserves_decision(self):
        self.assertTrue(self.admission.allow('call-1', 'caller-1'))
        for _ in range(5):
            self.assertTrue(Admission(self.file, daily=4, per_caller=2, now=lambda: self.clock).allow('call-1', 'caller-1'))
        self.assertTrue(self.admission.allow('call-2', 'caller-1'))
        self.assertFalse(self.admission.allow('call-3', 'caller-1'))
        self.assertFalse(self.admission.allow('call-3', 'different-caller'))

    def test_global_and_per_caller_limits_reset_on_utc_day(self):
        for i in range(4):
            self.assertTrue(self.admission.allow(str(i), str(i//2)))
        self.assertFalse(self.admission.allow('new', 'other'))
        self.clock += 86400
        self.assertTrue(self.admission.allow('next-day', '0'))

    def test_concurrent_admissions_cannot_exceed_total(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.admission.allow(str(n), str(n)), range(12)))
        self.assertEqual(sum(results), 4)

    def form(self):
        return urllib.parse.urlencode({'From': '+17065550123', 'To': '+15550100100',
            'Direction': 'inbound', 'CallStatus': 'ringing', 'CallSid': 'CA'+'b'*32}).encode()

    def test_reception_requires_verified_native_reply_before_admission(self):
        settings = {'testNumber': '+17865550123', 'fromNumber': '+15550100100', 'receptionEnabled': True}
        class Never:
            def allow(self, *_):
                raise AssertionError('unsigned callback must not consume admission')
        self.assertEqual(process(PATH,self.form(),'bad',settings,lambda *_:(401,'text/plain',b'Unauthorized'),Never())[0],401)

    def test_reception_answers_new_caller_only_after_quota_and_native_verification(self):
        settings = {'testNumber': '+17865550123', 'fromNumber': '+15550100100', 'receptionEnabled': True}
        send = lambda *_: (200,'application/xml',b'<Response><Pause length="30"/></Response>')
        self.assertIn(b'<Redirect', process(PATH,self.form(),'signed',settings,send,self.admission)[2])
        class Full:
            def allow(self, *_):
                return False
        self.assertIn(b'<Reject',process(PATH,self.form(),'signed',settings,send,Full())[2])
        self.assertIn(b'<Reject',process(PATH,self.form(),'signed',settings,send,None)[2])

    def test_realtime_stream_passes_unchanged_only_with_admission(self):
        settings = {'testNumber': '+17865550123', 'fromNumber': '+15550100100', 'receptionEnabled': True}
        body = b'<Response><Connect><Stream url="wss://assistant.example.com/assistant/voice/realtime/exampletoken123456"/></Connect></Response>'
        send = lambda *_: (200, 'application/xml', body)
        self.assertEqual(process(PATH, self.form(), 'signed', settings, send, self.admission)[2], body)
        class Full:
            def allow(self, *_):
                return False
        self.assertIn(b'<Reject', process(PATH, self.form(), 'signed', settings, send, Full())[2])
        self.assertIn(b'<Reject', process(PATH, self.form(), 'signed', settings, send, None)[2])

    def test_native_reject_does_not_consume_reception_quota(self):
        class Never:
            def allow(self, *_):
                raise AssertionError('rejected calls must not consume the quota')
        settings = {'testNumber': '+17865550123', 'fromNumber': '+15550100100', 'receptionEnabled': True}
        body = b'<Response><Reject reason="busy"/></Response>'
        self.assertEqual(process(PATH, self.form(), 'signed', settings, lambda *_:(200, 'application/xml', body), Never())[2], body)


if __name__ == '__main__':
    unittest.main()
