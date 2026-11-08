"""Phase 4 (0.18.0): owner facts vault, trajectory hints, review buttons and the watch-only live view."""
import copy
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from broker import Broker
from guard import model_view
from memory import Memory
from test_broker import Provider, control
from test_tasks import CONTEXT

ORIGIN = 'https://www.leaveco.example'
FORM = ORIGIN + '/absence/report'
PHONE = '706-555-0100'

class VaultProvider(Provider):
    def __init__(self):
        super().__init__(); self.typed = []; self.buttons = []
    def run(self, s, operation, **kw):
        if operation == 'act':
            out = super().run(s, operation, **kw)
            if out.get('ok'):
                for st in kw['steps']:
                    if st['kind'] == 'fill':
                        self.typed.append(st['value'])
                        for c in self.snap['controls']:
                            if c['ref'] == st['control']['ref']: c['value'] = st['value']
            return out
        return super().run(s, operation, **kw)
    def telegram_buttons(self, message, rows):
        self.messages.append(message); self.buttons.append(rows)
    def api(self, method, path, body=None, timeout=35):
        return {'debuggerFullscreenUrl': 'https://www.browserbase.com/devtools-fullscreen/inspector.html?wss=x'}

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = VaultProvider()
        self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]); dns.start(); self.addCleanup(dns.stop)
        self.s = self.b.call('open', {'request_id': 'vault-open-1', 'url': 'practice'})
        with self.b.db() as db: db.execute("UPDATE sessions SET origin=?,url=?,practice=0,provider_id='p1' WHERE id=?", (ORIGIN, FORM, self.s['id']))
        self.provider.snap.update({'url': FORM, 'title': 'Report', 'text': 'Absence form', 'page_errors': [],
            'controls': [control('e0', 'tel', 'Phone number') | {'form_action': ''}, control('e1', 'button', 'Next') | {'tag': 'button', 'form_action': ''}]})
        self.b.vault.set('owner.phone', PHONE); self.b.vault.set('owner.address.zip', '31901')
    def tearDown(self): self.tmp.cleanup()
    def task(self, rid='vault-task-01', **extra):
        t = self.b.call('task_prepare', {'request_id': rid, 'session_id': self.s['id'], 'title': 'Leaveco absence', 'goal': 'Report the Nov 3 absence.', 'mode': 'autonomous', 'facts': ['4']} | extra)
        self.b.call('task_review', {'id': t['id']}); r = self.b.tasks.row(t['id'])
        self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        return t

class VaultTests(Base):
    def test_owner_fact_is_typed_by_the_broker_and_never_shown(self):
        t = self.task()
        self.assertEqual(t['owner_fact_keys']['keys'], ['owner.address.zip', 'owner.phone'])
        self.assertNotIn(PHONE, json.dumps(t))
        out = self.b.call('task_step', {'id': t['id'], 'runtime_context': CONTEXT, 'request_id': 'vault-step-01', 'steps': [{'kind': 'fill', 'ref': 'e0', 'value': '{{owner.phone}}'}]})
        self.assertEqual(out['status'], 'completed'); self.assertEqual(self.provider.typed, [PHONE])
        with self.b.db() as db: plan = db.execute('SELECT plan FROM plans').fetchone()['plan']
        self.assertIn('{{owner.phone}}', plan)
        # What reaches the model over the tools socket: the value is replaced by its placeholder everywhere.
        snap = self.b.call('snapshot', {'id': self.s['id']})
        self.assertIn(PHONE, json.dumps(snap))                                     # raw inside the broker
        self.assertNotIn(PHONE, json.dumps(self.b.vault.redact(model_view(snap))))  # what the socket sends
        self.assertIn('{{owner.phone}}', json.dumps(self.b.vault.redact(model_view(snap))))

    def test_review_and_activity_show_it_masked(self):
        self.provider.snap['controls'][0]['value'] = PHONE
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'vault-rev-01', 'steps': [{'kind': 'fill', 'ref': 'e0', 'value': '{{owner.phone}}'}]})
        self.b.call('review', {'id': p['id']})
        review = next(m for m in self.provider.messages if 'BROWSER ACTION REVIEW' in m)
        self.assertNotIn(PHONE, review); self.assertIn('saved owner.phone (…0100)', review)
        r = self.b.plan(p['id']); self.b.call('execute', {'id': p['id'], 'hash': r['hash'][:16]}, True)
        import sqlite3
        rows = [list(r) for r in sqlite3.connect(str(self.b.activity.path)).execute('SELECT * FROM activity')]
        self.assertTrue(rows); self.assertNotIn(PHONE, json.dumps(rows)); self.assertIn('saved owner.phone', json.dumps(rows))
        for m in self.provider.messages: self.assertNotIn(PHONE, m)

    def test_unknown_keys_and_refused_values(self):
        with self.assertRaisesRegex(ValueError, 'No saved owner fact named owner.email'):
            self.b.call('prepare', {'id': self.s['id'], 'request_id': 'vault-bad-01', 'steps': [{'kind': 'fill', 'ref': 'e0', 'value': '{{owner.email}}'}]})
        for k, v in [('owner.ssn', 'x'), ('owner.card', '1'), ('owner.note', '4111 1111 1111 1111'), ('owner.code', 'x' * 201)]:
            with self.assertRaises(ValueError): self.b.vault.set(k, v)
        self.assertEqual(oct((Path(self.tmp.name) / 'owner-facts.json').stat().st_mode & 0o777), '0o600')

    def test_traces_are_scrubbed_of_saved_values(self):
        z = Path(self.tmp.name) / 'trace.zip'
        with zipfile.ZipFile(z, 'w') as f: f.writestr('trace.trace', 'typed ' + PHONE + ' into Phone'); f.writestr('x.png', b'\x89PNG')
        self.b.vault.scrub_file(z)
        with zipfile.ZipFile(z) as f: self.assertEqual(f.read('trace.trace').decode(), 'typed {{owner.phone}} into Phone')

class MemoryTests(Base):
    def test_completed_task_leaves_hints_for_the_next_one_and_a_stuck_step_retires_them(self):
        t = self.task()
        step = lambda rid, steps: self.b.call('task_step', {'id': t['id'], 'runtime_context': CONTEXT, 'request_id': rid, 'steps': steps})
        step('mem-step-001', [{'kind': 'fill', 'ref': 'e0', 'value': '{{owner.phone}}'}]); step('mem-step-002', [{'kind': 'click', 'ref': 'e1'}])
        self.provider.snap['controls'] = [control('e2', 'submit', 'Submit') | {'tag': 'button', 'form_action': ''}]
        stop = step('mem-step-003', [{'kind': 'click', 'ref': 'e2'}])
        self.b.call('review', {'id': stop['action_id']}); r = self.b.plan(stop['action_id'])
        self.b.call('execute', {'id': r['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        self.provider.snap['text'] = 'Your absence was submitted. Confirmation number 7781.'
        self.assertEqual(self.b.call('task_complete', {'id': t['id'], 'runtime_context': CONTEXT})['status'], 'complete')
        # Next task on the same site gets the hints (labels only, never values).
        s2 = self.b.call('open', {'request_id': 'vault-open-2', 'url': 'practice'})
        with self.b.db() as db: db.execute("UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?", (ORIGIN, FORM, s2['id']))
        t2 = self.b.call('task_prepare', {'request_id': 'vault-task-02', 'session_id': s2['id'], 'title': 'Leaveco absence', 'goal': 'Report the Nov 10 absence.', 'mode': 'autonomous', 'facts': ['4']})
        self.assertIn('Last time on leaveco.example', t2['hints']['text']); self.assertIn('"Phone number"', t2['hints']['text'])
        self.assertIn('press (final, needs approval) "Submit"', t2['hints']['text']); self.assertNotIn(PHONE, json.dumps(t2))
        self.assertEqual(self.b.memory.invalidate('leaveco.example', 'Next', 'stuck'), 1)
        self.assertIsNone(self.b.memory.hints('leaveco.example'))

    def test_hints_expire(self):
        m = Memory(Path(self.tmp.name) / 'm.sqlite3')
        m.record('cvs.com', 'flu', [{'kind': 'click', 'label': 'Schedule'}, {'kind': 'fill', 'label': 'ZIP'}], 1, 't1')
        self.assertIsNotNone(m.hints('cvs.com'))
        self.assertIsNone(m.hints('cvs.com', now=time.time() + 61 * 86400))

class ButtonsAndWatchTests(Base):
    def test_reviews_carry_copy_lines_and_a_single_use_watch_link(self):
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'btn-rev-001', 'steps': [{'kind': 'click', 'ref': 'e1'}]})
        self.b.call('review', {'id': p['id']}); r = self.b.plan(p['id'])
        rows = self.provider.buttons[-1]
        self.assertEqual(rows[0][0]['copy_text']['text'], '/webapprove %s %s' % (p['id'], r['hash'][:16]))
        self.assertTrue(all('callback_data' not in b for row in rows for b in row))        # nothing approves by tapping
        watch = rows[-1][0]['url']; token = watch.rsplit('/', 1)[1]
        self.assertTrue(watch.startswith('https://assistant.example.com/assistant/dashboard/watch/'))
        w = self.b.watch_open(token); self.assertIn('navbar=false', w['live_view_url'])
        with self.assertRaisesRegex(ValueError, 'already opened'): self.b.watch_open(token)
        with self.assertRaisesRegex(ValueError, 'expired'): self.b.watch_open('0' * 64)

    def test_watch_link_tool_sends_to_the_owner_not_the_model(self):
        out = self.b.call('watch_link', {'id': self.s['id']})
        self.assertEqual(out, {'id': self.s['id'], 'sent': True, 'note': 'A watch-only link was sent to the owner in Telegram.'})
        self.assertNotIn('watch/', json.dumps(out)); self.assertIn('watch/', json.dumps(self.provider.buttons[-1]))

if __name__ == '__main__':
    unittest.main()
