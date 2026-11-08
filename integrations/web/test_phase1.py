"""Phase 0/1: task metrics, no-progress and failure budgets, owner screenshots, stable digests."""
import base64
import tempfile
import unittest
from pathlib import Path
from broker import Broker, digest
from test_broker import Provider, control
from test_tasks import CONTEXT

ORIGIN = 'https://www.cvs.com'
SCHED = ORIGIN + '/scheduling/scheduler'


def button(ref, label):
    return control(ref, 'button', label) | {'tag': 'button', 'role': '', 'form_action': '', 'name': '', 'id': ''}


class Shots(Provider):
    def __init__(self):
        super().__init__(); self.photos = []; self.changed = True; self.dispatch = True
    def run(self, s, operation, **kw):
        if operation == 'screenshot':
            return {'ok': True, 'image_b64': base64.b64encode(b'\xff\xd8fakejpeg').decode(), 'url': SCHED}
        if operation == 'act' and not self.dispatch:
            return {'ok': False, 'kind': 'not_dispatched', 'reason': 'control_not_clickable'}
        out = super().run(s, operation, **kw)
        if operation == 'act' and out.get('ok'):
            out = {**out, 'progress': {'changed': self.changed, 'url_changed': False, 'new_controls': 0, 'removed_controls': 0,
                                       'values_changed': False, 'new_errors': [], 'new_notices': [], 'field_errors': [], 'text_changed': False},
                   'fills': []}
        return out
    def telegram_photo(self, image, caption): self.photos.append((image, caption))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Shots(); self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', self.p)
        self.s = self.b.call('open', {'request_id': 'p1-open-1', 'url': 'practice'})
        with self.b.db() as db: db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (ORIGIN, SCHED, self.s['id']))
        self.p.snap.update({'url': SCHED, 'title': 'CVS', 'text': 'Pick a time', 'controls': [button('e1', 'Continue'), button('e2', 'Next week')]})
        t = self.b.call('task_prepare', {'request_id': 'p1-task-1', 'session_id': self.s['id'], 'title': 'Flu shot', 'goal': 'Book a flu shot.', 'mode': 'autonomous'})
        self.b.call('task_review', {'id': t['id']}); r = self.b.tasks.row(t['id'])
        self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True); self.t = t['id']
    def step(self, rid, ref='e1'):
        return self.b.call('task_step', {'id': self.t, 'runtime_context': CONTEXT, 'request_id': 'phase1-step-' + rid, 'steps': [{'kind': 'click', 'ref': ref}]})
    def test_each_step_returns_validation_guidance(self):
        r = self.step('a'); self.assertEqual(r['status'], 'completed'); self.assertIn('State in one sentence', r['validate'])
    def test_three_no_change_steps_pause_and_alert_owner_with_screenshot(self):
        self.p.changed = False
        for i in range(2):
            r = self.step('n%d' % i); self.assertIn('Nothing visible changed', r['validate']); self.assertNotIn('stop_reason', r)
        r = self.step('n2')
        self.assertEqual(r['stop_reason'], 'budget'); self.assertEqual(self.b.tasks.row(self.t)['status'], 'needs_attention')
        self.assertIn('BROWSER TASK PAUSED', self.p.messages[-1]); self.assertIn('3 steps in a row changed nothing', self.p.messages[-1])
        self.assertEqual(len(self.p.photos), 1)
        with self.assertRaisesRegex(ValueError, 'needs_attention'): self.step('n3')
    def test_progress_resets_streak_and_five_failures_pause(self):
        self.p.changed = False; self.step('x0'); self.step('x1'); self.p.changed = True; self.step('x2'); self.p.changed = False
        self.assertNotIn('stop_reason', self.step('x3'))
        self.p.dispatch = False
        for i in range(4): self.assertNotIn('stop_reason', self.step('f%d' % i, 'e2'))
        self.assertEqual(self.step('f4', 'e2')['stop_reason'], 'budget')
    def test_metrics_and_webstats_summary(self):
        self.step('m1'); self.p.changed = False; self.step('m2')
        stats = self.b.call('task_stats', {})
        t = stats['tasks'][0]
        self.assertEqual((t['steps'], t['no_change_steps'], t['not_dispatched']), (2, 1, 0)); self.assertEqual(stats['summary']['tasks'], 1)
        self.assertIsNone(t['model_cost_usd'])  # 0.17.0: filled from the gateway usage hook (task_cost)
    def test_reviews_carry_a_screenshot_and_webshot_is_owner_only(self):
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'p1-manual-1', 'steps': [{'kind': 'click', 'ref': 'e2'}]})
        r = self.b.call('review', {'id': p['id']}); self.assertTrue(r['screenshot_sent']); self.assertIn('what you are approving', self.p.photos[-1][1])
        with self.assertRaisesRegex(ValueError, 'only to the authenticated owner'): self.b.call('owner_screenshot', {'id': self.s['id']})
        self.assertTrue(self.b.call('owner_screenshot', {'id': self.s['id']}, True)['sent'])
    def test_display_only_fields_do_not_change_the_approval_fingerprint(self):
        snap = {'url': SCHED, 'controls': [], 'text': 'x'}
        self.assertEqual(digest(snap), digest({**snap, 'new_refs': ['e1'], 'site_login_status': 'signed_out', 'site_login_guidance': 'x'}))
        self.assertNotEqual(digest(snap), digest({**snap, 'text': 'y'}))

if __name__ == '__main__':
    unittest.main()
