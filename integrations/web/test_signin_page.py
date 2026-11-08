"""Phone sign-in page (Nov 4: Browserbase live view has no phone keyboard; Leaveco login impossible)."""
import json
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from broker import Broker
import signin_page
from test_site_logins import SiteCloud, origin_of


class KeyCloud(SiteCloud):
    def __init__(self):
        super().__init__(); self.keys = []; self.login_result = {'ok': True, 'submitted': True}
    def run(self, s, operation, **kw):
        if operation == 'owner_keys':
            self.keys.append(kw); return {'ok': True}
        if operation in ('owner_login', 'owner_code', 'owner_tap', 'owner_scroll'):
            self.keys.append({'op': operation, **kw}); return self.login_result if operation in ('owner_login', 'owner_code') else {'ok': True}
        if operation == 'owner_shot':
            import base64; return {'ok': True, 'image_b64': base64.b64encode(b'\xff\xd8\xff\xe0fake-jpeg').decode(), 'host': 'www.leaveco.example'}
        return super().run(s, operation, **kw)


def free_port():
    with socket.socket() as x:
        x.bind(('127.0.0.1', 0)); return x.getsockname()[1]


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = KeyCloud(); self.b = Broker(Path(self.tmp.name) / 'b.db', self.p)
        patcher = patch('broker.url', side_effect=origin_of); patcher.start(); self.addCleanup(patcher.stop)
        self.b.url_validator = origin_of
        self.port = free_port(); self.server = signin_page.start(self.b, self.port); self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.r = self.b.call('site_connect', {'site': 'leaveco'}, True)
        self.token = self.r['signin_url'].rsplit('/', 1)[1]
        self.base = f'http://127.0.0.1:{self.port}/assistant/signin/{self.token}'
    def get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as r: return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e: return e.code, dict(e.headers), e.read().decode()
    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b'{}')
    def test_connect_returns_a_private_phone_link_and_stores_only_its_hash(self):
        self.assertTrue(self.r['signin_url'].startswith('https://assistant.example.com/assistant/signin/'))
        self.assertGreaterEqual(len(self.token), 40)
        row = self.b.sites.row('leaveco'); self.assertNotIn(self.token, json.dumps(row)); self.assertEqual(len(row['signin_hash']), 64)
    def test_page_shows_live_view_and_typing_bar_with_strict_headers(self):
        status, headers, body = self.get(self.base)
        self.assertEqual(status, 200)
        # Nov 5: no live-view iframe in the page itself (it blocked the iPhone keyboard); computers add it from data-live.
        self.assertNotIn('<iframe', body); self.assertIn('data-live="https://browserbase.com/private-fixture?navbar=false"', body)
        self.assertIn('id="screen"', body); self.assertIn("matchMedia('(hover: hover) and (pointer: fine)')", body)
        self.assertIn('autocomplete="current-password"', body); self.assertIn('I’m signed in', body)
        self.assertIn('autocomplete="username"', body); self.assertIn('autocomplete="one-time-code"', body)
        self.assertRegex(body, r'<form id="login" autocomplete="on">\s*<input id="username"[^>]*>\s*<input id="password" name="password" type="password"')
        csp = headers['Content-Security-Policy']; self.assertIn("frame-ancestors 'none'", csp); self.assertIn("img-src 'self'", csp); self.assertIn("script-src 'nonce-", csp)
        self.assertEqual(headers['Cache-Control'], 'no-store'); self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
    def test_typing_goes_to_the_cloud_browser_and_is_not_kept(self):
        self.assertEqual(self.post('/keys', {'text': 'owner.p'}), (200, {'ok': True, 'message': 'Sent. Check the page above.'}))
        self.post('/keys', {'key': 'Tab'}); self.post('/keys', {'clear': True})
        self.assertEqual([(k['text'], k['key'], k['clear']) for k in self.p.keys], [('owner.p', None, False), ('', 'Tab', False), ('', None, True)])
        with self.b.db() as db:
            dump = '\n'.join(str(tuple(r)) for t in ('site_logins', 'sessions', 'plans', 'task_events') for r in db.execute('SELECT * FROM ' + t))
        self.assertNotIn('owner.p', dump)
        for bad in [{'text': 'x' * 257}, {'text': 'a\nb'}, {'key': 'F12'}]:
            self.assertFalse(self.post('/keys', bad)[1]['ok'])
        self.assertEqual(self.post('/keys', {'text': 'x'}, {'Origin': 'https://evil.example'})[0], 403)
    def test_bad_or_expired_tokens_and_the_bot_get_nothing(self):
        status, _, body = self.get(f'http://127.0.0.1:{self.port}/assistant/signin/' + 'A' * 43)
        self.assertEqual(status, 410); self.assertIn('expired', body)
        with self.assertRaisesRegex(ValueError, 'Only the owner sign-in page'):
            self.b.call('site_signin_keys', {'token': self.token, 'text': 'x'})          # tools socket (the bot)
        with self.b.db() as db: db.execute('UPDATE site_logins SET signin_expires=?', (time.time() - 1,))
        self.assertIn('expired', self.post('/keys', {'text': 'x'})[1]['error']); self.assertEqual(self.p.keys, [])
    def test_im_signed_in_saves_the_login_once_and_tells_the_owner(self):
        status, out = self.post('/done', {})
        self.assertTrue(out['saved']); self.assertEqual(self.b.sites.row('leaveco')['status'], 'owner_connected')
        self.assertIn('Signed in to leaveco', self.p.messages[-1]); self.assertIn('same browser', self.p.messages[-1])
        self.assertIn('continues by itself', out['message'])
        # the browser the owner signed in to stays open for the assistant, and the gateway is told once to wake it
        sid = self.r['session_id']; self.assertEqual(self.b.session(sid)['status'], 'open')
        self.assertEqual(self.b.call('site_list', {})['sites'][0]['open_browser'], sid)
        self.assertEqual([e['browser'] for e in self.b.call('site_signin_events', {})['events']], [sid])
        self.assertEqual(self.b.call('site_signin_events', {})['events'], [])
        self.assertIn('expired', self.post('/keys', {'text': 'x'})[1]['error'])   # link is single-use

    def test_password_manager_sign_in_and_code_are_filled_by_the_server_and_not_kept(self):
        status, out = self.post('/login', {'username': 'owner.p', 'password': 'S3cret!pw'})
        self.assertEqual((status, out['ok']), (200, True)); self.assertIn('Submitted', out['message'])
        self.assertEqual(self.p.keys[-1], {'op': 'owner_login', 'username': 'owner.p', 'password': 'S3cret!pw'})
        self.assertTrue(self.post('/code', {'code': ' 482913 '})[1]['ok']); self.assertEqual(self.p.keys[-1], {'op': 'owner_code', 'code': '482913'})
        with self.b.db() as db:
            dump = '\n'.join(str(tuple(r)) for t in ('site_logins', 'sessions', 'plans', 'task_events') for r in db.execute('SELECT * FROM ' + t))
        self.assertNotIn('S3cret', dump); self.assertNotIn('482913', dump)
        self.assertIn('Fill the password', self.post('/login', {'username': 'a', 'password': ''})[1]['error'])
        self.assertFalse(self.post('/code', {'code': '12 34; drop'})[1]['ok'])
        self.p.login_result = {'ok': False, 'reason': 'no_login_form'}
        self.assertIn('Log in / Sign in link first', self.post('/login', {'username': 'a', 'password': 'b'})[1]['error'])
        with self.assertRaisesRegex(ValueError, 'Only the owner sign-in page'):
            self.b.call('site_signin_login', {'token': self.token, 'password': 'x'})

    def test_phone_picture_taps_and_scrolls_reach_the_browser_and_only_the_owner(self):
        req = urllib.request.Request(self.base + '/shot?t=1')
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers['Content-Type'], 'image/jpeg'); self.assertTrue(r.read().startswith(b'\xff\xd8'))
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.get(self.base + '/shot')[0], 429)            # at most about one picture a second
        self.assertEqual(self.post('/tap', {'x': 0.5, 'y': 0.25}), (200, {'ok': True}))
        self.assertEqual(self.p.keys[-1], {'op': 'owner_tap', 'x': 0.5, 'y': 0.25})
        self.assertTrue(self.post('/scroll', {'direction': 'down'})[1]['ok']); self.assertEqual(self.p.keys[-1], {'op': 'owner_scroll', 'direction': 'down'})
        for bad in ({'x': 2, 'y': 0}, {'x': 'a', 'y': 0}, {}):
            self.assertFalse(self.post('/tap', bad)[1]['ok'])
        self.assertFalse(self.post('/scroll', {'direction': 'left'})[1]['ok'])
        for action in ('site_signin_shot', 'site_signin_tap', 'site_signin_scroll'):
            with self.assertRaisesRegex(ValueError, 'Only the owner sign-in page'):
                self.b.call(action, {'token': self.token, 'x': 0.1, 'y': 0.1, 'direction': 'up'})      # the bot
        self.assertEqual(self.get(f'http://127.0.0.1:{self.port}/assistant/signin/' + 'A' * 43 + '/shot')[0], 410)

    def test_expired_saved_login_is_signed_in_again_in_the_same_browser(self):
        # Nov 5 Leaveco: logins made in a separate private browser did not carry over, so the re-sign-in uses the bot's browser.
        self.post('/done', {}); sid = self.r['session_id']
        again = self.b.call('open', {'request_id': 'leaveco-open-9', 'url': 'https://www.leaveco.example/claims', 'site_login': 'leaveco'})
        self.assertEqual(again['id'], sid); self.assertTrue(again['reused_signed_in_browser'])
        self.p.snap.update({'url': 'https://www.leaveco.example/login', 'controls': [{'ref': 'e1', 'type': 'password', 'label': 'Password', 'tag': 'input'}]})
        snap = self.b.call('snapshot', {'id': sid})['snapshot']; self.assertIn('opens THIS browser', snap['site_login_guidance'])
        r = self.b.call('site_signin_request', {'site': 'leaveco'})
        self.assertTrue(r['sent']); self.assertEqual(r['browser'], sid); self.assertIn('Do not open a new browser', r['note'])
        self.assertIn('already using', self.p.messages[-1]); self.assertEqual(self.b.session(sid)['status'], 'open')
        with self.b.db() as db: self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'signing in to .* privately'): self.b.call('snapshot', {'id': sid})
        token = self.p.messages[-1].split('/assistant/signin/')[1].split()[0]
        self.base = f'http://127.0.0.1:{self.port}/assistant/signin/{token}'
        self.assertEqual(self.get(self.base)[0], 200)
        self.p.snap.update({'url': 'https://www.leaveco.example/claims', 'controls': []})
        out = self.post('/done', {})[1]; self.assertTrue(out['saved'])
        self.assertEqual(self.b.sites.row('leaveco')['status'], 'owner_connected'); self.assertEqual(self.b.session(sid)['status'], 'open')
        self.assertNotIn('site_login_status', self.b.call('snapshot', {'id': sid})['snapshot'])
        self.assertEqual([e['browser'] for e in self.b.call('site_signin_events', {})['events']], [sid, sid])


if __name__ == '__main__':
    unittest.main()


class TakeoverPageTests(unittest.TestCase):
    """Nov 6 (Leaveco, live): takeover links were the raw Browserbase live view, which has no iPhone keyboard or password
    manager. Every takeover now opens the private phone page for THAT browser."""
    get, post = Tests.get, Tests.post
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = KeyCloud(); self.b = Broker(Path(self.tmp.name) / 'b.db', self.p)
        patcher = patch('broker.url', side_effect=origin_of); patcher.start(); self.addCleanup(patcher.stop)
        self.b.url_validator = origin_of
        self.port = free_port(); self.server = signin_page.start(self.b, self.port); self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.sid = self.b.call('open', {'request_id': 'leaveco-task-browser-1', 'url': 'https://www.leaveco.example/login'})['id']

    def takeover_token(self):
        out = self.b.call('takeover_request', {'id': self.sid})              # the bot's tool
        self.assertTrue(out['sent']); self.assertNotIn('http', json.dumps(out))   # the link never reaches the bot
        msg = self.p.messages[-1]
        self.assertIn('https://assistant.example.com/assistant/signin/', msg); self.assertNotIn('browserbase.com', msg)
        self.assertIn('password', msg.lower()); self.assertIn('I’m done', msg)
        return msg.split('/assistant/signin/')[1].split()[0]

    def test_bot_takeover_sends_the_phone_page_with_typing_and_password_manager(self):
        token = self.takeover_token()
        base = f'http://127.0.0.1:{self.port}/assistant/signin/{token}'
        status, _, body = self.get(base)
        self.assertEqual(status, 200); self.assertIn('Private browser: leaveco.example', body); self.assertIn('I’m done', body)
        self.assertIn('autocomplete="current-password"', body); self.assertIn('id="screen"', body); self.assertNotIn('<iframe', body)
        self.base = base
        self.assertTrue(self.post('/login', {'username': 'owner.p', 'password': 'S3cret!pw'})[1]['ok'])
        self.assertEqual(self.p.keys[-1]['op'], 'owner_login')
        self.assertTrue(self.post('/keys', {'text': '12345'})[1]['ok'])
        with self.b.db() as db:
            dump = '\n'.join(str(tuple(r)) for t in ('owner_pages', 'sessions', 'site_logins') for r in db.execute('SELECT * FROM ' + t))
        self.assertNotIn('S3cret', dump); self.assertNotIn(token, dump)
        status, out = self.post('/done', {})
        self.assertTrue(out['saved']); self.assertIn('continues by itself', out['message'])
        ev = self.b.call('site_signin_events', {})['events']
        self.assertEqual([(e['browser'], e['kind'], e['site']) for e in ev], [(self.sid, 'takeover', 'leaveco.example')])
        self.assertIn('expired', self.post('/keys', {'text': 'x'})[1]['error'])       # single use
        self.assertEqual(self.b.session(self.sid)['status'], 'open')                    # the owner finished in the bot's browser

    def test_only_the_newest_link_works_and_only_the_owner_gets_one(self):
        first = self.takeover_token(); second = self.takeover_token()
        self.assertEqual(self.get(f'http://127.0.0.1:{self.port}/assistant/signin/{first}')[0], 410)
        self.assertEqual(self.get(f'http://127.0.0.1:{self.port}/assistant/signin/{second}')[0], 200)
        with self.assertRaisesRegex(ValueError, 'only to the owner'):
            self.b.call('site_takeover_page', {'id': self.sid})                       # tools socket (the bot)
        page = self.b.call('site_takeover_page', {'id': self.sid}, True)              # /webtakeover (owner)
        self.assertTrue(page['page_url'].startswith('https://assistant.example.com/assistant/signin/')); self.assertGreater(page['minutes'], 0)

    def test_closed_or_home_browsers_get_no_page(self):
        with self.b.db() as db: db.execute("UPDATE sessions SET provider_id='home:pc:x' WHERE id=?", (self.sid,))
        with self.assertRaisesRegex(ValueError, 'PC at home'):
            self.b.call('site_takeover_page', {'id': self.sid}, True)
        with self.b.db() as db: db.execute("UPDATE sessions SET status='closed' WHERE id=?", (self.sid,))
        with self.assertRaisesRegex(ValueError, 'not open'):
            self.b.call('site_takeover_page', {'id': self.sid}, True)
