"""Phase 2 (0.17.0): traces, page archives on trouble signals, the offline fixture runner, and the three weekly numbers."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
import fixtures
from broker import Broker
from test_broker import Provider, control
from test_tasks import CONTEXT

SEEDS = Path(__file__).resolve().parent / 'fixtures'

class CaptureProvider(Provider):
    def __init__(self):
        super().__init__(); self.captures = 0; self.traced = []
    def run(self, s, operation, **kw):
        if operation == 'capture':
            self.captures += 1
            Path(kw['capture_path']).write_text('MIME-Version: 1.0\n\n<html>archived</html>')
            return {'ok': True, 'bytes': 40, 'url': self.snap['url'], 'snapshot': copy.deepcopy(self.snap) | {'controls': [c | {'selector': 'private'} for c in self.snap['controls']]}}
        if operation == 'act' and kw.get('trace_path'):
            self.traced.append(kw['trace_path']); Path(kw['trace_path']).write_bytes(b'PK\x03\x04trace')
        return super().run(s, operation, **kw)

class LiveSideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = CaptureProvider()
        self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        self.inbox = Path(self.tmp.name) / 'inbox'; self.inbox.mkdir(); self.b.fixtures.inbox = self.inbox
        self.s = self.b.call('open', {'request_id': 'fixture-open-1', 'url': 'practice'})
        with self.b.db() as db: db.execute("UPDATE sessions SET origin='https://www.cvs.com',url='https://www.cvs.com/x',practice=0 WHERE id=?", (self.s['id'],))
        self.provider.snap.update({'url': 'https://www.cvs.com/scheduling/patient-details', 'controls': [control(), control('e1', 'button', 'Continue') | {'tag': 'button'}]})
    def tearDown(self): self.tmp.cleanup()
    def approve(self, rid, steps):
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': rid, 'steps': steps})
        self.b.call('review', {'id': p['id']}); r = self.b.plan(p['id'])
        return self.b.call('execute', {'id': p['id'], 'hash': r['hash'][:16]}, True)

    def test_every_act_is_traced_into_server_state_and_rotated(self):
        self.approve('fill-00001', [{'kind': 'fill', 'ref': 'e0', 'value': 'The owner'}])
        self.assertEqual(len(self.provider.traced), 1); self.assertFalse(Path(self.provider.traced[0]).exists())  # moved out of the inbox
        files = self.b.fixtures.trace_files(self.s['id'])
        self.assertEqual(len(files), 1)
        f = self.b.fixtures.trace_file(self.s['id'], files[0]['name']); self.assertEqual(oct(f.stat().st_mode & 0o777), '0o600')
        for bad in [('../x', files[0]['name']), (self.s['id'], '../../browser.sqlite3'), (self.s['id'], 'x.zip')]:
            with self.assertRaises(ValueError): self.b.fixtures.trace_file(*bad)
        old = time.time() - 8 * 86400; os.utime(f, (old, old))
        self.b.fixtures.last_rotate = 0; self.b.fixtures.rotate()
        self.assertEqual(self.b.fixtures.trace_files(self.s['id']), [])
        with self.b.db() as db: db.execute("INSERT OR REPLACE INTO owner_settings VALUES('tracing','off')")
        self.assertIsNone(self.b.fixtures.trace_path('abc'))

    def test_trouble_signals_archive_the_page_once(self):
        snap = {'form_gaps': [{'label': 'State'}]}
        self.assertEqual(self.b.fixtures.triggers({'status': 'completed', 'result': {'snapshot': snap, 'progress': {'changed': False}, 'fills': [{'ok': False}]}}),
                         ['form_gaps', 'fill_warnings', 'no_change'])
        self.assertEqual(self.b.fixtures.triggers({'status': 'not_dispatched', 'result': {'reason': 'choice_not_registered'}}), ['choice_not_registered'])
        self.provider.snap['form_gaps'] = [{'label': 'State', 'target': 'ps-select', 'why': 'closed web component'}]
        self.approve('fill-00002', [{'kind': 'fill', 'ref': 'e0', 'value': 'The owner'}])
        caps = list((Path(self.tmp.name) / 'fixtures').rglob('expect.json'))
        self.assertEqual(len(caps), 1); d = caps[0].parent
        self.assertEqual(d.parent.name, 'cvs.com'); self.assertIn('form_gaps', d.name)
        self.assertTrue((d / 'page.mhtml').is_file())
        self.assertNotIn('selector', (d / 'snapshot.json').read_text())
        expect = json.loads((d / 'expect.json').read_text())
        self.assertEqual(expect['targets'], [{'label': 'Continue', 'class': 'navigate'}])
        self.approve('fill-00003', [{'kind': 'fill', 'ref': 'e0', 'value': 'The owner B'}])   # same reason within 10 minutes: not again
        self.assertEqual(self.provider.captures, 1)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_sign_in_pages_are_never_archived(self):
        self.provider.snap['controls'].append(control('e2', 'password', 'Password') | {'value': '[manual entry only]'})
        self.assertIsNone(self.b.fixtures.capture(self.b.session(self.s['id']), 'takeover', 'abc'))
        self.assertEqual(self.provider.captures, 1); self.assertEqual(list((Path(self.tmp.name)).rglob('page.mhtml')), [])
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_takeover_archives_the_page_before_the_owner_acts(self):
        t = self.b.call('task_prepare', {'request_id': 'fixture-task-1', 'session_id': self.s['id'], 'title': 'Flu', 'goal': 'Book a flu shot.', 'mode': 'autonomous', 'facts': ['31901']})
        self.b.call('task_review', {'id': t['id']}); r = self.b.tasks.row(t['id'])
        self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        self.provider.api = lambda *a, **k: {'debuggerFullscreenUrl': 'https://www.browserbase.com/devtools/x'}
        with self.b.db() as db: db.execute("UPDATE sessions SET provider_id='p1' WHERE id=?", (self.s['id'],))
        self.b.call('takeover', {'id': self.s['id']}, True)
        self.assertEqual(self.provider.captures, 1)
        self.assertTrue(any('takeover' in p.name for p in (Path(self.tmp.name) / 'fixtures').rglob('*') if p.is_dir()))

    def test_model_cost_and_the_three_weekly_numbers(self):
        def task(rid, site_status, approvals=0, takeover=False, cost=None, autostarted=False):
            t = self.b.call('task_prepare', {'request_id': rid, 'session_id': self.s['id'], 'title': 'T', 'goal': 'Goal text.', 'mode': 'autonomous', 'facts': ['31901']})
            with self.b.db() as db:
                db.execute("UPDATE web_tasks SET status=?,activated=? WHERE id=?", (site_status, time.time() - 3600, t['id']))
            for _ in range(approvals): self.b.event(t['id'], 'owner_approval', {})
            if takeover: self.b.event(t['id'], 'takeover')
            if autostarted: self.b.event(t['id'], 'autostarted')
            if cost is not None:
                with self.assertRaises(ValueError): self.b.call('task_cost', {'id': t['id'], 'cost_usd': cost, 'tokens': 1000})
                self.b.call('task_cost', {'id': t['id'], 'cost_usd': cost, 'tokens': 1000}, True)
            return t['id']
        task('week-task-001', 'complete', approvals=1, cost=0.40)
        task('week-task-002', 'complete', approvals=0, takeover=True, cost=0.20, autostarted=True)
        t3 = task('week-task-003', 'needs_attention', cost=0.30)
        w = self.b.call('task_week', {'days': 7})
        cvs = w['sites'][0]
        self.assertEqual((cvs['site'], cvs['started'], cvs['completed']), ('cvs.com', 3, 2))
        self.assertAlmostEqual(cvs['completion_without_takeover'], 0.333)
        self.assertEqual(cvs['approvals_per_completed'], 1.0)          # (1 /webtask + 1 /webapprove) + (autostarted, 0) over 2
        self.assertAlmostEqual(cvs['cost_per_completed_usd'], 0.45)    # all model cost / completed tasks
        self.assertEqual(w['total']['started'], 3)
        self.assertEqual(self.b.tasks.metrics(t3)['model_cost_usd'], 0.3)
        with self.assertRaises(ValueError): self.b.call('task_cost', {'id': 'x' * 16, 'cost_usd': -1}, True)

class OfflineRunnerTests(unittest.TestCase):
    def test_seed_fixtures_pass_on_recorded_snapshots(self):
        results = fixtures.run([SEEDS], recorded=True)
        self.assertGreaterEqual(len(results), 2)
        self.assertTrue(all(r['ok'] for r in results), results)

    def test_a_serializer_that_drops_the_state_select_fails(self):
        d = SEEDS / 'cvs-state-dropdown'
        expect = json.loads((d / 'expect.json').read_text()); snap = json.loads((d / 'snapshot.json').read_text())
        self.assertEqual(fixtures.check(expect, snap), [])
        broken = dict(snap, controls=[c for c in snap['controls'] if c['label'] != 'State'])
        self.assertIn('control missing: State (select)', fixtures.check(expect, broken))
        unexposed = dict(snap, controls=[dict(c, hidden_native=False) if c['label'] == 'State' else c for c in snap['controls']])
        self.assertTrue(any('hidden native' in p for p in fixtures.check(expect, unexposed)))

    def test_reload_fixture_reads_as_already_submitted_and_final(self):
        d = SEEDS / 'leaveco-reload'
        expect = json.loads((d / 'expect.json').read_text()); snap = json.loads((d / 'snapshot.json').read_text())
        self.assertEqual(fixtures.check(expect, snap), [])
        relabelled = dict(snap, controls=[dict(c, label='Continue', type='button') if c['label'] == 'Submit' else c for c in snap['controls']])
        self.assertTrue(any('target missing: Submit' in p for p in fixtures.check(expect, relabelled)))
        self.assertTrue(any('verify' in p for p in fixtures.check(expect, dict(snap, text='Review your absence'))))

    @unittest.skipUnless(os.environ.get('PROCURATOR_TEST_CHROME') and os.environ.get('PROCURATOR_PLAYWRIGHT_PATH'), 'needs a local Chrome (PROCURATOR_TEST_CHROME, PROCURATOR_PLAYWRIGHT_PATH)')
    def test_seed_archives_reserialize_in_headless_chrome(self):
        started = time.time()
        results = fixtures.run([SEEDS])
        self.assertTrue(all(r['ok'] for r in results), results)
        self.assertLess(time.time() - started, 300)

if __name__ == '__main__':
    unittest.main()
