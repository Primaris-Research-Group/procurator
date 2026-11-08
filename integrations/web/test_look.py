"""Phase 3 (0.18.0): vision sidecar with a stub vision endpoint; click_xy only on a sidecar target, never on a final one."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from broker import Broker, digest
from look import clean
from test_broker import Provider, control
from test_tasks import CONTEXT

ORIGIN = 'https://www.cvs.com'
PAGE = ORIGIN + '/scheduling/slots'

class LookProvider(Provider):
    def __init__(self):
        super().__init__(); self.point = {'in_viewport': True, 'found': True, 'tag': 'div', 'type': '', 'role': '', 'label': 'Thu 11/12', 'href': '', 'form_action': ''}
        self.looks = 0; self.clicked = []
    def run(self, s, operation, **kw):
        if operation == 'look':
            self.looks += 1
            if any(c.get('type') == 'password' for c in self.snap['controls']): return {'ok': False, 'kind': 'look_refused_signin'}
            return {'ok': True, 'image_b64': 'aGVsbG8=', 'marks': [{'ref': 'e1', 'label': 'Continue', 'tag': 'button', 'type': 'button', 'box': [10, 10, 80, 30]}],
                    'viewport': {'width': 1280, 'height': 800, 'dpr': 1}, 'url': self.snap['url'], 'digest': digest(self.snap)}
        if operation == 'point': return {'ok': True, 'element': dict(self.point)}
        if operation == 'act':
            for st in kw['steps']:
                if st['kind'] == 'click_xy': self.clicked.append((st['x'], st['y']))
        return super().run(s, operation, **kw)

def vision(answer='A calendar of dates.', targets=None, confirmation=None):
    calls = []
    def caller(key, model, image, marks, question):
        calls.append((model, question))
        return {'answer': answer, 'page_state': 'date picker', 'refs_seen': [{'ref': 'e1', 'what': 'next button'}, {'ref': 'e99', 'what': 'invented'}],
                'unlisted_targets': targets if targets is not None else [{'bbox': [400, 300, 60, 40], 'description': 'Thu 11/12', 'likely_role': 'button'}],
                'confirmation_number': confirmation}, 0.004
    return caller, calls

class LookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = LookProvider()
        self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]); dns.start(); self.addCleanup(dns.stop)
        self.s = self.b.call('open', {'request_id': 'look-open-1', 'url': 'practice'})
        with self.b.db() as db: db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (ORIGIN, PAGE, self.s['id']))
        self.provider.snap.update({'url': PAGE, 'title': 'Slots', 'text': 'Pick a date', 'controls': [control('e1', 'button', 'Continue') | {'tag': 'button', 'form_action': ''}], 'page_errors': []})
        self.b.look.key_reader = lambda: 'test-key'
        self.caller, self.calls = vision(); self.b.look.caller = self.caller
    def tearDown(self): self.tmp.cleanup()
    def task(self):
        t = self.b.call('task_prepare', {'request_id': 'look-task-1', 'session_id': self.s['id'], 'title': 'Flu', 'goal': 'Book a flu shot.', 'mode': 'autonomous', 'facts': ['31901']})
        self.b.call('task_review', {'id': t['id']}); r = self.b.tasks.row(t['id'])
        self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        return t['id']

    def test_look_returns_text_only_with_known_refs_and_targets(self):
        out = self.b.call('look', {'id': self.s['id'], 'question': 'Where is Thursday?'})
        self.assertEqual(out['answer'], 'A calendar of dates.'); self.assertEqual(out['model'], 'claude-sonnet-5-5')
        self.assertEqual([r['ref'] for r in out['refs_seen']], ['e1'])            # invented refs are dropped
        self.assertEqual(out['unlisted_targets'][0]['target'], 't1')
        self.assertNotIn('image', json.dumps(out)); self.assertNotIn('aGVsbG8=', json.dumps(out))
        with self.b.db() as db: row = db.execute('SELECT image_sha,result FROM looks').fetchone()
        self.assertEqual(len(row['image_sha']), 64)

    def test_budget_key_and_sign_in_refusals(self):
        with self.b.db() as db: db.execute("INSERT OR REPLACE INTO owner_settings VALUES('look_budget_per_task','2')")
        self.b.call('look', {'id': self.s['id']}); self.b.call('look', {'id': self.s['id']})
        self.assertIn('budget', self.b.call('look', {'id': self.s['id']})['skipped'])
        self.assertEqual(self.provider.looks, 2)
        self.b.look.key_reader = lambda: None
        self.assertIn('no vision key', self.b.call('look', {'id': self.s['id']})['skipped'])

    def test_sign_in_page_never_looked_at(self):
        self.provider.snap['controls'].append(control('e2', 'password', 'Password') | {'value': '[manual entry only]'})
        self.assertIn('sign-in', self.b.call('look', {'id': self.s['id']})['skipped'])

    def test_click_xy_on_a_sidecar_target_runs_as_a_routine_step(self):
        t = self.task()
        lk = self.b.call('look', {'id': self.s['id'], 'question': 'Which date?'})
        out = self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'xy-step-0001', 'steps': [{'kind': 'click_xy', 'look_id': lk['look_id'], 'target': 't1'}]})
        self.assertEqual(out['status'], 'completed'); self.assertEqual(self.provider.clicked, [(430, 320)])
        with self.b.db() as db: j = db.execute("SELECT kind,label,screenshot_hash FROM task_journal").fetchone()
        self.assertEqual(j['kind'], 'click'); self.assertEqual(j['label'], 'Thu 11/12'); self.assertEqual(len(j['screenshot_hash']), 64)

    def test_click_xy_refused_on_final_targets_listed_controls_stale_looks_and_changed_pages(self):
        t = self.task()
        self.b.look.caller = vision(targets=[{'bbox': [400, 300, 60, 40], 'description': 'Confirm appointment', 'likely_role': 'button'}])[0]
        lk = self.b.call('look', {'id': self.s['id']})
        with self.assertRaisesRegex(ValueError, 'never used for something that submits'):
            self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'xy-final-001', 'steps': [{'kind': 'click_xy', 'look_id': lk['look_id'], 'target': 't1'}]})
        self.b.look.caller = self.caller
        self.provider.point['label'] = 'Place order'
        lk = self.b.call('look', {'id': self.s['id']})
        with self.assertRaisesRegex(ValueError, 'never used'):
            self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'xy-final-002', 'steps': [{'kind': 'click_xy', 'look_id': lk['look_id'], 'target': 't1'}]})
        self.provider.point['label'] = 'Continue'; self.provider.point['tag'] = 'button'
        lk = self.b.call('look', {'id': self.s['id']})
        with self.assertRaisesRegex(ValueError, 'use the ref'):
            self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'xy-listed-01', 'steps': [{'kind': 'click_xy', 'look_id': lk['look_id'], 'target': 't1'}]})
        self.provider.point.update(label='Thu 11/12', tag='div')
        lk = self.b.call('look', {'id': self.s['id']})
        self.provider.snap['text'] = 'Page changed'
        with self.assertRaisesRegex(ValueError, 'page changed'):
            self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'xy-stale-001', 'steps': [{'kind': 'click_xy', 'look_id': lk['look_id'], 'target': 't1'}]})
        with self.assertRaisesRegex(ValueError, 'Unknown look_id'):
            self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'xy-unknown01', 'steps': [{'kind': 'click_xy', 'look_id': 'nope', 'target': 't1'}]})
        self.assertEqual(self.provider.clicked, [])

    def test_automatic_look_after_a_final_press_reads_the_confirmation(self):
        self.b.look.caller, calls = vision(confirmation='A-12345')
        self.provider.snap['controls'] = [control('e1', 'submit', 'Submit') | {'tag': 'button', 'form_action': ''}]
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'final-0001', 'steps': [{'kind': 'click', 'ref': 'e1'}]})
        self.b.call('review', {'id': p['id']}); r = self.b.plan(p['id'])
        out = self.b.call('execute', {'id': p['id'], 'hash': r['hash'][:16]}, True)
        self.assertEqual(out['after_submit_look']['confirmation_number'], 'A-12345'); self.assertEqual(calls[0][0], 'claude-haiku-5-5')

    def test_targets_outside_the_viewport_or_malformed_are_dropped(self):
        out = clean({'unlisted_targets': [{'bbox': [2550, 1590, 50, 50], 'description': 'x'}, {'bbox': 'bad'}, {'bbox': [10, 10, 20, 20], 'description': 'ok'}]}, [], {'width': 1280, 'height': 800, 'dpr': 2})
        self.assertEqual(len(out['unlisted_targets']), 1); self.assertEqual(out['unlisted_targets'][0]['bbox'], [5, 5, 10, 10])



class FallbackTests(LookTests):
    def test_openai_is_used_when_anthropic_is_missing_or_fails(self):
        used = []
        def oai(key, model, image, marks, question):
            used.append((key, model)); return {'answer': 'From OpenAI.', 'unlisted_targets': []}, 0.001
        self.b.look.openai_reader = lambda: 'sk-test'; self.b.look.openai_caller = oai
        self.b.look.key_reader = lambda: None
        out = self.b.call('look', {'id': self.s['id']})
        self.assertEqual((out['answer'], out['model']), ('From OpenAI.', 'gpt-6.1-terra'))
        def broken(*a): raise TimeoutError()
        self.b.look.key_reader = lambda: 'ak'; self.b.look.caller = broken
        self.assertEqual(self.b.call('look', {'id': self.s['id']})['model'], 'gpt-6.1-terra')
        self.b.look.openai_reader = lambda: None
        self.assertIn('unavailable (TimeoutError)', self.b.call('look', {'id': self.s['id']})['skipped'])

if __name__ == '__main__':
    unittest.main()
