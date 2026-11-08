"""0.30.0 first-run setup (Tier 1): key checks, tap-to-pair with a one-time start code, owner.json + .env + allow-list, the browser
wizard and its one-time link, the root applier hand-off, and the dashboard routes. Telegram and Anthropic are fakes; no network."""
import io
import contextlib
import subprocess
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

import setup_core as core
import setup_wizard as wiz

KEY = 'sk-ant-api03-' + 'k' * 40
TOKEN = '123456789:' + 'A' * 35
OWNER_ID = 100000001


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status = json.dumps(body).encode(), status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Anthropic /v1/models and the Telegram Bot API, enough for setup."""

    def __init__(self):
        self.good_key, self.good_token = KEY, TOKEN
        self.updates, self.sent, self.acked, self.calls = [], [], [], []
        self.busy = False
        self.lock = threading.Lock()

    def __call__(self, req, timeout):
        url = req.full_url
        self.calls.append(url.split('/bot')[0] + ('/bot<token>/' + url.rsplit('/', 1)[1] if '/bot' in url else ''))
        if url.startswith('https://api.anthropic.com/'):
            if req.get_header('X-api-key') != self.good_key:
                raise urllib.error.HTTPError(url, 401, 'unauthorized', {}, io.BytesIO(b'{}'))
            return Resp({'data': []})
        token, method = url.split('/bot', 1)[1].split('/', 1)
        if token != self.good_token:
            raise urllib.error.HTTPError(url, 401, 'unauthorized', {}, io.BytesIO(b'{"ok":false,"error_code":401}'))
        params = dict(urllib.parse.parse_qsl((req.data or b'').decode()))
        if method == 'getMe':
            return Resp({'ok': True, 'result': {'id': 42, 'is_bot': True, 'username': 'alex_assistant_bot'}})
        if method == 'deleteWebhook':
            return Resp({'ok': True, 'result': True})
        if method == 'getUpdates':
            if self.busy:
                raise urllib.error.HTTPError(url, 409, 'conflict', {}, io.BytesIO(b'{"ok":false,"error_code":409}'))
            off = int(params.get('offset', 0) or 0)
            if params.get('timeout') == '0':
                self.acked.append(off)
            with self.lock:
                out = [u for u in self.updates if u['update_id'] >= off]
            if not out and params.get('timeout') != '0':
                time.sleep(0.02)
            return Resp({'ok': True, 'result': out})
        if method == 'sendMessage':
            self.sent.append(params)
            return Resp({'ok': True, 'result': {}})
        return Resp({'ok': False, 'error_code': 404})

    def press_start(self, text, uid=OWNER_ID, chat='private', is_bot=False, first='Alex'):
        with self.lock:
            n = 1000 + len(self.updates)
            self.updates.append({'update_id': n, 'message': {'text': text, 'chat': {'type': chat, 'id': uid},
                                                             'from': {'id': uid, 'is_bot': is_bot, 'first_name': first, 'last_name': 'Example', 'username': 'alexex'}}})


class FakeBox(core.Box):
    def __init__(self, root):
        self.commands = []
        super().__init__(root, run=lambda cmd, **kw: self.commands.append(cmd))
        self.env.write_text('OPENCLAW_TZ=America/Chicago\nOPENCLAW_GATEWAY_TOKEN=x\n')

    def broker_units(self):
        return ['procurator-web', 'procurator-mail']


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.net = FakeNet()
        os.environ.pop('PROCURATOR_OWNER_FILE', None)
        self.box = FakeBox(self.root)

    def tearDown(self):
        self.td.cleanup()

    def test_keys_are_checked_with_friendly_errors(self):
        self.assertEqual(core.verify_anthropic(' ' + KEY + ' ', opener=self.net), KEY)
        with self.assertRaisesRegex(core.SetupError, 'starts with "sk-ant-"'):
            core.verify_anthropic('hello', opener=self.net)
        with self.assertRaisesRegex(core.SetupError, 'did not accept'):
            core.verify_anthropic('sk-ant-api03-' + 'z' * 40, opener=self.net)
        self.assertEqual(core.verify_bot(TOKEN, opener=self.net)['username'], 'alex_assistant_bot')
        # pasted the way phones and Telegram deliver it: wrapped across lines, a smart dash, the sentence around it
        wrapped = 'Use this token to access the HTTP API:\n' + TOKEN[:20] + '\n' + TOKEN[20:] + '\nKeep your token secure'
        self.assertEqual(core.verify_bot(wrapped, opener=self.net)['token'], TOKEN)
        self.assertEqual(core.verify_bot(' ' + TOKEN + '\u200b ', opener=self.net)['token'], TOKEN)
        net2 = FakeNet(); net2.good_token = '123456789:AAEf-' + 'x' * 30
        self.assertEqual(core.verify_bot('123456789:AAEf\u2011' + 'x' * 30, opener=net2)['token'], net2.good_token)
        self.assertEqual(core.verify_anthropic(KEY[:15] + '\n' + KEY[15:], opener=self.net), KEY)
        with self.assertRaisesRegex(core.SetupError, 'does not look like a bot token'):
            core.verify_bot('nope', opener=self.net)
        with self.assertRaisesRegex(core.SetupError, 'did not accept that token'):
            core.verify_bot('123456789:' + 'B' * 35, opener=self.net)

    def test_pairing_needs_the_exact_start_code_from_a_private_chat(self):
        code = core.new_code()
        self.net.press_start('/start wrongcode123')                 # a stranger without the link
        self.net.press_start('/start ' + code, chat='group')         # right code, but in a group
        self.net.press_start('/start ' + code, is_bot=True)          # a bot
        self.net.press_start('hello')
        self.net.press_start('/start ' + code, uid=555, first='Alex')
        owner = core.wait_for_start(TOKEN, code, timeout_s=5, opener=self.net)
        self.assertEqual(owner['id'], '555')
        self.assertEqual(owner['first_name'], 'Alex')
        self.assertTrue(self.net.acked and self.net.acked[-1] == 1005, 'the start update is acknowledged so the gateway never replays it')

    def test_pairing_times_out_and_reports_a_busy_gateway(self):
        with self.assertRaisesRegex(core.SetupError, 'Nobody pressed Start'):
            core.wait_for_start(TOKEN, core.new_code(), timeout_s=0.2, opener=self.net, poll_s=1)
        self.net.busy = True
        with self.assertRaises(core.GatewayBusy):
            core.wait_for_start(TOKEN, core.new_code(), timeout_s=5, opener=self.net)

    def test_finish_writes_env_owner_allowlist_and_restarts(self):
        bot = core.verify_bot(TOKEN, opener=self.net)
        out = core.finish(self.box, KEY, bot, {'id': '555', 'first_name': 'Alex', 'last_name': 'Example', 'username': 'alexex'},
                          timezone='America/Chicago', opener=self.net)
        env = core.read_env(self.box.env)
        self.assertEqual((env['ANTHROPIC_API_KEY'], env['TELEGRAM_BOT_TOKEN'], env['PROCURATOR_OWNER_ID']), (KEY, TOKEN, '555'))
        self.assertEqual(env['OPENCLAW_GATEWAY_TOKEN'], 'x', 'other lines are kept')
        self.assertEqual(oct(self.box.env.stat().st_mode & 0o777), '0o600')
        owner = json.loads(self.box.owner_file.read_text())
        self.assertEqual(owner['schema'], 2)
        self.assertEqual(owner['channels'][0], {'type': 'telegram', 'id': '555', 'primary': True, 'label': '@alexex'})
        self.assertEqual((owner['name'], owner['short_name'], owner['timezone']), ('Alex Example', 'Alex', 'America/Chicago'))
        batch = json.loads((self.root / 'state' / 'telegram-setup.json').read_text())
        self.assertIn({'path': 'channels.telegram.dmPolicy', 'value': 'allowlist'}, batch)
        self.assertIn({'path': 'channels.telegram.allowFrom', 'value': ['555']}, batch)
        self.assertIn({'path': 'commands.ownerAllowFrom', 'value': ['telegram:555']}, batch)
        flat = [' '.join(c) for c in self.box.commands]
        self.assertTrue(any('config set --batch-file' in c for c in flat))
        self.assertTrue(any('up -d --no-build --force-recreate' in c for c in flat))
        self.assertIn('systemctl restart procurator-web procurator-mail', flat)
        self.assertEqual(self.net.sent[-1]['chat_id'], '555')
        self.assertIn('quick questions', self.net.sent[-1]['text'])
        welcome = json.loads((self.root / 'state' / 'procurator-admin' / 'welcome.json').read_text())
        self.assertEqual(welcome['status'], 'pending', 'a new owner gets the welcome interview on the first message')
        self.assertNotIn(KEY, json.dumps(out))
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_config_runs_inside_a_running_gateway_and_a_failure_leaves_no_owner(self):
        # 0.30.0 droplet test: a one-off container cannot take a running gateway's config lock (GatewayLockNamespaceError).
        class R:
            def __init__(self, out=''):
                self.stdout, self.returncode = out, 0
        cmds = []

        def running(cmd, **kw):
            cmds.append(cmd)
            return R('abc123\n' if cmd[:3] == ['docker', 'compose', 'ps'] else '')
        box = core.Box(self.root, run=running)
        box.cli(['dist/index.js', 'config', 'validate'])
        self.assertEqual(cmds[-1][:6], ['docker', 'compose', 'exec', '-T', 'openclaw-gateway', 'node'])
        stopped = core.Box(self.root, run=lambda cmd, **kw: (cmds.append(cmd), R(''))[1])
        stopped.cli(['dist/index.js', 'config', 'validate'])
        self.assertEqual(cmds[-1][:4], ['docker', 'compose', 'run', '--rm'])

        def failing(cmd, **kw):
            if 'config' in cmd:
                raise subprocess.CalledProcessError(1, cmd, output='', stderr='lock held by ' + TOKEN)
            return R('')
        box = core.Box(self.root, run=failing)
        box.env.write_text('OPENCLAW_GATEWAY_TOKEN=x\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaisesRegex(core.SetupError, 'failed on the server'):
            core.finish(box, None, core.verify_bot(TOKEN, opener=self.net), {'id': '555', 'first_name': 'Alex'}, opener=self.net, restart=False)
        self.assertFalse(core.owner_configured(box.owner_file), 'a failed setup leaves no owner, so the same link can be retried')
        self.assertIn('lock held by [bot token]', err.getvalue(), 'the reason reaches the journal, secrets masked')
        self.assertNotIn(TOKEN, err.getvalue())

    def test_owner_file_keeps_what_the_owner_set_and_other_channels(self):
        self.box.owner_file.parent.mkdir(parents=True)
        self.box.owner_file.write_text(json.dumps({'schema': 2, 'name': 'Dr. A. Example', 'timezone': 'Europe/Madrid', 'email': 'owner@example.com',
                                                  'channels': [{'type': 'telegram', 'id': '1', 'primary': True},
                                                               {'type': 'slack', 'team': 'T1', 'id': 'U1', 'primary': False}]}))
        d = core.write_owner(self.box.owner_file, '777', name='Alex', timezone='America/New_York')
        self.assertEqual((d['name'], d['timezone'], d['email']), ('Dr. A. Example', 'Europe/Madrid', 'owner@example.com'))
        self.assertEqual([c['id'] for c in d['channels']], ['777', 'U1'])
        self.assertTrue(core.owner_configured(self.box.owner_file))

    def test_env_writer_refuses_injection(self):
        with self.assertRaises(core.SetupError):
            core.write_env(self.box.env, {'ANTHROPIC_API_KEY': 'a\nTELEGRAM_BOT_TOKEN=evil'})
        with self.assertRaises(core.SetupError):
            core.write_env(self.box.env, {'bad name': 'x'})

    def test_status_reports_without_secrets(self):
        core.write_env(self.box.env, {'ANTHROPIC_API_KEY': KEY})
        st = core.setup_status(self.root, owner_file=self.box.owner_file)
        self.assertTrue(st['model_key'])
        self.assertFalse(st['owner'])
        self.assertNotIn(KEY, json.dumps(st))


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / 'box'
        self.root.mkdir()
        self.state = Path(self.td.name) / 'state'
        self.net = FakeNet()
        self.box = FakeBox(self.root)
        self.links = wiz.SetupLinks(self.state)
        self.applied = []

        def apply(action, payload, timeout_s=0):
            # stands in for the root applier: same function, same box, run inline
            self.applied.append(action)
            req = {'id': 'r', 'action': action, **payload}
            out = core.apply_request(req, self.box, opener=self.net)
            if action == 'finish':
                self.links.complete(out.get('owner_first') or out.get('owner_name'), out.get('bot'))
            return out

        self.w = wiz.Wizard(self.links, self.box, apply=apply, opener=self.net, login_link=lambda nxt: 'https://box.example.com/assistant/dashboard/login/' + 'L' * 43 + '?next=' + nxt)
        self.url = self.links.mint('https://box.example.com')
        self.tok = self.url.rsplit('/', 1)[1]

    def tearDown(self):
        self.td.cleanup()

    def post(self, sub, body=None, origin_ok=True):
        return self.w.handle('POST', self.tok, sub, body or {}, origin_ok)

    def wait_state(self, want, t=5):
        end = time.time() + t
        while time.time() < end:
            st = self.w.status(self.links.check(self.tok))
            if st['state'] == want:
                return st
            time.sleep(0.02)
        self.fail('state never became %s: %s' % (want, self.w.status()))

    def test_link_is_hashed_single_use_and_expires(self):
        rec = json.loads((self.state / wiz.LINK_FILE).read_text())
        self.assertNotIn(self.tok, json.dumps(rec))
        self.assertEqual(oct((self.state / wiz.LINK_FILE).stat().st_mode & 0o777), '0o600')
        self.assertIsNone(self.links.check('x' * 43))
        self.assertIsNotNone(self.links.check(self.tok))
        later = wiz.SetupLinks(self.state, now=lambda: time.time() + 25 * 3600)
        self.assertIsNone(later.check(self.tok))
        self.assertTrue(self.url.startswith('https://box.example.com/assistant/dashboard/setup/'))

    def test_full_browser_flow(self):
        code, out = self.post('model', {'key': 'sk-ant-api03-' + 'z' * 40})
        self.assertEqual((code, out['ok']), (400, False))
        self.assertIn('did not accept', out['error'])
        self.assertEqual(self.post('model', {'key': KEY})[0], 200)
        self.assertEqual(core.read_env(self.box.env)['ANTHROPIC_API_KEY'], KEY)
        code, out = self.post('bot', {'token': TOKEN})
        self.assertEqual(out['bot'], '@alex_assistant_bot')
        self.assertNotIn('TELEGRAM_BOT_TOKEN', core.read_env(self.box.env), 'the token is only written once pairing succeeds')
        code, out = self.post('pair')
        self.assertTrue(out['link'].startswith('https://t.me/alex_assistant_bot?start='))
        st = self.w.handle('GET', self.tok, 'status', {}, True)[1]
        self.assertEqual(st['state'], 'waiting')
        self.assertNotIn(TOKEN, json.dumps(st))
        self.assertNotIn(KEY, json.dumps(st))
        self.net.press_start('/start ' + out['link'].rsplit('=', 1)[1])
        st = self.wait_state('done')
        self.assertEqual(st['owner'], 'Alex')
        self.assertIn('/login/', st['next'])
        self.assertEqual(self.applied, ['save_model', 'finish'])
        self.assertEqual(core.read_env(self.box.env)['PROCURATOR_OWNER_ID'], str(OWNER_ID))
        # after completion: status still answers (for the page across the broker restart) but nothing can be changed
        self.assertEqual(self.w.handle('GET', self.tok, 'status', {}, True)[1]['state'], 'done')
        self.assertEqual(self.post('model', {'key': KEY})[0], 409)
        # a fresh Wizard (the broker restarted) still shows done from the link record
        w2 = wiz.Wizard(self.links, self.box, apply=lambda *a, **k: None, opener=self.net, login_link=lambda n: 'L')
        self.assertEqual(w2.handle('GET', self.tok, 'status', {}, True)[1]['state'], 'done')

    def test_order_origin_and_existing_owner_guards(self):
        self.assertEqual(self.post('pair')[1]['error'], 'Add your bot first (step 2).')
        self.post('bot', {'token': TOKEN})
        self.assertIn('model key first', self.post('pair')[1]['error'])
        self.assertEqual(self.post('model', {'key': KEY}, origin_ok=False)[0], 403)
        core.write_owner(self.box.owner_file, '9')
        code, out = self.post('model', {'key': KEY})
        self.assertEqual(code, 410)
        self.assertIn('already has an owner', out['error'])
        url = self.links.mint('https://box.example.com', reconfigure=True)
        self.assertEqual(self.w.handle('POST', url.rsplit('/', 1)[1], 'model', {'key': KEY}, True)[0], 200)

    def test_busy_gateway_is_stopped_then_pairing_continues(self):
        self.post('model', {'key': KEY})
        self.post('bot', {'token': TOKEN})
        self.net.busy = True
        orig = self.w.apply

        def apply(action, payload, timeout_s=0):
            if action == 'stop_gateway':
                self.net.busy = False
            return orig(action, payload, timeout_s)
        self.w.apply = apply
        link = self.post('pair')[1]['link']
        time.sleep(0.1)
        self.net.press_start('/start ' + link.rsplit('=', 1)[1])
        self.wait_state('done')
        self.assertIn('stop_gateway', self.applied)

    def test_page_embeds_no_secret_and_escapes_status(self):
        html = wiz.page('NONCE', self.tok, {'bot': '</script><b>', 'state': 'idle'})
        self.assertNotIn('</script><b>', html)
        self.assertIn('nonce="NONCE"', html)


class ApplierTests(unittest.TestCase):
    def test_request_file_is_consumed_and_result_has_no_secret(self):
        with tempfile.TemporaryDirectory() as td:
            root, state = Path(td) / 'box', Path(td) / 'state'
            root.mkdir()
            box = FakeBox(root)
            net = FakeNet()
            links = wiz.SetupLinks(state)
            links.mint('https://box.example.com')
            (state / 'setup').mkdir(parents=True)
            (state / 'setup' / 'request.json').write_text(json.dumps({'id': 'abc', 'action': 'finish', 'bot_token': TOKEN,
                                                                      'owner': {'id': '555', 'first_name': 'Alex'}}))
            out = wiz.run_applier(state, box, opener=net)
            self.assertTrue(out['ok'])
            self.assertFalse((state / 'setup' / 'request.json').exists())
            res = (state / 'setup' / 'result.json').read_text()
            self.assertEqual(json.loads(res)['id'], 'abc')
            self.assertNotIn(TOKEN, res)
            self.assertIn('systemctl restart procurator-web procurator-mail', [' '.join(c) for c in box.commands])
            self.assertTrue(json.loads((state / wiz.LINK_FILE).read_text())['done'])
            self.assertIsNone(wiz.run_applier(state, box, opener=net), 'nothing pending: nothing done')
            (state / 'setup' / 'request.json').write_text(json.dumps({'id': 'x', 'action': 'finish', 'bot_token': 'bad', 'owner': {'id': '1'}}))
            out = wiz.run_applier(state, box, opener=net)
            self.assertFalse(out['ok'])

    def test_file_applier_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            fa = core.FileApplier(td, sleep=lambda s: None)
            done = threading.Event()

            def applier():
                d = Path(td) / 'setup'
                for _ in range(500):
                    if (d / 'request.json').exists():
                        req = json.loads((d / 'request.json').read_text())
                        (d / 'request.json').unlink()
                        (d / 'result.json').write_text(json.dumps({'id': req['id'], 'ok': True}))
                        done.set()
                        return
                    time.sleep(0.005)
            t = threading.Thread(target=applier)
            t.start()
            ticks = iter(range(10 ** 6))
            fa.clock = lambda: next(ticks) * 0.001
            self.assertTrue(fa('stop_gateway', {}, timeout_s=100)['ok'])
            t.join()

    def test_file_applier_reports_a_failed_step_at_once(self):
        # SetupError is a ValueError: it used to be swallowed by the read loop and the page sat on "finishing" for 5 minutes.
        with tempfile.TemporaryDirectory() as td:
            fa = core.FileApplier(td, sleep=lambda s: None)
            d = Path(td) / 'setup'

            def applier():
                for _ in range(2000):
                    if (d / 'request.json').exists():
                        req = json.loads((d / 'request.json').read_text())
                        (d / 'request.json').unlink()
                        (d / 'result.json').write_text(json.dumps({'id': req['id'], 'ok': False, 'error': 'docker said no'}))
                        return
                    time.sleep(0.002)
            t = threading.Thread(target=applier)
            t.start()
            started = time.monotonic()
            fa.clock = time.monotonic
            fa.sleep = lambda s: time.sleep(0.005)
            with self.assertRaisesRegex(core.SetupError, 'docker said no'):
                fa('finish', {}, timeout_s=30)
            self.assertLess(time.monotonic() - started, 5)
            t.join()


class DashboardRouteTests(unittest.TestCase):
    """The dashboard serves the wizard before its sign-in check, and only for the live link."""

    def test_routes(self):
        import http.client
        import dashboard
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'box'
            root.mkdir()
            net = FakeNet()

            class Broker:
                path = str(Path(td) / 'broker.sqlite3')

                def call(self, *a, **k):
                    return {'providers': []}
            dash = dashboard.Dashboard(Broker(), td, str(Path(td) / 'dashboard.sqlite3'))
            box = FakeBox(root)
            links = wiz.SetupLinks(td)
            dash.setup = wiz.Wizard(links, box, apply=lambda a, p, t=0: core.apply_request({'action': a, **p}, box, opener=net), opener=net,
                                    login_link=dash.sessions.login_link)
            tok = links.mint('https://box.example.com').rsplit('/', 1)[1]
            srv = dashboard.Server(('127.0.0.1', 0), dashboard.handler_for(dash))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def req(method, path, body=None, headers=None):
                    c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
                    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
                    r = c.getresponse()
                    return r.status, r.read().decode()
                st, page = req('GET', '/assistant/dashboard/setup/' + tok)
                self.assertEqual(st, 200)
                self.assertIn('Three steps', page)
                self.assertEqual(req('GET', '/assistant/dashboard/setup/' + 'Q' * 43)[0], 410)
                h = {'Content-Type': 'application/json', 'X-Procurator-Setup': '1'}
                self.assertEqual(req('POST', '/assistant/dashboard/setup/%s/model' % tok, {'key': KEY}, {'Content-Type': 'application/json'})[0], 403)
                st, out = req('POST', '/assistant/dashboard/setup/%s/model' % tok, {'key': KEY}, h)
                self.assertEqual((st, json.loads(out)['ok']), (200, True))
                st, out = req('GET', '/assistant/dashboard/setup/%s/status' % tok)
                self.assertTrue(json.loads(out)['model'])
                self.assertNotIn(KEY, out)
                self.assertEqual(req('GET', '/assistant/dashboard/setup')[0], 401, 'the Setup tab itself needs a session')
            finally:
                srv.shutdown()


if __name__ == '__main__':
    unittest.main()
