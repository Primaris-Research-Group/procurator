import re
"""Dashboard (Nov 6 2026): login links and sessions, CSRF, pages, JSON API, packet upload, settings, no credentials."""
import http.cookies
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from broker import Broker
import dashboard
from dashboard import Dashboard, Sessions, Server, handler_for
from vitals import Vitals, parse_size, cpu_percent

HERE = os.path.dirname(os.path.abspath(__file__))
BG = os.path.join(HERE, '..', '..', 'task-packets', 'legal-matter')


class Provider:
    def __init__(self):
        self.messages = []

    def telegram(self, m):
        self.messages.append(m)


def seed_state(state):
    state = Path(state)
    (state / 'procurator-admin').mkdir(parents=True)
    (state / 'procurator-build' / 'builders').mkdir(parents=True)
    (state / 'procurator-build' / 'reports').mkdir(parents=True)
    (state / 'openclaw.json').write_text(json.dumps({'agents': {'defaults': {'model': 'anthropic/claude-sonnet-5-5'}, 'entries': {'main': {'model': {'primary': 'openai/gpt-6.1-terra', 'fallbacks': ['anthropic/claude-sonnet-5-5']}}}}}))
    today = dashboard.day_key(time.time())
    (state / 'procurator-admin' / 'usage.json').write_text(json.dumps({'version': 1, 'days': {
        '2026-11-04': {'total': {'calls': 10, 'input': 50000, 'output': 4000, 'cacheRead': 20000, 'cacheWrite': 0, 'cost': 0.42}, 'by': {'chat': {'calls': 10, 'input': 50000, 'output': 4000, 'cacheRead': 20000, 'cacheWrite': 0, 'cost': 0.42}}, 'model': 'anthropic/claude-sonnet-5-5'},
        today: {'total': {'calls': 3, 'input': 9000, 'output': 800, 'cacheRead': 1000, 'cacheWrite': 100, 'cost': 1.5}, 'by': {'chat': {'calls': 1, 'input': 3000, 'output': 300, 'cacheRead': 0, 'cacheWrite': 0, 'cost': 0.5}, 'missions': {'calls': 2, 'input': 6000, 'output': 500, 'cacheRead': 1000, 'cacheWrite': 100, 'cost': 1.0}},
                'by_mission': {'bg': {'calls': 2, 'input': 6000, 'output': 500, 'cacheRead': 1000, 'cacheWrite': 100, 'cost': 1.0}}, 'builds': {'cost': 0.25}, 'model': 'openai/gpt-6.1-terra'}}}))
    (state / 'procurator-build' / 'builders' / 'windows.json').write_text(json.dumps({'label': 'Windows PC', 'seen_at': time.time() * 1000, 'version': '1.3.0', 'host': 'ryzen', 'repos': ['example-org/procurator-sandbox'], 'agents': {'claude': True, 'codex': True},
                                                                                 'vitals': {'load1': 0.4, 'cpus': 12, 'mem_used': 6e9, 'mem_total': 16e9, 'disk_free': 1.2e12, 'disk_total': 2e12, 'uptime': 90000, 'os': 'WSL2 Ubuntu'}, 'secret': 'sk-should-never-show-0123456789abcdef'}))
    (state / 'procurator-build' / 'builders' / 'mac.json').write_text(json.dumps({'label': 'Mac', 'seen_at': time.time() * 1000 - 3600e3, 'version': '1.2.1', 'repos': [], 'agents': {'claude': True, 'codex': False}}))
    (state / 'procurator-build' / 'jobs.json').write_text(json.dumps({'jobs': [{'id': '634954b496dee75d', 'status': 'done', 'plan': {'repo': 'example-org/procurator-sandbox', 'agent': 'codex', 'builder': 'any', 'title': 'Add --utc option'}, 'created_at': time.time() - 600, 'mission': 'bg', 'step': 's2'}]}))
    (state / 'procurator-build' / 'reports' / '634954b496dee75d.json').write_text(json.dumps({'status': 'done', 'builder': 'windows', 'pr_url': 'https://github.com/example-org/procurator-sandbox/pull/1', 'duration_s': 61, 'cost_usd': 0.12, 'summary': 'Added the option.',
                                                                                             'tests': {'status': 'passed', 'command': 'python3 -m unittest', 'output_tail': 'OK'}, 'file_count': 2, 'updated_at': time.time() - 500, 'token': 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'}))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.provider = Provider()
        self.b = Broker(root / 'browser.sqlite3', self.provider)
        seed_state(root / 'state')
        self.vitals = Vitals(str(root / 'vitals.sqlite3'))
        self.vitals.record({'cpu': 12.5, 'load1': 0.3, 'mem_used': 2e9, 'mem_total': 4e9, 'disk_free': 30e9, 'disk_total': 50e9, 'gw_mem': 900e6, 'uptime': 400000})
        self.dash = Dashboard(self.b, root / 'state', str(root / 'dashboard.sqlite3'), vitals=self.vitals)
        self.b.dashboard = self.dash; self.b.missions.checks = self.dash.checks; self.b.missions.quiet = self.dash.paused_all
        self.server = Server(('127.0.0.1', 0), handler_for(self.dash))
        threading.Thread(target=self.server.serve_forever, daemon=True).start(); self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.cookie, self.csrf = None, None

    def req(self, path, body=None, method=None, headers=None, raw=False):
        h = {'Cookie': dashboard.COOKIE + '=' + self.cookie} if self.cookie else {}
        if body is not None:
            h['Content-Type'] = 'application/json'; h['X-Procurator-CSRF'] = self.csrf or ''
        h.update(headers or {})
        r = urllib.request.Request(self.base + dashboard.ROOT + path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(r, timeout=10)
        except urllib.error.HTTPError as e:
            resp = e
        data = resp.read().decode()
        return (resp.status, data, resp.headers) if raw else (resp.status, json.loads(data) if 'json' in (resp.headers.get('Content-Type') or '') else data)

    def login(self):
        link = self.b.call('dashboard_link', {'return_link': True}, True)['link']
        token = link.rsplit('/', 1)[1]
        status, _, headers = self.req('/login/' + token, raw=True)
        self.assertEqual(status, 302)
        c = http.cookies.SimpleCookie(headers['Set-Cookie'])[dashboard.COOKIE]
        self.assertTrue(c['httponly'] and c['secure'] and c['samesite'] == 'Strict' and c['path'] == dashboard.ROOT)
        self.cookie = c.value
        status, page = self.req('/')
        self.assertEqual(status, 200)
        import re
        self.csrf = re.search(r'name="csrf" content="([^"]+)"', page).group(1)
        return token

    def test_login_link_is_single_use_and_sessions_are_capped(self):
        self.assertEqual(self.req('/')[0], 401)
        token = self.login()
        self.assertEqual(self.req('/login/' + token, raw=True)[0], 410, 'a link works once')
        self.assertIn('Your dashboard sign-in link', self.b.call('dashboard_link', {}, True) and self.provider.messages[-1])
        self.assertNotIn(token, self.provider.messages[-1])
        with self.assertRaises(ValueError):
            self.b.call('dashboard_link', {}, False)
        for _ in range(3):
            self.cookie = None; self.login()
        self.assertEqual(len(self.dash.sessions.devices()), 3, 'oldest device is signed out when a 4th signs in')
        self.assertEqual(self.b.call('dashboard_logout', {}, True)['revoked'], 3)
        self.assertEqual(self.req('/')[0], 401)
        self.assertIsNone(self.dash.sessions.session('x.y')); self.assertIsNone(self.dash.sessions.session(None))

    def test_pages_render_and_show_no_secrets(self):
        self.login()
        for path in ('/', '/packets', '/builds', '/builds/634954b496dee75d', '/usage', '/machines', '/activity?days=3', '/settings'):
            status, page = self.req(path)
            self.assertEqual(status, 200, path)
            self.assertIn('PROCURATOR', page)
            self.assertNotIn('sk-should-never', page); self.assertNotIn('ghp_', page); self.assertNotIn(self.cookie, page)
        _, page = self.req('/')
        self.assertIn('GPT-6.1 Terra', page); self.assertIn('Claude Sonnet 5.5', page); self.assertIn('$1.50', page); self.assertIn('1 / 2 online', page)
        _, page = self.req('/builds/634954b496dee75d')
        self.assertIn('pull/1', page); self.assertIn('/buildcancel 634954b496dee75d', page)
        _, page = self.req('/machines')
        self.assertIn('Windows PC', page); self.assertIn('WSL2 Ubuntu', page); self.assertIn('worker 1.3.0', page.replace('<span>1.3.0', 'worker 1.3.0')); self.assertIn('CPU', page)
        _, page = self.req('/usage')
        self.assertIn('Packet work', page); self.assertIn('>bg<', page); self.assertIn('$0.25', page)
        status, csv_text = self.req('/usage.csv')
        self.assertEqual(status, 200); self.assertIn('day,model_replies', csv_text); self.assertIn('2026-11-04,10,', csv_text)
        # 0.25.0: the Health tab — off until /healthadult; then visits, read scopes, grants and connections, and visit notes save.
        _, page = self.req('/health')
        self.assertIn('/healthadult yes', page)
        h = self.b.health
        h.call('health_adult_confirm', {}, True)
        c = h.call('health_connection_add', {'provider': 'clinic_web', 'institution': 'Synthetic Derm Clinic', 'environment': 'fake', 'contacts': ['desk@synthetic-derm.example']}, True)
        with h.db() as db:
            db.execute("INSERT INTO effects(id,request_id,payload_hash,subject,connection,grant_id,grant_revision,action,kind,target,step,state,payload,created) "
                       "VALUES('e0e0e0e0e0e0e0e0','vis-0001','h',(SELECT id FROM subjects LIMIT 1),?,'g',1,'appointment.book','appointment','t','',"
                       "'confirmed',?,0)", (c['id'], json.dumps({'date': '2099-01-12', 'time': '09:15', 'clinician': 'Dr. Synthetic', 'location': 'Suite 4'})))
        status, page = self.req('/health')
        self.assertEqual(status, 200)
        for want in ('Synthetic Derm Clinic', 'Dr. Synthetic', '2099-01-12', 'Insurance card', 'desk@synthetic-derm.example', 'activity'):
            self.assertIn(want, page)
        self.assertIn('/coach on', page)   # 0.26.0: the coach card, off by default
        self.b.health.call('health_coach_settings', {'set': 'on'}, True)
        self.b.health.call('health_coach_answers', {'goals': ['heart_health'], 'eating_disorder': 'no', 'supervised': []})
        self.b.health.call('health_coach_metric', {'kind': 'blood_pressure', 'value': '128/79'})
        _, page = self.req('/health')
        self.assertIn('Wellness coach', page); self.assertIn('blood pressure', page); self.assertIn('128/79', page)
        status, md = self.req('/templates/coding')
        self.assertEqual(status, 200); self.assertIn('**Kind:** coding', md)
        self.assertEqual(self.req('/templates/nope')[0], 404)

    def test_api_requires_session_and_csrf(self):
        self.assertEqual(self.req('/api/overview')[0], 401)
        self.login()
        status, j = self.req('/api/overview')
        self.assertEqual(status, 200); self.assertTrue(j['ok']); self.assertEqual(j['model']['label'], 'GPT-6.1 Terra'); self.assertEqual(j['builders']['online'], 1)
        self.assertNotIn('secret', json.dumps(j))
        good = self.csrf
        self.csrf = 'wrong'
        self.assertEqual(self.req('/api/settings', {'quiet_mode': True})[0], 403)
        self.csrf = good
        self.assertEqual(self.req('/api/settings', {'quiet_mode': True}, headers={'Origin': 'https://evil.example'})[0], 403)
        status, j = self.req('/api/settings', {'quiet_mode': True})
        self.assertEqual(status, 200); self.assertIn('quiet mode on', j['message'])
        self.assertEqual(self.dash.paused_all(), 'quiet mode')
        self.b.missions.save_packet('qm', {'00.md': '# Quiet\n**Revision:** QM-1\n**Goal:** test\n'}, 'owner_upload', activate=True)
        self.assertEqual(self.b.call('mission_due', {}, True)['due'], [], 'quiet mode pauses ticks')
        self.req('/api/settings', {'quiet_mode': False})
        self.assertEqual(len(self.b.call('mission_due', {}, True)['due']), 1)
        self.assertEqual(self.req('/api/nope')[0], 404)
        self.assertEqual(self.req('/api/settings', {'daily_budget_usd': 'abc'})[0], 400)

    def test_daily_budget_alerts_and_pauses(self):
        self.login()
        self.req('/api/settings', {'daily_budget_usd': '2'})
        self.assertIn('Heads-up', self.provider.messages[-1]); self.assertIsNone(self.dash.paused_all())
        self.req('/api/settings', {'daily_budget_usd': '1.5'})
        self.assertEqual(self.dash.paused_all(), 'daily budget spent'); self.assertIn('paused for today', self.provider.messages[-1])
        n = len(self.provider.messages); self.dash.paused_all(); self.assertEqual(len(self.provider.messages), n, 'alerts once per day')
        _, j = self.req('/api/overview'); self.assertEqual(j['paused_all'], 'daily budget spent')
        self.req('/api/settings', {'daily_budget_usd': ''}); self.assertIsNone(self.dash.paused_all())

    @unittest.skipUnless(os.path.isdir(BG), 'the legal-matter example packet is only on the Mac')
    def test_packet_upload_validate_save_control_and_answer(self):
        self.login()
        files = {n: pathlib.Path(BG, n).read_text(encoding='utf-8') for n in os.listdir(BG) if n.endswith('.md')}
        status, j = self.req('/api/packets', {'files': files, 'token': 'nope'})
        self.assertEqual(status, 400); self.assertIn('Validate', j['error'])
        status, v = self.req('/api/packets/validate', {'files': files})
        self.assertEqual(status, 200); self.assertTrue(v['ok']); self.assertEqual(v['packet']['kind'], 'legal'); self.assertEqual(v['packet']['mission'], 'bg')
        self.assertEqual(self.b.call('mission_list', {}, True)['missions'], [], 'validate stores nothing')
        status, j = self.req('/api/packets', {'files': files, 'token': v['token'], 'mission': 'bg', 'activate': False})
        self.assertEqual(status, 200); self.assertEqual((j['mission'], j['status'], j['kind']), ('bg', 'draft', 'legal'))
        self.assertIn('saved from the dashboard', self.provider.messages[-1])
        bad = dict(files); bad['00_START_HERE.md'] += '\nPortal password: hunter2222\n'
        status, v2 = self.req('/api/packets/validate', {'files': bad})
        self.assertFalse(v2['ok']); self.assertIn('line', v2['errors'][0]); self.assertNotIn('hunter', v2['errors'][0]); self.assertNotIn('token', v2)
        status, page = self.req('/packets/bg')
        self.assertEqual(status, 200); self.assertIn('BG-NJ-r1', page); self.assertIn('Activate', page); self.assertIn('legal', page)
        status, j = self.req('/api/packets/bg/state', {'action': 'activate'})
        self.assertEqual(status, 200); self.assertEqual(j['status'], 'active')
        status, j = self.req('/api/packets/bg/settings', {'cadence': '120', 'hours': '08:00-20:00', 'budget': '25', 'model': 'opus'})
        self.assertEqual(status, 200)
        g = self.b.call('mission_get', {'mission': 'bg'}, True)
        self.assertEqual((g['cadence_minutes'], g['hours'], g['budget_usd'], g['model']), (120, [8, 20], 25.0, 'opus'))
        self.assertEqual(self.req('/api/packets/bg/settings', {'budget': '9999'})[0], 400)
        self.b.call('mission_question', {'mission': 'bg', 'action': 'ask', 'question_id': 'q-receipt', 'question': 'When did you receive the letter?'}, False)
        _, page = self.req('/packets/bg'); self.assertIn('When did you receive the letter?', page)
        status, j = self.req('/api/packets/bg/answer', {'question_id': 'q-receipt', 'answer': 'About October 6, envelope kept.'})
        self.assertEqual(status, 200)
        qs = self.b.call('mission_question', {'mission': 'bg', 'action': 'list', 'status': 'answered'}, True)['questions']
        self.assertEqual(qs[0]['answer'], 'About October 6, envelope kept.')
        self.assertEqual(self.req('/api/packets/bg/answer', {'question_id': 'q-receipt', 'answer': ''})[0], 400)
        self.b.call('mission_thread', {'mission': 'bg', 'thread': 'BG-NJ-r1', 'action': 'update', 'step_id': 's1', 'status': 'waiting_for_approval', 'awaiting': 'approval:call:1662b60a62b51816'}, False)
        _, page = self.req('/'); self.assertIn('/callapprove 1662b60a62b51816', page); self.assertIn('BG-NJ-r1/s1', page)
        _, page = self.req('/packets/bg'); self.assertIn('/callapprove 1662b60a62b51816', page); self.assertIn('Dry run', page)
        status, j = self.req('/api/packets/bg/dryrun', {})
        self.assertEqual(status, 200); self.assertIn('DRY RUN', self.provider.messages[-1]); self.assertNotIn('Recovery of disputed', self.provider.messages[-1])
        status, j = self.req('/api/packets/bg/state', {'action': 'pause'}); self.assertEqual(j['status'], 'paused')
        status, j = self.req('/api/packets/bg/state', {'action': 'close'}); self.assertEqual(j['status'], 'closed')
        self.assertEqual(self.req('/api/packets/bg/state', {'action': 'approve'})[0], 400)
        _, page = self.req('/'); self.assertIn('Waiting for you', page)

    def test_devices_revoke_and_logout_page(self):
        self.login()
        first = self.cookie
        self.cookie = None; self.login()
        devices = self.dash.sessions.devices(self.cookie.split('.')[0])
        self.assertEqual(sum(d['this_device'] for d in devices), 1)
        other = next(d for d in devices if not d['this_device'])
        status, j = self.req('/api/devices/revoke', {'id': other['id']})
        self.assertEqual(status, 200); self.assertEqual(j['revoked'], 1)
        self.assertEqual(len(self.dash.sessions.devices()), 1)
        status, _, headers = self.req('/logout', raw=True)
        self.assertEqual(status, 200); self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.req('/')[0], 401)
        self.cookie = first
        self.assertEqual(self.req('/')[0], 401, 'the revoked device stays signed out')

    def test_vitals_helpers(self):
        self.assertEqual(parse_size('1.5GiB'), int(1.5 * 1024 ** 3)); self.assertEqual(parse_size('512MiB'), 512 * 1024 ** 2); self.assertIsNone(parse_size('x'))
        self.assertEqual(cpu_percent((100, 1000), (150, 1200)), 75.0); self.assertIsNone(cpu_percent(None, (1, 2)))
        self.assertEqual(self.vitals.latest()['cpu'], 12.5); self.assertEqual(len(self.vitals.series()), 1)
        self.assertEqual(self.dash.model_info()['fallbacks'], ['Claude Sonnet 5.5'])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AssetTests(unittest.TestCase):
    def test_0243_validation_findings_are_shown_not_hidden_behind_request_failed(self):
        """B05: the api() helper returns an ok:false check result when asked, and otherwise names the errors."""
        import shutil, subprocess
        from dashboard_assets import JS
        if not shutil.which('node'):
            self.skipTest('node not installed')
        fn = re.search(r'async function api\(path,method,body,opt\)\{.*?\n\}', JS, re.S).group(0)
        script = ("const base='',csrf='';let reply;globalThis.fetch=async()=>({ok:true,status:200,json:async()=>reply});" + fn +
                  "(async()=>{reply={ok:false,errors:['Step 2: unknown type teleport'],warnings:[]};const j=await api('/packets/validate','POST',{},{result:true});"
                  "let thrown='';try{await api('/packets/validate','POST',{});}catch(e){thrown=e.message;}"
                  "console.log(JSON.stringify([j.errors,thrown]));})();")
        out = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(out.stdout), [['Step 2: unknown type teleport'], 'Step 2: unknown type teleport'])
        self.assertIn("$('#packet-activate-save').disabled=true;say(out,'');", JS)      # a new pick disables both save buttons
        self.assertIn("up.addEventListener('click',()=>{up.value='';});", JS)          # the same file can be re-checked


if __name__ == '__main__':
    unittest.main()
