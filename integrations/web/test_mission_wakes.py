"""Nov 5: missions wake on events (owner answers, watched email, the assistant's request), not only on the clock."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from missions import Missions, EASTERN
from test_missions import FILES, Fake

NOON = dt.datetime(2026, 11, 6, 12, 0, tzinfo=EASTERN).timestamp()


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = [NOON]
        self.m = Missions(str(Path(self.tmp.name) / 'missions.sqlite3'), Fake(), now=lambda: self.clock[0])
        link = self.m.call('mission_upload_link', {'return_link': True}, True)['link']
        self.m.call('mission_upload', {'token': link.rsplit('/', 1)[1], 'files': FILES, 'activate': True}, True)
        self.assertEqual(len(self.m.call('mission_due', {})['due']), 1)       # first session claimed
        self.m.call('mission_tick_done', {'mission': 'sg'})

    def due(self, minutes):
        self.clock[0] += minutes * 60
        return self.m.call('mission_due', {})['due']

    def test_owner_answer_brings_the_next_session_forward_with_the_reason(self):
        self.m.call('mission_question', {'mission': 'sg', 'action': 'ask', 'question_id': 'q-date', 'question': 'When did you mail it?'})
        self.assertEqual(self.due(2), [])
        self.m.call('mission_question', {'mission': 'sg', 'action': 'answer', 'question_id': 'q-date', 'answer': 'Oct 28'})
        [d] = self.due(2)
        self.assertEqual(d['reasons'], ['The owner answered question q-date'])
        self.m.call('mission_tick_done', {'mission': 'sg'})
        self.assertEqual(self.due(5), [], 'reasons are cleared and the clock cadence resumes')

    def test_watch_baseline_then_new_email_wakes_and_is_logged(self):
        self.m.call('mission_watch', {'mission': 'sg', 'watch_id': 'ethics', 'account': 'gmail', 'query': 'from:ethics@example.gov'})
        [w] = self.m.call('mission_watch_due', {})['watches']
        out = self.m.call('mission_watch_seen', {**w, 'messages': [{'id': 'old1', 'from': 'Ethics', 'subject': 'Old'}]})
        self.assertTrue(out['baseline'])
        self.assertEqual(self.m.call('mission_watch_due', {})['watches'], [], 'not re-checked within 10 minutes')
        self.assertEqual(self.due(11), [])
        out = self.m.call('mission_watch_seen', {**w, 'messages': [{'id': 'old1'}, {'id': 'new2', 'from': 'Ethics <e@x.gov>', 'subject': 'Docket opened'}]})
        self.assertEqual(out['new'], 1)
        [d] = self.due(2)
        self.assertIn('watch ethics', d['reasons'][0])
        events = self.m.call('mission_log', {'mission': 'sg', 'action': 'list'})
        self.assertIn('Docket opened', str(events))

    def test_wakes_respect_waking_hours_and_model_rate_limit(self):
        self.clock[0] = dt.datetime(2026, 11, 6, 23, 0, tzinfo=EASTERN).timestamp()
        self.m.call('mission_due', {}); self.m.call('mission_tick_done', {'mission': 'sg'})   # the 4 PM session ran
        out = self.m.call('mission_wake', {'mission': 'sg', 'reason': 'Call to the ethics office ended'})
        self.assertIn('07:00 AM', out['next_work_session'])
        self.clock[0] = NOON + 86400
        self.m.call('mission_due', {}); self.m.call('mission_tick_done', {'mission': 'sg'})
        out = self.m.call('mission_wake', {'mission': 'sg', 'reason': 'again'})
        self.assertIn('12:', out['next_work_session'])
        self.assertEqual(len(self.due(2)), 1); self.m.call('mission_tick_done', {'mission': 'sg'})
        out = self.m.call('mission_wake', {'mission': 'sg', 'reason': 'and again'})
        self.assertIn('12:30', out['next_work_session'], 'the assistant can wake a mission at most every 30 minutes')

    def test_paused_missions_do_not_wake_and_watch_limits(self):
        self.m.call('mission_owner', {'mission': 'sg', 'action': 'pause'}, True)
        with self.assertRaises(ValueError):
            self.m.call('mission_wake', {'mission': 'sg', 'reason': 'x'})
        with self.assertRaises(ValueError):
            self.m.call('mission_watch', {'mission': 'sg', 'watch_id': 'w1', 'account': 'whatsapp', 'query': 'x'})


if __name__ == '__main__':
    unittest.main()
