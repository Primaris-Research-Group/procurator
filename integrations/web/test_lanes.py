"""Phase 5 (0.19.0): lane choice, job withdrawal and loss semantics, provider routing (without a real PC)."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from broker import Broker, digest
from test_broker import Provider, control

class FakePC:
    """Answers jobs like browser-worker would, from a fake page."""
    def __init__(self, root, name='home', answer=True):
        self.root, self.name, self.answer, self.stop, self.seen = Path(root) / name, name, answer, False, []
        self.snap = {'url': 'https://www.leaveco.example/absence', 'title': 'Leaveco', 'text': 'Absence', 'controls': [control('e0', 'text', 'Hours') | {'form_action': ''}], 'page_errors': []}
        for d in ('jobs', 'claims', 'results'): (self.root / d).mkdir(parents=True, exist_ok=True)
        self.hello()
    def hello(self, sessions=(), profiles=()):
        tmp = self.root / 'hello.tmp'
        tmp.write_text(json.dumps({'name': self.name, 'label': 'Ryzen PC', 'seen_at': int(time.time() * 1000), 'chrome': 'Google Chrome 142',
            'headed': True, 'sessions': list(sessions), 'profiles': [{'name': p, 'ready': True} for p in profiles]}))
        os.replace(tmp, self.root / 'hello.json')
    def run(self):
        while not self.stop:
            for f in sorted((self.root / 'jobs').glob('*.json')):
                jid = f.stem
                try:
                    fd = os.open(self.root / 'claims' / (jid + '.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL); os.close(fd)
                except FileExistsError:
                    continue
                job = json.loads(f.read_text()); self.seen.append(job)
                if not self.answer: continue
                if job['op'] == 'driver':
                    op = job['input']['operation']
                    out = {'ok': True, 'snapshot': self.snap} if op != 'act' else {'ok': True, 'snapshot': self.snap, 'progress': {'changed': True}, 'fills': []}
                    stdout = json.dumps({'event': 'protocol', 'version': 2}) + '\n' + (json.dumps({'event': 'action_dispatched', 'steps': len(job['input'].get('steps', []))}) + '\n' if op == 'act' else '') + json.dumps(out)
                else:
                    stdout = json.dumps({'ok': True})
                # Written the way the real relay writes it (temp file, then rename), so the broker never sees half a file.
                tmp = self.root / 'results' / (jid + '.tmp'); tmp.write_text(json.dumps({'id': jid, 'stdout': stdout}))
                os.replace(tmp, self.root / 'results' / (jid + '.json'))
            time.sleep(0.05)

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = Provider()
        self.b = Broker(Path(self.tmp.name) / 'browser.sqlite3', self.provider)
        self.b.lanes.poll = 0.02
        dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]); dns.start(); self.addCleanup(dns.stop)
        self.pc = None
    def start_pc(self, **kw):
        self.pc = FakePC(Path(self.tmp.name) / 'lanes', **kw)
        t = threading.Thread(target=self.pc.run, daemon=True); t.start()
        self.addCleanup(setattr, self.pc, 'stop', True)
    def tearDown(self): self.tmp.cleanup()

class ChoiceTests(Base):
    def test_defaults_rules_and_offline_fallback(self):
        L = self.b.lanes
        self.assertEqual(L.choose('https://www.leaveco.example')[0], 'cloud')                  # no PC registered: cloud
        self.start_pc()
        self.assertEqual(L.choose('https://www.leaveco.example')[:2], ('home', 'home'))
        self.assertEqual(L.choose('https://www.dominos.com')[0], 'cloud')
        self.assertEqual(L.choose('https://www.georgiapower.com')[0], 'home')
        self.assertEqual(L.choose(None, practice=True)[0], 'cloud')
        L.set_rule('dominos.com', 'home'); self.assertEqual(L.choose('https://www.dominos.com')[0], 'home')
        L.set_rule('leaveco.example', 'cloud'); self.assertEqual(L.choose('https://www.leaveco.example')[0], 'cloud')
        # A saved login runs at home only once its profile was signed in at the PC.
        L.set_rule('leaveco.example', 'default')
        self.assertEqual(L.choose('https://www.leaveco.example', site='leaveco')[0], 'cloud')
        self.pc.hello(profiles=['leaveco']); self.assertEqual(L.choose('https://www.leaveco.example', site='leaveco')[0], 'home')
        L.set_rule('cvs.com', 'home')
        old = int(time.time() * 1000) - 10 * 60 * 1000
        h = json.loads((self.pc.root / 'hello.json').read_text()); h['seen_at'] = old; (self.pc.root / 'hello.json').write_text(json.dumps(h))
        (self.pc.root / 'alive.json').write_text(json.dumps({'at': old}))
        self.assertEqual(L.choose('https://www.leaveco.example')[0], 'cloud')                  # PC offline: default falls back
        with self.assertRaisesRegex(ValueError, 'home browser only'): L.choose('https://www.cvs.com')
        with self.assertRaises(ValueError): L.set_rule('x.com', 'anywhere')

class JobTests(Base):
    def session(self):
        sid = 'a' * 16
        with self.b.db() as db:
            db.execute('INSERT INTO sessions(id,request_id,provider_id,url,origin,practice,status,created,timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?)',
                       (sid, 'lane-test-001', 'home:home:' + sid, 'https://www.leaveco.example/', 'https://www.leaveco.example', 0, 'open', time.time(), 2700))
        return self.b.session(sid)
    def test_a_job_the_pc_never_took_is_withdrawn_and_counts_as_not_done(self):
        os.makedirs(Path(self.tmp.name) / 'lanes' / 'home', exist_ok=True)
        s = self.session()
        with patch.object(self.b.lanes, 'submit', wraps=lambda m, op, payload, timeout=70, grace=20: type(self.b.lanes).submit(self.b.lanes, m, op, payload, timeout=0.2, grace=0.2)):
            out = self.b.lanes.run(s, 'act', expected='x', steps=[{'kind': 'click', 'control': {}}])
        self.assertEqual(out['kind'], 'not_dispatched')
        claims = list((Path(self.tmp.name) / 'lanes' / 'home' / 'claims').glob('*.json'))
        self.assertEqual(json.loads(claims[0].read_text())['by'], 'broker-withdrawn')
        self.assertEqual(list((Path(self.tmp.name) / 'lanes' / 'home' / 'jobs').glob('*.json')), [])
    def test_a_claimed_job_without_an_answer_is_unconfirmed(self):
        self.start_pc(answer=False)
        s = self.session()
        with patch.object(self.b.lanes, 'submit', wraps=lambda m, op, payload, timeout=70, grace=20: type(self.b.lanes).submit(self.b.lanes, m, op, payload, timeout=0.3, grace=0.2)):
            out = self.b.lanes.run(s, 'act', expected='x', steps=[{'kind': 'click', 'control': {}}])
        self.assertEqual(out['kind'], 'unconfirmed')
    def test_driver_ops_go_to_the_pc_and_uploads_travel_with_the_job(self):
        self.start_pc()
        s = self.session()
        snap = self.b.call('snapshot', {'id': s['id']})['snapshot']
        self.assertEqual(snap['title'], 'Leaveco'); self.assertEqual(self.provider.acts, 0)
        f = Path(self.tmp.name) / 'upload.bin'; f.write_bytes(b'%PDF')
        self.b.lanes.run(s, 'act', expected=digest(self.pc.snap), steps=[{'kind': 'upload', 'file_path': str(f), 'file': {'name': 'n.pdf'}}])
        job = self.pc.seen[-1]
        self.assertEqual(job['input']['steps'][0]['file_b64'], 'JVBERg=='); self.assertNotIn('connectUrl', job['input'])
    def test_routing_inspect_watch_and_takeover_on_a_home_browser(self):
        self.start_pc(); s = self.session()
        self.pc.hello(sessions=[s['id']])
        self.assertEqual(self.b.provider.inspect(s['provider_id'])['status'], 'RUNNING')
        self.pc.hello(sessions=[])
        self.assertEqual(self.b.provider.inspect(s['provider_id'])['status'], 'COMPLETED')
        with self.assertRaisesRegex(ValueError, 'PC at home'): self.b.watch_link(s['id'])
        with self.assertRaisesRegex(ValueError, 'PC at home'): self.b.call('takeover', {'id': s['id']}, True)
        self.assertEqual(self.b.public_session(s)['lane'], 'home'); self.assertIsNone(self.b.public_session(s)['session_url'])
        self.pc.hello(sessions=[s['id']])
        self.b.call('close', {'id': s['id']})
        self.assertEqual(self.pc.seen[-1]['op'], 'close')
    def test_open_on_the_home_lane(self):
        self.start_pc()
        out = self.b.call('open', {'request_id': 'lane-open-0001', 'url': 'https://www.leaveco.example/'})
        self.assertEqual(out['lane'], 'home'); self.assertIn('Ryzen PC', out['lane_reason'])
        self.assertEqual(self.pc.seen[0]['op'], 'start'); self.assertEqual(self.pc.seen[0]['profile'], 'procurator')   # 0.20.3: the shared profile
        self.assertEqual(self.provider.created, 0)
        st = self.b.call('lane_status', {})
        self.assertEqual(st['machines'][0]['label'], 'Ryzen PC'); self.assertTrue(st['machines'][0]['online'])
    def test_a_new_home_browser_is_not_closed_before_the_pc_reports_it(self):
        # Nov 6 deploy battery: a status check right after opening (before the PC's next heartbeat) closed the session.
        self.start_pc()
        out = self.b.call('open', {'request_id': 'lane-open-0002', 'url': 'https://www.leaveco.example/'})
        self.pc.hello(sessions=[])                                  # a heartbeat written before the start was recorded
        h = json.loads((self.pc.root / 'hello.json').read_text()); h['seen_at'] = int(time.time() * 1000) - 5000
        (self.pc.root / 'hello.json').write_text(json.dumps(h))
        self.b.call('status', {})
        self.assertEqual(self.b.session(out['id'])['status'], 'open')
        self.pc.hello(sessions=[])                                  # a later heartbeat without it: really gone
        self.b.call('status', {})
        self.assertEqual(self.b.session(out['id'])['status'], 'closed')
    def fast(self):
        return patch.object(self.b.lanes, 'submit', wraps=lambda m, op, payload, timeout=70, grace=20: type(self.b.lanes).submit(self.b.lanes, m, op, payload, timeout=0.3, grace=0.2))
    def test_a_start_the_pc_never_took_fails_cleanly_and_blocks_nothing(self):
        # Nov 6 QA: a withdrawn start left a creation_uncertain row that blocked every browser.
        self.pc = FakePC(Path(self.tmp.name) / 'lanes')                 # online heartbeat, but no worker taking jobs
        with self.fast(), self.assertRaisesRegex(ValueError, 'did not pick up the request'):
            self.b.call('open', {'request_id': 'lane-open-0003', 'url': 'https://www.leaveco.example/'})
        with self.b.db() as db:
            self.assertEqual(db.execute("SELECT status FROM sessions WHERE request_id='lane-open-0003'").fetchone()['status'], 'failed')
        self.b.lanes.set_rule('leaveco.example', 'cloud')
        self.assertEqual(self.b.call('open', {'request_id': 'lane-open-0004', 'url': 'https://www.leaveco.example/'})['lane'], 'cloud')
    def test_a_start_the_pc_took_but_never_answered_is_reconciled_from_its_heartbeat(self):
        self.start_pc(answer=False)
        with self.fast(), self.assertRaisesRegex(ValueError, 'unconfirmed'):
            self.b.call('open', {'request_id': 'lane-open-0005', 'url': 'https://www.leaveco.example/'})
        with self.b.db() as db:
            row = dict(db.execute("SELECT * FROM sessions WHERE request_id='lane-open-0005'").fetchone())
        self.assertEqual(row['status'], 'creation_uncertain'); self.assertTrue(row['provider_id'].startswith('home:home:'))
        time.sleep(0.01); self.pc.hello(sessions=[row['id']])            # the PC reports the browser is running
        self.b.call('status', {}); self.assertEqual(self.b.session(row['id'])['status'], 'open')
        time.sleep(0.01); self.pc.hello(sessions=[])                     # later heartbeat without it: gone
        self.b.call('status', {}); self.assertEqual(self.b.session(row['id'])['status'], 'closed')
    def test_a_pc_offline_for_half_an_hour_frees_the_browser_slot(self):
        self.start_pc(); s = self.session()
        h = json.loads((self.pc.root / 'hello.json').read_text()); h['seen_at'] = int(time.time() * 1000) - 31 * 60 * 1000
        self.pc.stop = True; (self.pc.root / 'hello.json').write_text(json.dumps(h))
        self.b.call('status', {}); self.assertEqual(self.b.session(s['id'])['status'], 'closed')
    def test_a_rule_for_a_domain_covers_its_subdomains_and_the_route_predicts_the_lane(self):
        # Nov 6 compat run: "/weblane wikipedia.org home" did not apply to en.wikipedia.org, and route events always said cloud.
        self.start_pc(); L = self.b.lanes
        L.set_rule('wikipedia.org', 'home')
        self.assertEqual(L.choose('https://en.wikipedia.org')[0], 'home')
        self.assertEqual(L.rule_for('en.wikipedia.org')['rule'], 'wikipedia.org')
        L.set_rule('en.wikipedia.org', 'cloud')                       # the most specific rule wins
        self.assertEqual(L.choose('https://en.wikipedia.org')[0], 'cloud')
        self.assertEqual(L.choose('https://de.wikipedia.org')[0], 'home')
        r = self.b.router.route('www.irs.gov', 'find form W-4')
        self.assertEqual(r['route'], 'browse:home'); self.assertIn('predicted', r['reason']); self.assertTrue(r['available']['home_lane'])
        self.assertEqual(self.b.router.route('wikipedia.org', 'population')['route'], 'browse:home')

if __name__ == '__main__':
    unittest.main()


class LeavecoHomeRuleTests(Base):
    """Nov 6 (Leaveco, live): with /weblane leaveco.example home and /homeready leaveco, the bot still went back to a cloud browser it had
    signed in to earlier and sent a cloud sign-in link. The lane rule now comes first."""
    def setUp(self):
        super().setUp()
        self.b.lanes.set_rule('leaveco.example', 'cloud')
        self.cloud = self.b.call('open', {'request_id': 'leaveco-cloud-0001', 'url': 'https://www.leaveco.example/'})['id']
        with self.b.db() as db:
            db.execute("INSERT INTO site_logins(name,start_url,origin,context_id,status,updated,session_id,sequence) VALUES('leaveco','https://www.leaveco.example/','https://www.leaveco.example','ctx-0123456789abcdef0123','owner_connected',?,?,1)", (time.time(), self.cloud))
            db.execute("INSERT INTO site_sessions VALUES(?, 'leaveco')", (self.cloud,))
        self.b.lanes.set_rule('leaveco.example', 'home')
        self.start_pc(); self.pc.hello(profiles=('leaveco',))

    def test_a_signed_in_cloud_browser_is_not_reused_when_the_site_runs_on_the_pc(self):
        out = self.b.call('open', {'request_id': 'leaveco-home-0001', 'url': 'https://www.leaveco.example/', 'site_login': 'leaveco'})
        self.assertEqual(out['lane'], 'home'); self.assertNotIn('reused_signed_in_browser', out)
        self.assertEqual(self.b.session(self.cloud)['status'], 'closed')          # the old cloud browser gave way
        start = next(x for x in self.pc.seen if x['op'] == 'start'); self.assertEqual(start['profile'], 'leaveco')
        self.assertEqual(self.provider.created, 1)                                 # only the earlier cloud one

    def test_the_cloud_login_state_does_not_block_the_pc(self):
        with self.b.db() as db: db.execute("UPDATE site_logins SET status='awaiting_owner_sign_in'")
        self.assertEqual(self.b.call('open', {'request_id': 'leaveco-home-0002', 'url': 'https://www.leaveco.example/', 'site_login': 'leaveco'})['lane'], 'home')

    def test_a_sign_in_request_for_a_pc_site_goes_to_the_pc_not_to_a_cloud_link(self):
        out = self.b.call('site_signin_request', {'site': 'leaveco'})
        self.assertEqual(out['lane'], 'home'); self.assertIn('/homeready leaveco', out['note']); self.assertNotIn('http', json.dumps(out))
        self.assertIn('/homeready leaveco', self.provider.messages[-1]); self.assertNotIn('/assistant/signin/', self.provider.messages[-1])
        self.assertEqual(next(x for x in self.pc.seen if x['op'] == 'signin')['profile'], 'procurator')     # the window opened on the PC (shared profile)
        self.b.lanes.set_rule('leaveco.example', 'cloud')                                 # back on the cloud: the cloud link again
        self.assertNotEqual(self.b.call('site_signin_request', {'site': 'leaveco'}).get('lane'), 'home')


class AddSiteToPcTests(Base):
    """Nov 6: /homesignin NAME URL adds any website to the PC without a cloud sign-in first."""
    def test_a_new_website_becomes_a_pc_login_with_a_home_rule(self):
        self.start_pc()
        out = self.b.call('home_signin', {'site': 'georgiapower', 'url': 'https://www.georgiapower.com/myaccount'}, True)
        self.assertTrue(out['created']); self.assertEqual(out['lane_set'], 'georgiapower.com'); self.assertEqual(out['profile'], 'procurator')
        self.assertEqual(next(x for x in self.pc.seen if x['op'] == 'signin')['url'], 'https://www.georgiapower.com/myaccount')
        self.assertEqual(self.b.lanes.rules()['georgiapower.com'], 'home')
        row = self.b.sites.row('georgiapower'); self.assertEqual((row['status'], row['context_id']), ('home_only', None))
        listed = {x['site']: x for x in self.b.call('site_list', {})['sites']}
        self.assertTrue(listed['georgiapower']['signed_in_browsing']); self.assertEqual(listed['georgiapower']['where'], 'home PC only')
        self.b.call('home_ready', {'site': 'georgiapower'}, True)                       # the owner signed in and sent /homeready
        opened = self.b.call('open', {'request_id': 'gp-home-0001', 'url': 'https://www.georgiapower.com/myaccount', 'site_login': 'georgiapower'})
        self.assertEqual(opened['lane'], 'home'); self.assertEqual(self.provider.created, 0)
        self.assertEqual(self.b.call('site_signin_request', {'site': 'georgiapower'})['lane'], 'home')   # re-sign-in goes to the PC

    def test_a_rule_he_chose_is_kept_and_banks_or_read_only_sites_are_refused(self):
        self.start_pc(); self.b.lanes.set_rule('duke-energy.com', 'either')
        self.assertIsNone(self.b.call('home_signin', {'site': 'duke', 'url': 'https://www.duke-energy.com/'}, True)['lane_set'])
        self.assertEqual(self.b.lanes.rules()['duke-energy.com'], 'either')
        for name, site in (('chase', 'https://www.chase.com/'), ('amazonx', 'https://www.amazon.com/')):
            with self.assertRaisesRegex(ValueError, 'No signed-in browsing'):
                self.b.call('home_signin', {'site': name, 'url': site}, True)
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.b.call('home_signin', {'site': 'duke', 'url': 'https://www.georgiapower.com/'}, True)
        with self.assertRaisesRegex(ValueError, 'Owner only'):
            self.b.call('home_signin', {'site': 'x1', 'url': 'https://www.example.org/'})          # the bot


class AskForThePcTests(Base):
    """Nov 6 (hotels.com dialogue): the owner can ask in chat for their PC; the assistant opens the sign-in window there and then
    uses the PC browser. Rules the owner set with /weblane still win."""
    def test_he_asks_to_log_in_on_his_pc_and_the_assistant_uses_it(self):
        self.start_pc()
        out = self.b.call('home_signin_request', {'site': 'hotels.com'})                  # the assistant's tool, not privileged
        self.assertTrue(out['sent']); self.assertEqual(out['site'], 'hotels'); self.assertIn('browser=home', out['note'])
        self.assertEqual(next(x for x in self.pc.seen if x['op'] == 'signin')['url'], 'https://www.hotels.com/')
        self.assertIn('/homeready hotels', self.provider.messages[-1])
        self.assertNotIn('hotels.com', self.b.lanes.rules())                              # the bot does not set their rules
        self.assertFalse(self.b.call('home_signin_request', {'site': 'hotels'})['sent'])  # no second window within 2 minutes
        with self.assertRaisesRegex(ValueError, 'Owner only'):
            self.b.call('home_ready', {'site': 'hotels'})                                  # only the owner confirms the sign-in
        with self.assertRaisesRegex(ValueError, 'not signed in on their PC yet'):
            self.b.call('open', {'request_id': 'hotels-pc-0001', 'url': 'https://www.hotels.com/', 'site_login': 'hotels'})
        ready = self.b.call('home_ready', {'site': 'hotels'}, True)                      # the owner signed in and sent /homeready
        self.assertTrue(ready['woke_assistant'])                                          # 0.20.3: the assistant is told to carry on
        self.assertEqual(self.b.call('site_signin_events', {})['events'][0] | {'at': 0}, {'site': 'hotels', 'browser': '', 'at': 0, 'kind': 'home_ready'})
        opened = self.b.call('open', {'request_id': 'hotels-pc-0002', 'url': 'https://www.hotels.com/', 'site_login': 'hotels'})
        self.assertEqual(opened['lane'], 'home'); self.assertEqual(self.provider.created, 0)
        self.assertEqual([x['profile'] for x in self.pc.seen if x['op'] == 'start'], ['procurator'])

    def test_any_public_site_can_open_on_the_pc_when_he_asks_but_his_rules_win(self):
        self.start_pc()
        out = self.b.call('open', {'request_id': 'pc-lookup-0001', 'url': 'https://www.dominos.com/', 'browser': 'home'})
        self.assertEqual(out['lane'], 'home'); self.assertIn('the owner asked for their PC', out['lane_reason'])
        self.b.call('close', {'id': out['id']})
        self.b.lanes.set_rule('dominos.com', 'cloud')
        with self.assertRaisesRegex(ValueError, r'/weblane dominos.com home'):
            self.b.call('open', {'request_id': 'pc-lookup-0002', 'url': 'https://www.dominos.com/', 'browser': 'home'})
        self.b.lanes.set_rule('leaveco.example', 'home')
        with self.assertRaisesRegex(ValueError, 'PC only'):
            self.b.call('open', {'request_id': 'pc-lookup-0003', 'url': 'https://www.leaveco.example/', 'browser': 'cloud'})
        with self.assertRaisesRegex(ValueError, 'cloud'):
            self.b.call('open', {'request_id': 'pc-lookup-0004', 'url': 'practice', 'browser': 'home'})
        with self.assertRaisesRegex(ValueError, 'browser must be'):
            self.b.call('open', {'request_id': 'pc-lookup-0005', 'url': 'https://www.dominos.com/', 'browser': 'laptop'})

    def test_offline_pc_and_never_list(self):
        with self.assertRaisesRegex(ValueError, 'offline'):
            self.b.call('open', {'request_id': 'pc-offline-0001', 'url': 'https://www.dominos.com/', 'browser': 'home'})
        self.start_pc()
        with self.assertRaisesRegex(ValueError, 'No signed-in browsing'):
            self.b.call('home_signin_request', {'site': 'chase.com'})

    def test_homesignin_accepts_a_website_or_an_address(self):
        n = self.b.home_site_name
        self.assertEqual(n('hotels.com'), ('hotels', None)); self.assertEqual(n('https://www.leaveco.example/'), ('leaveco', None))
        self.assertEqual(n('georgiapower.com'), ('georgiapower', 'https://www.georgiapower.com/'))
        self.assertEqual(n('https://my.duke-energy.com/login'), ('duke-energy', 'https://my.duke-energy.com/login'))
        self.assertEqual(n('cvs'), ('cvs', None))
        with self.assertRaises(ValueError): n('georgia power')


class SharedProfileTests(Base):
    """0.20.3 (Nov 6): "the bot always opens a browser with no history". Every home task now uses one shared Chrome profile,
    so history, cache and sign-ins carry over between tasks and websites; /homeprofile separate restores per-site profiles."""
    def starts(self):
        return [x['profile'] for x in self.pc.seen if x['op'] == 'start']

    def open_close(self, rid, url, **kw):
        out = self.b.call('open', {'request_id': rid, 'url': url, **kw}); self.b.call('close', {'id': out['id']}); return out

    def test_every_site_and_task_uses_the_same_profile(self):
        self.start_pc()
        self.open_close('shared-0001', 'https://www.weather.gov/', browser='home')
        self.open_close('shared-0002', 'https://www.hotels.com/', browser='home')
        self.b.call('home_signin', {'site': 'hotels.com'}, True); self.b.call('home_ready', {'site': 'hotels'}, True)
        self.open_close('shared-0003', 'https://www.hotels.com/', site_login='hotels')
        self.assertEqual(self.starts(), ['procurator'] * 3)
        st = self.b.call('lane_status', {})['home_profile']
        self.assertEqual((st['mode'], st['signed_in_sites']), ('shared', ['hotels']))
        self.assertNotIn('woke_assistant', self.b.call('home_ready', {'site': 'hotels'}, True))   # their own /homesignin: nothing to wake

    def test_a_site_signed_in_before_keeps_its_own_profile_until_he_signs_in_again(self):
        self.start_pc(); self.pc.hello(profiles=('leaveco',)); self.b.lanes.set_rule('leaveco.example', 'home')
        with self.b.db() as db:
            db.execute("INSERT INTO site_logins(name,start_url,origin,context_id,status,updated,session_id,sequence) VALUES('leaveco','https://www.leaveco.example/','https://www.leaveco.example',NULL,'home_only',?,NULL,0)", (time.time(),))
        self.open_close('legacy-0001', 'https://www.leaveco.example/', site_login='leaveco')
        self.b.call('home_signin', {'site': 'leaveco'}, True); self.b.call('home_ready', {'site': 'leaveco'}, True)
        self.open_close('legacy-0002', 'https://www.leaveco.example/', site_login='leaveco')
        self.assertEqual(self.starts(), ['leaveco', 'procurator'])

    def test_no_task_browser_closes_the_sign_in_window_he_is_using(self):
        self.start_pc()
        self.b.call('home_signin_request', {'site': 'hotels.com'})
        with self.assertRaisesRegex(ValueError, 'signing in to hotels'):
            self.b.call('open', {'request_id': 'guard-0001', 'url': 'https://www.weather.gov/', 'browser': 'home'})
        self.assertEqual(self.starts(), [])
        self.b.call('home_ready', {'site': 'hotels'}, True)
        self.open_close('guard-0002', 'https://www.weather.gov/', browser='home')
        self.b.lanes.signing['old'] = (time.time() - 16 * 60, 'procurator')       # a window from 16 minutes ago no longer blocks
        self.open_close('guard-0003', 'https://www.weather.gov/', browser='home')
        self.assertEqual(len(self.starts()), 2)

    def test_separate_mode_and_owner_only(self):
        self.start_pc()
        with self.assertRaisesRegex(ValueError, 'Owner only'):
            self.b.call('home_profile', {'mode': 'separate'})
        with self.assertRaisesRegex(ValueError, 'shared or'):
            self.b.call('home_profile', {'mode': 'incognito'}, True)
        self.assertEqual(self.b.call('home_profile', {'mode': 'separate'}, True)['mode'], 'separate')
        self.open_close('sep-0001', 'https://www.weather.gov/', browser='home')
        self.assertEqual(self.b.call('home_signin', {'site': 'hotels.com'}, True)['profile'], 'hotels')
        self.assertEqual(self.starts(), ['weather.gov'])
        self.assertEqual(self.b.call('home_profile', {}, True)['mode'], 'separate')


class SignedInAtThePcTests(Base):
    """0.20.3 (Nov 6, hotels.com live): hotels had an old cloud saved login and no /weblane rule. The owner signed in at the PC and
    sent /homeready, but the assistant then opened hotels.com in the cloud, sent a cloud sign-in link, and the waiting cloud
    sign-in blocked the PC browser ("the private sign-in is unfinished"). A site signed in at the PC now goes to the PC."""
    def setUp(self):
        super().setUp(); self.start_pc()
        with self.b.db() as db:
            db.execute("INSERT INTO site_logins(name,start_url,origin,context_id,status,updated,session_id,sequence) VALUES('hotels','https://www.hotels.com/','https://www.hotels.com','ctx-0123456789abcdef0123','owner_connected',?,NULL,1)", (time.time(),))
        self.b.call('home_signin_request', {'site': 'hotels.com'})
        self.b.call('home_ready', {'site': 'hotels'}, True)

    def test_the_site_opens_on_the_pc_signed_in_even_without_naming_the_login(self):
        out = self.b.call('open', {'request_id': 'hotels-auto-0001', 'url': 'https://www.hotels.com/'})
        self.assertEqual(out['lane'], 'home'); self.assertEqual(self.provider.created, 0)
        self.assertEqual(self.b.session(out['id'])['site_login'], 'hotels')
        self.b.call('close', {'id': out['id']})
        cloud = self.b.call('open', {'request_id': 'hotels-cloud-0001', 'url': 'https://www.hotels.com/', 'browser': 'cloud'})
        self.assertNotEqual(cloud.get('lane'), 'home')                                  # the owner can still ask for the cloud

    def test_a_sign_in_request_goes_to_the_pc_not_a_cloud_link(self):
        self.b.lanes.signing.clear(); self.b._home_signin_asked = {}
        out = self.b.call('site_signin_request', {'site': 'hotels'})
        self.assertEqual(out['lane'], 'home'); self.assertNotIn('/assistant/signin/', ' '.join(self.provider.messages))
        self.assertEqual(self.b.sites.row('hotels')['status'], 'owner_connected')          # the cloud login was not touched

    def test_a_waiting_cloud_sign_in_no_longer_blocks_the_pc(self):
        self.b.lanes.signing.clear()
        cloud = self.b.call('open', {'request_id': 'hotels-cloud-0002', 'url': 'https://www.hotels.com/', 'browser': 'cloud'})['id']
        with self.b.db() as db:
            db.execute("INSERT INTO site_sessions VALUES(?, 'hotels')", (cloud,))
            db.execute("UPDATE site_logins SET status='awaiting_owner_sign_in',session_id=?,signin_hash='x',signin_expires=? WHERE name='hotels'", (cloud, time.time() + 2000))
        out = self.b.call('open', {'request_id': 'hotels-home-0003', 'url': 'https://www.hotels.com/', 'site_login': 'hotels'})
        self.assertEqual(out['lane'], 'home'); self.assertEqual(self.b.session(cloud)['status'], 'closed')
        row = self.b.sites.row('hotels'); self.assertEqual((row['status'], row['signin_hash']), ('reauthentication_required', None))
        self.assertTrue(self.b.call('snapshot', {'id': out['id']})['snapshot'])            # no "owner is signing in privately"

    def test_homeready_drops_a_waiting_cloud_sign_in(self):
        cloud = self.b.call('open', {'request_id': 'hotels-cloud-0004', 'url': 'https://www.hotels.com/', 'browser': 'cloud'})['id']
        with self.b.db() as db:
            db.execute("INSERT INTO site_sessions VALUES(?, 'hotels')", (cloud,))
            db.execute("UPDATE site_logins SET status='awaiting_owner_sign_in',session_id=?,signin_hash='x',signin_expires=? WHERE name='hotels'", (cloud, time.time() + 2000))
        self.b.call('home_ready', {'site': 'hotels'}, True)
        self.assertEqual(self.b.session(cloud)['status'], 'closed'); self.assertEqual(self.b.sites.row('hotels')['status'], 'reauthentication_required')

    def test_pc_offline_falls_back_to_the_cloud_login(self):
        old = int(time.time() * 1000) - 10 * 60 * 1000
        h = json.loads((self.pc.root / 'hello.json').read_text()); h['seen_at'] = old; (self.pc.root / 'hello.json').write_text(json.dumps(h))
        (self.pc.root / 'alive.json').write_text(json.dumps({'at': old}))
        self.assertIsNone(self.b.sites.home_login_for('https://www.hotels.com'))
        self.assertFalse(self.b.lanes.ready_online('hotels'))
        self.assertEqual(self.b.lanes.choose('https://www.hotels.com', site='hotels')[0], 'cloud')   # the cloud login is used
