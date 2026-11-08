"""Phase 7 (0.19.0): playbooks from 3 completed runs; replay through the normal task step; drift; demotion; no values kept."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from broker import Broker, digest
from test_broker import Provider, control
from test_tasks import CONTEXT

ORIGIN = 'https://www.leaveco.example'
PHONE = '706-555-0100'

def page_a(label='Phone number'):
    return {'url': ORIGIN + '/absence/contact', 'title': 'Contact', 'text': 'Contact details', 'page_errors': [],
            'controls': [control('e0', 'tel', label) | {'form_action': ''}, control('e1', 'button', 'Next') | {'tag': 'button', 'form_action': ''},
                         control('e5', 'button', 'Help') | {'tag': 'button', 'form_action': ''}]}
PAGE_B = {'url': ORIGIN + '/absence/review', 'title': 'Review', 'text': 'Review your absence', 'page_errors': [],
          'controls': [control('e2', 'submit', 'Submit') | {'tag': 'button', 'form_action': ''}, control('e3', 'button', 'Go Back') | {'tag': 'button', 'form_action': ''}]}

class FlowProvider(Provider):
    def __init__(self):
        super().__init__(); self.snap = copy.deepcopy(page_a()); self.typed = []
    def run(self, s, operation, **kw):
        if operation == 'act':
            if kw['expected'] != digest(self.snap): return {'ok': False, 'kind': 'stale'}
            self.acts += 1
            for st in kw['steps']:
                if st['kind'] == 'fill': self.typed.append(st['value'])
                if st['kind'] == 'click' and st['control']['label'] == 'Next': self.snap = copy.deepcopy(PAGE_B)
                if st['kind'] == 'click' and st['control']['label'] == 'Submit': self.snap['text'] = 'Your absence was submitted. Confirmation number 7781.'
            return {'ok': True, 'snapshot': copy.deepcopy(self.snap), 'progress': {'changed': True}, 'fills': []}
        return {'ok': True, 'snapshot': copy.deepcopy(self.snap)}

class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = FlowProvider()
        self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]); dns.start(); self.addCleanup(dns.stop)
        self.b.vault.set('owner.phone', PHONE); self.n = 0
    def tearDown(self): self.tmp.cleanup()
    def task(self, label='Phone number'):
        self.n += 1; self.provider.snap = copy.deepcopy(page_a(label))
        s = self.b.call('open', {'request_id': 'pb-open-%04d' % self.n, 'url': 'practice'})
        with self.b.db() as db: db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (ORIGIN, ORIGIN + '/', s['id']))
        t = self.b.call('task_prepare', {'request_id': 'pb-task-%04d' % self.n, 'session_id': s['id'], 'title': 'Leaveco absence', 'goal': 'Report an absence for Monday.', 'mode': 'autonomous', 'facts': ['4']})
        self.b.call('task_review', {'id': t['id']}); r = self.b.tasks.row(t['id'])
        self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        return t['id']
    def step(self, t, rid, steps): return self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': rid, 'steps': steps})
    def full_run(self):
        t = self.task()
        self.step(t, 'pb-fill-%04d' % self.n, [{'kind': 'fill', 'ref': 'e0', 'value': '{{owner.phone}}'}])
        self.step(t, 'pb-next-%04d' % self.n, [{'kind': 'click', 'ref': 'e1'}])
        stop = self.step(t, 'pb-subm-%04d' % self.n, [{'kind': 'click', 'ref': 'e2'}])
        self.b.call('review', {'id': stop['action_id']}); r = self.b.plan(stop['action_id'])
        self.b.call('execute', {'id': r['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        self.assertEqual(self.b.call('task_complete', {'id': t, 'runtime_context': CONTEXT})['status'], 'complete')

    def test_three_runs_make_a_playbook_that_replays_routine_steps_and_stops_at_the_gated_one(self):
        t0 = self.task()
        self.assertEqual(self.b.call('task_playbook', {'id': t0, 'runtime_context': CONTEXT})['stopped'], 'no_playbook')
        self.b.call('task_stop', {'id': t0, 'context': CONTEXT}, True)
        for _ in range(3): self.full_run()
        pb = self.b.playbooks.stats()[0]
        self.assertEqual((pb['site'], pb['runs'], pb['steps']), ('leaveco.example', 3, 3))
        self.assertNotIn(PHONE, json.dumps(self.b.playbooks.find('leaveco.example', 'Leaveco absence — Report an absence for Monday.')))
        t = self.task(); acts = self.provider.acts
        out = self.b.call('task_playbook', {'id': t, 'runtime_context': CONTEXT})
        self.assertEqual([x['label'] for x in out['replayed']], ['Phone number', 'Next'])
        self.assertEqual(out['stopped'], 'needs_you'); self.assertIn('Submit', out['next']); self.assertIn('gated', out['next'])
        self.assertEqual(self.provider.acts, acts + 2); self.assertEqual(self.provider.typed[-1], PHONE)   # the vault value, typed by the broker
        self.assertEqual(self.b.playbooks.stats()[0]['hits'], 2)

    def test_drift_hands_back_to_the_model_and_failures_demote(self):
        for _ in range(3): self.full_run()
        t = self.task(label='Mobile phone')                         # the site renamed the field: fingerprint and label differ
        out = self.b.call('task_playbook', {'id': t, 'runtime_context': CONTEXT})
        self.assertEqual(out['replayed'], []); self.assertEqual(out['stopped'], 'drift')
        pid = 1
        for _ in range(5): self.b.playbooks.note(pid, False)
        self.assertTrue(self.b.playbooks.stats()[0]['demoted'])
        self.assertEqual(self.b.call('task_playbook', {'id': t, 'runtime_context': CONTEXT})['stopped'], 'demoted')

    def test_only_its_own_conversation_can_replay(self):
        t = self.task()
        with self.assertRaises(ValueError):
            self.b.call('task_playbook', {'id': t, 'runtime_context': dict(CONTEXT, sessionId='other')})

if __name__ == '__main__':
    unittest.main()
