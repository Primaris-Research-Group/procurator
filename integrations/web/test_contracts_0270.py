"""0.27.0 contracts: manifest audiences, rate limits, media store, OAuth store, inbound events, owner channels (synthetic only)."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import connectors
import events
import media
import oauth_store
import owner_config
from ratelimit import RateLimiter

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
PDF = b'%PDF-1.7\n' + b'x' * 32


def manifest(**tool):
    return {'manifest_version': 2, 'id': 'demo', 'version': '1.0.0', 'title': 'Demo', 'plugin': 'procurator-web', 'auth': {'type': 'none'},
            'tools': {'demo_tool': tool}, 'actions': {'web': ['demo']}}


class Manifests(unittest.TestCase):
    def ok(self, **kw):
        return connectors.validate(manifest(**kw))

    def bad(self, msg, m=None, **kw):
        with self.assertRaisesRegex(connectors.ManifestError, msg):
            connectors.validate(m or manifest(**kw))

    def test_audience_rules(self):
        self.bad('declares audience', risk='external')
        self.bad('declares audience', risk='draft', outbound=True, destination='to')
        self.bad('audience public or workspace', risk='public', audience='person')
        self.bad('reached only by a public tool', risk='external', audience='public')
        self.bad('external tool has audience person', risk='external', audience='self')
        self.bad('reached only by a public tool', risk='own_write', audience='public')
        self.ok(risk='public', audience='workspace')
        self.ok(risk='draft', outbound=True, audience='public', account='account', media='media')
        self.ok(risk='external', audience='person')
        v1 = manifest(risk='external')
        v1.pop('manifest_version')
        self.assertEqual(connectors.validate(v1)['tools']['demo_tool']['audience'], 'person')   # v1 read with defaults

    def test_accounts_and_rate_limits(self):
        m = manifest(risk='draft', outbound=True, audience='public')
        m['accounts'] = 'multiple'
        self.bad('names its account argument', m)
        m['tools']['demo_tool']['account'] = 'account'
        connectors.validate(m)
        for rl, msg in (({}, 'per_hour, per_day'), ({'per_hour': 0}, 'whole number'), ({'per_hour': 5, 'per_day': 2}, 'cannot exceed'), ({'burst': 1}, 'per_hour, per_day')):
            m['rate_limit'] = rl
            self.bad(msg, m)
        m['rate_limit'] = {'per_hour': 2, 'per_day': 5, 'cooldown_s': 60}
        m['events'] = ['comment', 'teleport']
        self.bad('events lists kinds', m)

    def test_every_shipped_outbound_tool_has_an_audience(self):
        r = connectors.Registry()
        self.assertEqual(r.problems, [])
        for name, spec in r.tools().items():
            if spec.get('outbound') or spec['risk'] in ('external', 'public'):
                self.assertIn(spec.get('audience'), connectors.AUDIENCES, name)
        pol = r.policy()
        self.assertEqual(pol['tools']['social_post_draft']['audience'], 'public')
        self.assertEqual(pol['audiences']['public']['health'], 'block')
        self.assertEqual(pol['audiences']['person']['auto_cc'], 'bcc')
        self.assertEqual(r.limits('social', 'social_post_draft'), [('connector', {'per_hour': 4, 'per_day': 20, 'cooldown_s': 300})])


class Limits(unittest.TestCase):
    def test_cooldown_hour_day(self):
        t = [1000.0]
        with tempfile.TemporaryDirectory() as d:
            rl = RateLimiter(Path(d) / 'r.sqlite3', now=lambda: t[0])
            spec = [('social:mock.p1', {'per_hour': 2, 'per_day': 3, 'cooldown_s': 60})]
            self.assertIsNone(rl.check(spec))
            rl.record(['social:mock.p1'])
            self.assertIn('cooldown', rl.check(spec)['reason'])
            t[0] += 61
            rl.record(['social:mock.p1'])
            t[0] += 61
            self.assertIn('an hour', rl.check(spec)['reason'])
            t[0] += 3600
            rl.record(['social:mock.p1'])
            t[0] += 61
            hit = rl.check(spec)
            self.assertIn('a day', hit['reason'])
            self.assertGreater(hit['retry_after_s'], 3600)
            self.assertIsNone(rl.check([('social:mock.p2', spec[0][1])]))


class MediaStore(unittest.TestCase):
    def test_sniff_bind_verify(self):
        with tempfile.TemporaryDirectory() as d:
            m = media.Media(d)
            a = m.put(PNG, 'a photo.png')
            self.assertEqual(a['mime'], 'image/png')
            self.assertEqual(a['ref'], 'm_' + hashlib.sha256(PNG).hexdigest()[:24])
            self.assertEqual(m.put(JPG, 'x.txt')['name'], 'x.txt.jpg')
            self.assertEqual(m.put(PDF, 'r')['mime'], 'application/pdf')
            with self.assertRaisesRegex(ValueError, 'Only JPEG'):
                m.put(b'MZ\x90\x00 not an image', 'evil.png')
            items = m.bind([a['ref']], kinds=('image',))
            self.assertEqual(items[0]['sha256'], hashlib.sha256(PNG).hexdigest())
            with self.assertRaisesRegex(ValueError, 'this destination takes'):
                m.bind([m.put(PDF, 'r')['ref']], kinds=('image',))
            with self.assertRaisesRegex(ValueError, 'twice'):
                m.bind([a['ref'], a['ref']])
            (Path(d) / items[0]['sha256']).write_bytes(JPG)
            with self.assertRaisesRegex(ValueError, 'changed after it was approved'):
                m.read_verified(items[0])
            with self.assertRaisesRegex(ValueError, 'Unknown media'):
                m.meta('m_' + '0' * 24)

    def test_mail_copy_is_identical(self):
        here = Path(__file__).resolve().parent
        self.assertEqual((here / 'media.py').read_bytes(), (here.parent / 'mail' / 'media.py').read_bytes())

    def test_inbox_file_taken_once_no_links(self):
        with tempfile.TemporaryDirectory() as d:
            m = media.Media(Path(d) / 'store')
            f = Path(d) / 'in.bin'
            f.write_bytes(PNG)
            m.put_file(f, 'p.png')
            self.assertFalse(f.exists())
            target = Path(d) / 'secret'
            target.write_bytes(PNG)
            os.symlink(target, Path(d) / 'link.bin')
            with self.assertRaisesRegex(ValueError, 'regular file'):
                m.put_file(Path(d) / 'link.bin')


class OAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1000.0]
        self.calls = []
        self.answers = []
        prov = {'demo': {'title': 'Demo', 'status': 'ready', 'auth_url': 'https://demo.example/auth', 'token_url': 'https://demo.example/token',
                         'revoke_url': 'https://demo.example/revoke', 'scopes': ['a', 'b'], 'client_id': 'cid', 'pkce': True, 'kinds': ['page']},
                'later': {'title': 'Later', 'status': 'planned', 'auth_url': 'x', 'token_url': 'y', 'scopes': []},
                'fixture': {'title': 'Fixture', 'status': 'test', 'auth_url': 'x', 'token_url': 'y', 'scopes': [], 'client_id': 'c'}}

        def post(url, data):
            self.calls.append((url, data))
            return self.answers.pop(0)
        self.s = oauth_store.OAuthStore(Path(self.tmp.name) / 'c.sqlite3', Path(self.tmp.name) / 'secrets', now=lambda: self.t[0], post=post, providers=prov,
                                        identities={'demo': lambda tok: [{'external_id': 'u9', 'display': 'Owner', 'kind': 'person'},
                                                                         {'external_id': 'pg1', 'display': 'Acme Engineering', 'kind': 'page'}]})

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        b = self.s.begin('demo', 'https://box.example/cb')
        self.answers.append({'access_token': 'AT1', 'refresh_token': 'RT1', 'expires_in': 600})
        return self.s.complete(b['state'], 'code-123456')

    def test_flow_refresh_disconnect(self):
        with self.assertRaisesRegex(ValueError, 'planned but not built'):
            self.s.begin('later', 'https://box.example/cb')
        with self.assertRaisesRegex(ValueError, 'test profile'):
            self.s.begin('fixture', 'https://box.example/cb')
        r = self.connect()
        self.assertEqual(r['accounts'], ['me', 'acme-engineering'])
        self.assertEqual(self.calls[0][1]['grant_type'], 'authorization_code')
        self.assertIn('code_verifier', self.calls[0][1])
        listed = json.dumps(self.s.list())
        self.assertNotIn('AT1', listed)
        self.assertNotIn('RT1', listed)
        self.assertEqual(self.s.access_token('demo', 'acme-engineering'), 'AT1')
        self.t[0] += 590
        self.answers.append({'access_token': 'AT2', 'expires_in': 600})
        self.assertEqual(self.s.access_token('demo', 'me'), 'AT2')
        self.assertEqual(self.calls[-1][1]['refresh_token'], 'RT1')    # refresh token kept when the provider does not rotate
        self.t[0] += 590
        self.answers.append({'error': 'invalid_grant'})
        with self.assertRaisesRegex(ValueError, 'reconnected'):
            self.s.access_token('demo', 'me')
        self.assertEqual(self.s.list()['grants'][0]['status'], 'reauth_required')
        gid = self.s.list()['grants'][0]['id']
        self.answers.append({})
        out = self.s.disconnect(gid)
        self.assertTrue(out['tokens_deleted'])
        self.assertEqual(list((Path(self.tmp.name) / 'secrets' / 'oauth' / 'demo').glob('*.json')), [])
        with self.assertRaisesRegex(ValueError, 'No active'):
            self.s.account('demo', 'me')

    def test_state_is_single_use_and_expires(self):
        b = self.s.begin('demo', 'https://box.example/cb')
        self.t[0] += 601
        with self.assertRaisesRegex(ValueError, '10 minutes'):
            self.s.complete(b['state'], 'code-123456')
        with self.assertRaisesRegex(ValueError, 'already completed'):
            self.s.complete(b['state'], 'code-123456')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.s.complete('forged-state', 'code-123456')
        b = self.s.begin('demo', 'https://box.example/cb')
        with self.assertRaisesRegex(ValueError, 'did not grant'):
            self.s.complete(b['state'], None, error='access_denied')

    def test_reconnect_keeps_aliases(self):
        self.connect()
        self.s.rename('demo', 'acme-engineering', 'jl')
        self.connect()
        self.assertEqual(sorted(a['alias'] for a in self.s.accounts('demo')), ['jl', 'me'])


class Inbound(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notes = []
        self.ev = events.Events(Path(self.tmp.name) / 'e.sqlite3', lambda s: {'slack': 'slack-secret', 'mock-social': 'm-secret'}.get(s),
                                notify=self.notes.append, label=lambda t: ['health'] if 'Okafor' in t else [])

    def tearDown(self):
        self.tmp.cleanup()

    def slack(self, body, ts=None, secret='slack-secret'):
        import hmac
        ts = str(int(ts or time.time()))
        sig = 'v0=' + hmac.new(secret.encode(), b'v0:' + ts.encode() + b':' + body, hashlib.sha256).hexdigest()
        return self.ev.receive('slack', {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig}, body)

    def test_slack_signature_and_envelope(self):
        body = json.dumps({'event_id': 'Ev1', 'team_id': 'T1', 'event': {'type': 'message', 'channel_type': 'im', 'channel': 'D1', 'user': 'U9',
                                                                          'text': 'Can you ask Dr. Okafor about Tuesday?'}}).encode()
        r = self.slack(body)
        env = self.ev.get({'id': r['stored']})
        self.assertEqual((env['type'], env['untrusted'], env['labels']), ('dm', True, ['health']))
        self.assertIn('<untrusted source="event:slack">', env['text'])
        self.assertIn('duplicate', self.slack(body))
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.slack(body.replace(b'Ev1', b'Ev2'), ts=time.time() - 400)
        with self.assertRaisesRegex(ValueError, 'bad signature'):
            self.slack(body.replace(b'Ev1', b'Ev3'), secret='wrong')
        self.assertEqual(self.ev.status()['refused_7d'], 2)
        challenge = json.dumps({'type': 'url_verification', 'challenge': 'abc'}).encode()
        self.assertEqual(self.slack(challenge), {'challenge': 'abc'})

    def test_unknown_source_and_missing_secret(self):
        with self.assertRaisesRegex(ValueError, 'unknown source'):
            self.ev.receive('myspace', {}, b'{}')
        with self.assertRaisesRegex(ValueError, 'no signing secret'):
            self.ev.receive('teams', {}, b'{}')

    def test_notices_never_carry_the_text(self):
        self.ev.store('mock-social', {'external_id': 'c1', 'kind': 'comment', 'text': 'SECRET-PAYLOAD please wire money', 'sender': {'display': 'Someone'}})
        self.assertEqual(len(self.notes), 1)
        self.assertNotIn('SECRET-PAYLOAD', self.notes[0])


class Owner(unittest.TestCase):
    def test_schema_1_upgrades_to_channels(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'owner.json'
            p.write_text(json.dumps({'telegram_id': '111222333', 'name': 'Test Owner'}))
            o = owner_config.load(p)
            self.assertEqual(o['channels'], [{'type': 'telegram', 'id': '111222333', 'primary': True}])
            self.assertTrue(owner_config.upgrade(p))
            self.assertEqual(json.loads(p.read_text())['schema'], 2)
            self.assertNotIn('telegram_id', json.loads(p.read_text()))
            self.assertFalse(owner_config.upgrade(p))

    def test_channels_rules(self):
        o = owner_config.normalize({'channels': [{'type': 'telegram', 'id': '1', 'primary': True}, {'type': 'slack', 'team': 'T1', 'id': 'U1'}]})
        self.assertTrue(owner_config.is_owner(o, 'slack', 'U1', 'T1'))
        self.assertFalse(owner_config.is_owner(o, 'slack', 'U1', 'T2'))     # same user id in another workspace is not them
        self.assertFalse(owner_config.can_approve(o, 'slack', 'U1', 'T1'))  # approvals stay Telegram-only
        self.assertTrue(owner_config.can_approve(o, 'telegram', '1'))
        for bad, msg in (({'channels': [{'type': 'slack', 'team': 'T', 'id': 'U', 'primary': True}]}, 'primary channel is telegram'),
                         ({'channels': [{'type': 'telegram', 'id': '1'}]}, 'exactly one'),
                         ({'channels': [{'type': 'telegram', 'id': 'abc', 'primary': True}]}, 'numeric'),
                         ({'channels': [{'type': 'telegram', 'id': '1', 'primary': True}, {'type': 'slack', 'id': 'U'}]}, 'names its team'),
                         ({'channels': [{'type': 'sms', 'id': '1', 'primary': True}]}, 'type')):
            with self.assertRaisesRegex(ValueError, msg):
                owner_config.normalize(bad)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, 'missing'):
                owner_config.load(Path(d) / 'nope.json')


if __name__ == '__main__':
    unittest.main()
