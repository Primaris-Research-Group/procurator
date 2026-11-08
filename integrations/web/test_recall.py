"""0.20.0 conversation memory: capture, scrubbing, nightly digest with lessons from the activity log, context, search, owner control."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from recall import SYSTEM_PROMPT, Recall, scrub, EASTERN

class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t

class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(dt.datetime(2026, 11, 6, 14, 0, tzinfo=EASTERN).timestamp())
        self.calls = []
        def summarizer(convo, work):
            self.calls.append((convo, work))
            return {'summary': 'The owner asked for the IRS W-4 revision date; it is Rev. December 2025. The owner wants CVS flu shots on Saturdays.',
                    'topics': ['W-4', 'flu shot'], 'decisions': ['Book the CVS flu shot on Saturday'],
                    'open_items': ['Book the CVS flu shot for Saturday Nov 14 (raised Nov 6)'],
                    'owner_said': ['"I only do Saturdays for errands"'],
                    'lessons': [{'site': 'cvs.com', 'text': 'The CVS store locator needs the ZIP typed before the list appears.'}]}
        summarizer.model = 'fake-haiku'
        self.r = Recall(Path(self.tmp.name) / 'recall.sqlite3', now=self.clock, summarizer=summarizer)
    def tearDown(self): self.tmp.cleanup()

    def test_turns_are_scrubbed_and_plugin_prompts_are_not_his_words(self):
        self.r.turn('chat', 'my card is 4111 1111 1111 1111 and my password is hunter2, code is 482913', 'Got it.', 'run-1')
        self.assertEqual(scrub('SSN 123-45-6789'), 'SSN [SSN removed]')
        rows = self.r.pending()
        self.assertNotIn('4111', rows[0]['text']); self.assertNotIn('hunter2', rows[0]['text']); self.assertNotIn('482913', rows[0]['text'])
        self.assertEqual(self.r.turn('chat', 'again', 'x', 'run-1')['reason'], 'already recorded')
        self.r.turn('chat', 'Continue owner-approved browser task abcdef0123456789. First read…', 'Done: page read.', 'run-2')
        self.assertEqual([t['role'] for t in self.r.pending()], ['owner', 'assistant', 'assistant'])
        self.r.turn('chat', '', 'NO_REPLY', 'run-3'); self.assertEqual(len(self.r.pending()), 3)
        # Every prompt the plugin itself sends into their conversation is filtered, not only task continuations.
        for prompt in ('SCHEDULED REMINDER that the owner asked for (set 2026-11-06 09:00 UTC): "call CVS".', 'MORNING BRIEF for the owner, Nov 7.',
                       'SIGN-IN FINISHED (from the owner\'s private sign-in page): the owner signed in to cvs.com.', 'MISSION TICK — taxes (r3): Taxes.',
                       'Browser task abcdef0123456789: the routine steps ran in the worker session.', 'TAKEOVER FINISHED (from the owner\'s private browser page): the owner finished on leaveco.example.'):
            self.assertTrue(SYSTEM_PROMPT.search(prompt), prompt)
        self.assertFalse(SYSTEM_PROMPT.search('Remind me to call CVS tomorrow'))

    def test_digest_turns_activity_into_summary_open_items_and_lessons(self):
        self.r.turn('chat', 'What is the W-4 revision date?', 'Rev. December 2025.', 'r1')
        self.r.turn('chat', 'Book my flu shot at CVS, I only do Saturdays for errands', 'I will look for Saturday slots.', 'r2')
        acts = lambda s, e: [{'kind': 'browser_task', 'status': 'completed', 'title': 'CVS store hours', 'details': 'locator needed ZIP first'}]
        out = self.r.digest(acts, reason='scheduled')
        self.assertEqual(out['turns'], 4); self.assertEqual(out['notes_added'], 2); self.assertEqual(out['model'], 'fake-haiku')
        self.assertIn('The owner: Book my flu shot', self.calls[0][0]); self.assertIn('CVS store hours', self.calls[0][1])
        self.assertEqual(self.r.pending(), [])
        ctx = self.r.context()
        self.assertIn('MEMORY', ctx); self.assertIn('Rev. December 2025', ctx); self.assertIn('Open items', ctx); self.assertIn('cvs.com: The CVS store locator', ctx)
        self.assertLessEqual(len(ctx), 1600)
        self.assertEqual(self.r.digest(acts)['digest'], None)            # nothing new

    def test_the_next_digest_sees_open_items_on_file_and_closes_the_finished_ones(self):
        self.r.turn('chat', 'Book my flu shot at CVS', 'Looking.', 'r1')
        self.r.digest(None, reason='scheduled')
        [item] = [n for n in self.r.context().split('\n') if n.startswith('Open items')]
        self.assertIn('[1] Book the CVS flu shot', item)
        seen = {}
        def summarizer(convo, work, open_items):
            seen['open'] = open_items
            return {'summary': 'The CVS flu shot is booked for Saturday.', 'open_items': ['Book the CVS flu shot for Saturday Nov 14 (raised Nov 6)', 'Pick up the Leaveco form (raised Nov 7)'],
                    'resolved_item_ids': [1, 999, 'x']}
        self.r.summarizer = summarizer
        self.clock.t += 86400
        self.r.turn('chat', 'Flu shot booked, thanks. Remind me about the Leaveco form', 'Noted.', 'r2')
        out = self.r.digest(None, reason='scheduled')
        self.assertIn('[1] (Fri Nov 6) Book the CVS flu shot', seen['open'])       # the model saw what was on file
        self.assertEqual(out['items_closed'], 1)                                     # only a real id on file is closed
        self.assertEqual(out['notes_added'], 1)                                      # the repeated item is not re-added; Leaveco is
        st = self.r.status(); self.assertEqual(st['open_items'], 1)
        self.assertIn('Leaveco', self.r.context()); self.assertNotIn('[1] Book', self.r.context())

    def test_without_a_model_the_digest_is_extractive(self):
        r = Recall(Path(self.tmp.name) / 'r2.sqlite3', now=self.clock)
        r.turn('chat', 'Find flights to Lima in December', 'Here are three options.', 'x1')
        out = r.digest(None)
        self.assertIn('extractive', out['model']); self.assertIn('Lima', r.context())

    def test_search_notes_and_open_items(self):
        self.r.turn('chat', 'Remind me what Leaveco said about my claim', 'Leaveco said the claim is under review.', 'r1')
        self.r.digest(None)
        n = self.r.note('lesson', 'Leaveco absence form reloads after choosing the state; refill hours after it.', site='leaveco.example')
        self.assertEqual(n['status'], 'remembered'); self.assertEqual(self.r.note('lesson', 'Leaveco absence form reloads after choosing the state; refill hours after it.', site='leaveco.example')['status'], 'already remembered')
        hits = self.r.search('leaveco claim')['hits']
        self.assertTrue(any('Leaveco' in h['text'] for h in hits)); self.assertTrue(any(h['kind'].startswith('note') for h in hits))
        item = self.r.note('open_item', 'Call Leaveco on Monday about the claim status.')
        self.assertIn('[%d]' % item['id'], self.r.context())
        self.r.note('open_item', '', done_id=item['id'])
        self.assertNotIn('Call Leaveco on Monday', self.r.context())
        self.assertIn('Nothing', self.r.search('zebra')['note'])
        with self.assertRaises(ValueError): self.r.note('wish', 'not a kind of note at all')

    def test_site_lessons_reach_task_prepare(self):
        from broker import Broker
        from test_broker import Provider
        b = Broker(Path(self.tmp.name) / 'browser.sqlite3', Provider())
        b.recall.note('lesson', 'The CVS store locator needs the ZIP typed before the list appears.', site='cvs.com')
        self.assertEqual(b.recall.site_lessons('www.cvs.com')[0]['kind'], 'lesson')
        self.assertEqual(b.recall.site_lessons('walgreens.com'), [])

    def test_owner_controls_off_forget_and_permissions(self):
        self.r.turn('chat', 'Tell me about the Acme Telecom invoice', 'It is due Friday.', 'r1')
        with self.assertRaises(ValueError): self.r.call('recall_turn', {}, privileged=False)
        with self.assertRaises(ValueError): self.r.call('recall_forget', {'what': 'invoice'}, privileged=False)
        self.assertEqual(self.r.call('recall_forget', {'what': 'Acme Telecom'}, privileged=True)['forgotten'], 1)
        self.assertEqual(self.r.search('Acme Telecom')['hits'], [])
        self.r.call('recall_set', {'on': False}, privileged=True)
        self.assertEqual(self.r.call('recall_turn', {'session_kind': 'chat', 'owner_text': 'hi', 'reply_text': 'hello'}, privileged=True)['recorded'], False)
        self.assertEqual(self.r.context(), '')
        self.assertIn('memory is off', self.r.call('recall_note', {'kind': 'fact', 'text': 'The owner lives in Columbus, GA.'})['status'])
        self.r.call('recall_set', {'on': True}, privileged=True)
        self.assertEqual(self.r.status()['memory'], 'on')

    def test_the_nightly_digest_runs_once_between_330_and_4_eastern(self):
        self.r.turn('chat', 'hello there', 'hi', 'r1')
        self.assertFalse(self.r.due())
        self.clock.t = dt.datetime(2026, 11, 7, 3, 40, tzinfo=EASTERN).timestamp()
        self.assertTrue(self.r.due()); self.r.digest(None)
        self.r.turn('chat', 'late message', 'ok', 'r2'); self.clock.t += 300
        self.assertFalse(self.r.due())                                   # one digest per night

    def test_the_broker_wires_memory_through_both_sockets(self):
        from broker import Broker
        from test_broker import Provider
        b = Broker(Path(self.tmp.name) / 'browser.sqlite3', Provider())
        self.assertTrue((Path(self.tmp.name) / 'recall.sqlite3').exists())
        b.call('recall_turn', {'session_kind': 'chat', 'owner_text': 'Check the W-4 date', 'reply_text': 'Rev. December 2025', 'run_id': 'z'}, True)
        with self.assertRaises(ValueError): b.call('recall_turn', {'owner_text': 'forged'}, False)
        self.assertEqual(b.call('recall_digest', {'reason': 'test'}, True)['turns'], 2)
        self.assertTrue(b.call('recall_search', {'query': 'W-4'})['hits'])

    def test_0243_a_health_turn_is_refused_by_the_broker_too(self):
        from broker import Broker
        from test_broker import Provider
        b = Broker(Path(self.tmp.name) / 'browser.sqlite3', Provider())
        b.health.call('health_adult_confirm', {}, True)   # 0.25.0 (H8)
        c = b.health.call('health_connection_add', {'provider': 'mock_ehr', 'institution': 'Riverside', 'environment': 'fake'}, True)
        r = b.call('recall_turn', {'session_kind': 'chat', 'owner_text': 'For the synthetic Riverside connection %s propose a grant' % c['id'], 'reply_text': 'refused', 'run_id': 'h'}, True)
        self.assertEqual(r, {'recorded': False, 'reason': 'health turn'})
        self.assertFalse(b.call('recall_turn', {'session_kind': 'chat', 'owner_text': 'ok', 'reply_text': 'HEALTH GRANT REVIEW — x', 'run_id': 'h2'}, True)['recorded'])
        self.assertTrue(b.call('recall_turn', {'session_kind': 'chat', 'owner_text': 'Riverside park hours?', 'reply_text': '8 to 8', 'run_id': 'n'}, True)['recorded'])
        b.health.call('health_policy', {'set': {'recall': 'on'}}, True)
        self.assertTrue(b.call('recall_turn', {'session_kind': 'chat', 'owner_text': 'about %s' % c['id'], 'reply_text': 'ok', 'run_id': 'h3'}, True)['recorded'])

    def test_0243_internal_transport_text_is_not_his_words(self):
        self.assertEqual(self.r.turn('chat', '[Inter-session message] sourceSession=agent:main:subagent:c1 sourceChannel=internal so', 'ok', 'i1')['ids'].__len__(), 1)
        self.assertEqual(self.r.turn('chat', 'Continue the current task from the existing transcript, preserving completed work.', '', 'i2')['recorded'], False)
        with self.r.db() as db:
            db.execute("INSERT INTO turns(ts,session_kind,role,text,run_id) VALUES(?,?,?,?,?)", (self.clock.t, 'chat', 'owner', '[Inter-session message] sourceSession=x', 'old'))
            db.execute("INSERT INTO turns(ts,session_kind,role,text,run_id) VALUES(?,?,?,?,?)", (self.clock.t, 'chat', 'owner', 'Book the dentist', 'real'))
        self.r.summarizer = None; self.r.key_reader = None
        d = self.r.digest(None, force=True)
        self.assertNotIn('Inter-session', d['summary']); self.assertIn('Book the dentist', d['summary'])

    def test_0243_structured_summary_and_failure_category(self):
        from recall import summary_from, SummaryError
        self.assertEqual(summary_from({'content': [{'type': 'tool_use', 'name': 'save_summary', 'input': {'summary': 's'}}]}), {'summary': 's'})
        self.assertEqual(summary_from({'content': [{'type': 'text', 'text': 'Here: {"summary": "t"} done'}]}), {'summary': 't'})
        with self.assertRaisesRegex(SummaryError, 'no summary in reply .stop_reason end_turn, 9 text chars'):
            summary_from({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'No braces'}]})
        self.r.turn('chat', 'plan the trip', 'ok', 'q1')
        def bad(convo, work, open_items):
            raise SummaryError('no summary in reply (stop_reason max_tokens, 0 text chars)')
        self.r.summarizer = bad
        self.assertIn('stop_reason max_tokens', self.r.digest(None, force=True)['model'])

    def test_0243_forget_a_message_by_its_search_reference(self):
        ids = self.r.turn('chat', 'Riverside synthetic grant test', 'Refused: Riverside', 'run-x')['ids']
        hit = [h for h in self.r.search('Riverside')['hits'] if h['kind'] == 'turn']
        self.assertTrue(hit)
        self.assertEqual(self.r.forget('t#%d' % ids[0])['forgotten'], 2)          # both halves of the exchange
        self.assertEqual([h for h in self.r.search('Riverside')['hits'] if h['kind'] == 'turn'], [])
        self.assertEqual(self.r.forget('#%d' % ids[0])['forgotten'], 0)           # #N is a note, as the reply says

if __name__ == '__main__':
    unittest.main()
