"""Phase 6 (0.18.0): router and read-only adapters, uploads, frames, mobile-layout trials, two-speed task loop."""
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from broker import Broker
from test_broker import Provider, control
from test_tasks import CONTEXT

ORIGIN = 'https://www.leaveco.example'
FORM = ORIGIN + '/absence/report'
WORKER = {'agentId': 'main', 'sessionKey': None, 'sessionId': 'worker-session'}

class P6Provider(Provider):
    def __init__(self):
        super().__init__(); self.acts_seen = []; self.options = []
    def run(self, s, operation, **kw):
        self.options.append((operation, {k: v for k, v in kw.items() if k in ('frame_origins', 'viewport')}))
        if operation == 'act':
            self.acts_seen.append(copy.deepcopy(kw['steps']))
            for st in kw['steps']:
                if st['kind'] == 'upload':
                    self.uploaded = Path(st['file_path']).read_bytes()
        return super().run(s, operation, **kw)

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = P6Provider()
        self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]); dns.start(); self.addCleanup(dns.stop)
        self.inbox = Path(self.tmp.name) / 'inbox'; self.inbox.mkdir(); self.b.uploads.inbox = self.inbox
        self.s = self.b.call('open', {'request_id': 'p6-open-001', 'url': 'practice'})
        with self.b.db() as db: db.execute("UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?", (ORIGIN, FORM, self.s['id']))
        self.provider.snap.update({'url': FORM, 'title': 'Report', 'text': 'Absence form', 'page_errors': [],
            'controls': [control('e0', 'file', 'Supporting document') | {'form_action': ''}, control('e1', 'button', 'Next') | {'tag': 'button', 'form_action': ''}]})
    def tearDown(self): self.tmp.cleanup()
    def task(self, rid='p6-task-0001', **extra):
        t = self.b.call('task_prepare', {'request_id': rid, 'session_id': self.s['id'], 'title': 'Leaveco absence', 'goal': 'Report the Nov 3 absence.', 'mode': 'autonomous', 'facts': ['4']} | extra)
        self.b.call('task_review', {'id': t['id']}); r = self.b.tasks.row(t['id'])
        self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        return t
    def stage(self, name='note.pdf', data=b'%PDF-1.4 doctor note'):
        n = 'upload-' + 'a' * 32 + '.bin'; (self.inbox / n).write_bytes(data)
        return self.b.call('upload_stage', {'session_id': self.s['id'], 'inbox_name': n, 'name': name})

class RouterTests(Base):
    def key(self, name, value):
        d = Path(self.tmp.name) / 'keys'; d.mkdir(exist_ok=True); (d / (name + '.key')).write_text(value)
    def test_routes_and_reasons(self):
        r = self.b.call('route', {'site': 'www.wellsfargo.com', 'intent': 'check my checking balance'})
        self.assertEqual(r['route'], 'takeover'); self.assertIn('SimpleFIN is not connected', r['reason'])
        self.key('simplefin', 'https://user:pass@bridge.simplefin.org/simplefin')
        self.assertEqual(self.b.call('route', {'site': 'wellsfargo.com', 'intent': 'what is my balance'})['route'], 'api')
        r = self.b.call('route', {'site': 'delta.com', 'intent': 'find flights ATL to MIA'})
        self.assertIn('Duffel', ' '.join(r['alternatives']))
        self.key('duffel', 'duffel_test_x')
        self.assertEqual(self.b.call('route', {'site': '', 'intent': 'round-trip flights to Miami'})['route'], 'api')
        r = self.b.call('route', {'site': 'leaveco.example', 'intent': 'report an absence'})
        self.assertEqual(r['route'], 'browse:cloud'); self.assertIn('predicted', r['reason'])   # 0.19.2: no PC registered -> cloud, said why
        self.b.router.note_platform('shop.example.com', 'shopify')
        self.assertEqual(self.b.call('route', {'site': 'shop.example.com', 'intent': 'order the blue mug'})['route'], 'mcp')
        share = self.b.router.share(0)
        self.assertEqual(share['routed'], 6); self.assertEqual(share['needed_browser'], 2)

    def test_duffel_offers_are_summarized_read_only(self):
        self.key('duffel', 'duffel_test_x'); sent = {}
        def opener(req, timeout):
            sent['url'] = req.full_url; sent['headers'] = dict(req.header_items()); sent['body'] = json.loads(req.data)
            return io.BytesIO(json.dumps({'data': {'offers': [
                {'total_amount': '310.20', 'total_currency': 'USD', 'owner': {'name': 'Delta'}, 'expires_at': 'x', 'conditions': {'refund_before_departure': {'allowed': False}},
                 'slices': [{'duration': 'PT1H50M', 'segments': [{'marketing_carrier': {'iata_code': 'DL'}, 'marketing_carrier_flight_number': '1234', 'origin': {'iata_code': 'ATL'}, 'destination': {'iata_code': 'MIA'}, 'departing_at': '2026-12-01T08:00:00', 'arriving_at': '2026-12-01T09:50:00'}]}]},
                {'total_amount': '199.00', 'total_currency': 'USD', 'owner': {'name': 'Spirit'}, 'slices': []}]}}).encode())
        out = self.b.router.flights({'origin': 'atl', 'destination': 'MIA', 'departure_date': '2026-12-01', 'nonstop': True}, opener=opener)
        self.assertEqual(sent['headers']['Duffel-version'], 'v2'); self.assertEqual(sent['body']['data']['max_connections'], 0)
        self.assertEqual([o['airline'] for o in out['offers']], ['Spirit', 'Delta'])
        self.assertEqual(out['offers'][1]['slices'][0]['flights'], ['DL1234 ATL→MIA 12-01 08:00–09:50'])
        self.assertIn('Not booked', out['booking'])
        with self.assertRaisesRegex(ValueError, 'airport codes'): self.b.router.flights({'origin': 'Atlanta', 'destination': 'MIA', 'departure_date': '2026-12-01'})

    def test_simplefin_balances_and_missing_keys(self):
        with self.assertRaisesRegex(ValueError, 'never browsed'): self.b.call('balances', {})
        self.key('simplefin', 'https://u%40x:p@bridge.simplefin.org/simplefin'); sent = {}
        def opener(req, timeout):
            sent['url'] = req.full_url; sent['auth'] = req.get_header('Authorization')
            return io.BytesIO(json.dumps({'accounts': [{'org': {'name': 'Wells'}, 'name': 'Checking', 'currency': 'USD', 'balance': '1520.11', 'available-balance': '1500.00', 'balance-date': 1793990000}]}).encode())
        out = self.b.router.balances(opener=opener)
        self.assertEqual(sent['url'], 'https://bridge.simplefin.org/simplefin/accounts?balances-only=1'); self.assertTrue(sent['auth'].startswith('Basic '))
        self.assertEqual(out['accounts'][0]['balance'], '1520.11'); self.assertIn('read-only', out['source'])

    def test_task_prepare_logs_its_route(self):
        t = self.task()
        self.assertEqual(t['route']['route'], 'browse:cloud')
        with self.b.db() as db: self.assertEqual(db.execute("SELECT COUNT(*) n FROM task_events WHERE kind='route'").fetchone()['n'], 1)

class UploadTests(Base):
    def test_upload_needs_owner_review_unless_the_task_names_the_file(self):
        st = self.stage(); self.assertEqual(st['file'], 'note.pdf'); self.assertEqual(list(self.inbox.iterdir()), [])
        t = self.task()
        stop = self.b.call('task_step', {'id': t['id'], 'runtime_context': CONTEXT, 'request_id': 'p6-up-00001', 'steps': [{'kind': 'upload', 'ref': 'e0', 'file': 'note.pdf'}]})
        self.assertEqual(stop['stop_reason'], 'upload')
        self.b.call('review', {'id': stop['action_id']})
        self.assertIn('Upload the file "note.pdf"', self.provider.messages[-1] if 'REVIEW' in self.provider.messages[-1] else '\n'.join(self.provider.messages))
        t2 = self.task('p6-task-0002', upload_files=['note.pdf'])
        out = self.b.call('task_step', {'id': t2['id'], 'runtime_context': CONTEXT, 'request_id': 'p6-up-00002', 'steps': [{'kind': 'upload', 'ref': 'e0', 'file': 'note.pdf'}]})
        self.assertEqual(out['status'], 'completed'); self.assertEqual(self.provider.uploaded, b'%PDF-1.4 doctor note')
        self.assertEqual(list(self.inbox.iterdir()), [])                       # the driver copy is removed

    def test_files_are_checked(self):
        with self.assertRaisesRegex(ValueError, 'documents and images'): self.stage('run.exe')
        with self.assertRaisesRegex(ValueError, 'no file arrived'): self.b.call('upload_stage', {'session_id': self.s['id'], 'inbox_name': '../x', 'name': 'a.pdf'})
        with self.assertRaisesRegex(ValueError, 'Stage it first'): self.task('p6-task-0003', upload_files=['missing.pdf'])
        self.stage()
        with self.assertRaisesRegex(ValueError, 'file field or an Upload'):
            self.b.call('prepare', {'id': self.s['id'], 'request_id': 'p6-up-wrong', 'steps': [{'kind': 'upload', 'ref': 'e1', 'file': 'note.pdf'}]})
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'p6-up-chg01', 'steps': [{'kind': 'upload', 'ref': 'e0', 'file': 'note.pdf'}]})
        f = Path(self.b.uploads.get(self.s['id'], 'note.pdf')['path']); f.write_bytes(b'changed')
        self.b.call('review', {'id': p['id']}); r = self.b.plan(p['id'])
        out = self.b.call('execute', {'id': p['id'], 'hash': r['hash'][:16]}, True)
        self.assertNotEqual(out['status'], 'completed'); self.assertFalse(any(s[0]['kind'] == 'upload' for s in self.provider.acts_seen))

class FrameAndViewportTests(Base):
    def test_controls_in_frames_from_other_origins_need_the_task_allowlist(self):
        self.provider.snap['controls'].append(control('f1e0', 'text', 'Preferred day') | {'form_action': '', 'frame': {'origin': 'https://scheduler.example.com', 'index': 1}})
        t = self.task()
        with self.assertRaisesRegex(ValueError, 'origin_not_allowed'):
            self.b.call('task_step', {'id': t['id'], 'runtime_context': CONTEXT, 'request_id': 'p6-fr-00001', 'steps': [{'kind': 'fill', 'ref': 'f1e0', 'value': '4'}]})
        t2 = self.task('p6-task-0004', partner_origins=['https://scheduler.example.com'])
        self.assertEqual(self.b.call('task_step', {'id': t2['id'], 'runtime_context': CONTEXT, 'request_id': 'p6-fr-00002', 'steps': [{'kind': 'fill', 'ref': 'f1e0', 'value': '4'}]})['status'], 'completed')
        self.assertIn({'frame_origins': ['https://scheduler.example.com']}, [o for op, o in self.provider.options if op == 'act'])

    def test_mobile_trial_after_two_cap_trips_kept_or_reverted(self):
        s = self.b.session(self.s['id'])
        self.provider.snap['controls_truncated'] = True
        self.b.call('snapshot', {'id': self.s['id']}); self.b.call('snapshot', {'id': self.s['id']})
        self.assertEqual(self.b.viewports()['leaveco.example']['mode'], 'mobile')
        self.assertEqual(self.provider.options[-1][1], {}); self.b.call('snapshot', {'id': self.s['id']})
        self.assertEqual(self.provider.options[-1][1], {'viewport': 'mobile'})
        self.b.call('snapshot', {'id': self.s['id']}); self.b.call('snapshot', {'id': self.s['id']})    # still truncated in mobile: 3 bad checks
        self.assertEqual(self.b.viewports()['leaveco.example'], {**self.b.viewports()['leaveco.example'], 'mode': 'desktop', 'source': 'trial_reverted'})
        self.b.call('viewport_set', {'site': 'www.leaveco.example', 'mode': 'mobile'}, True)
        self.assertEqual(self.b.viewports()['leaveco.example'], {'mode': 'mobile', 'source': 'owner'})
        with self.assertRaises(ValueError): self.b.call('viewport_set', {'site': 'leaveco.example', 'mode': 'mobile'})

class TwoSpeedTests(Base):
    def test_worker_session_steps_but_never_completes_and_escalation_moves_back(self):
        t = self.task(); tid = t['id']
        lane = self.b.call('task_lane', {'id': tid}, True)
        self.assertEqual((lane['worker'], lane['cheap_model']), (True, 'haiku'))
        worker = dict(WORKER, sessionKey='agent:main:webtask-' + tid)
        with self.assertRaisesRegex(ValueError, 'another conversation'): self.b.call('task_get', {'id': tid, 'runtime_context': worker})
        with self.assertRaises(ValueError): self.b.call('task_delegate', {'id': tid, 'worker_key': 'agent:main:webtask-' + tid})          # owner socket only
        with self.assertRaises(ValueError): self.b.call('task_delegate', {'id': tid, 'worker_key': 'agent:main:main'}, True)
        self.b.call('task_delegate', {'id': tid, 'worker_key': 'agent:main:webtask-' + tid}, True)
        self.assertEqual(self.b.call('task_get', {'id': tid, 'runtime_context': worker})['id'], tid)
        self.assertEqual(self.b.call('task_step', {'id': tid, 'runtime_context': worker, 'request_id': 'p6-wk-00001', 'steps': [{'kind': 'click', 'ref': 'e1'}]})['status'], 'completed')
        with self.assertRaisesRegex(ValueError, 'main conversation'): self.b.call('task_complete', {'id': tid, 'runtime_context': worker})
        other = dict(WORKER, sessionKey='agent:main:webtask-ffffffffffffffff')
        with self.assertRaises(ValueError): self.b.call('task_get', {'id': tid, 'runtime_context': other})
        self.b.call('task_escalate', {'id': tid, 'why': 'test'}, True)
        self.assertEqual(self.b.call('task_lane', {'id': tid}, True)['worker'], False)
        self.assertEqual(self.b.call('task_speed', {'set': 'off'}, True)['on'], False)
        with self.assertRaises(ValueError): self.b.call('task_speed', {'set': 'on'})

if __name__ == '__main__':
    unittest.main()
