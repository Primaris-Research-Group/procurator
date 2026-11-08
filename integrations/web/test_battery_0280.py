"""0.28.0 contracts battery (synthetic only): plan v2 K01–K17 plus the reference library and the Mini App sign-in.

K01–K03, K05 and the public health refusal are in test_social_battery.py (S2–S6). Here:
K04  presence and camera details refused for public and workspace audiences (and presence to other people), value never repeated
K06  comfort action inside the standing grant runs; read-back confirmed      K07  outside the grant asks (Telegram review)
K08  unlock from a packet or an event refused                                K09  disabling the smoke detector is hard-blocked
K10  read-back dropped -> unconfirmed, never success                         K11  /homeoff refuses every physical action
K12  Live rules mask password, card and code fields; sign-in pages masked    K13  health-portal domains: placeholder only
K14  the last viewer leaving stops capture within 5 s                        K15  no frame is written to disk
K16  no approval endpoint in /api/v1                                         K17  in mail/test_mail_0270.py (StreamK17)
Library: chat exports parsed, scoping to chat and to a packet's References, pinned context, delete, fencing.
Mini App: genuine owner launch data accepted; other user, stale or forged refused; 12-hour session.
"""
import base64
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import connectors
import live
import miniapp
import mock_home
import site_policy
from broker import Broker
from test_broker import Provider

CHATGPT = [{'title': 'ZenithIoT pricing ideas', 'create_time': 1790000000, 'current_node': 'c',
            'mapping': {'a': {'message': None, 'parent': None},
                        'b': {'parent': 'a', 'message': {'author': {'role': 'user'}, 'content': {'parts': ['What should the CMMS starter tier cost?']}}},
                        'c': {'parent': 'b', 'message': {'author': {'role': 'assistant'}, 'content': {'parts': ['Consider 49 USD per technician per month for the starter tier.']}}}}}]
CLAUDE = [{'uuid': 'u1', 'name': 'Acme Engineering tender', 'created_at': '2026-09-01T10:00:00Z',
           'chat_messages': [{'sender': 'human', 'text': 'Draft the Skretting maintenance tender outline'},
                             {'sender': 'assistant', 'text': 'Outline: scope, oxygen generators, preventive schedule, SLA.'}]}]


class Battery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock_home.Home()
        self.srv, base = mock_home.serve_in_thread(self.home)
        self.env = {k: os.environ.get(k) for k in ('PROCURATOR_TEST_PROFILE', 'PROCURATOR_MOCK_HOME')}
        os.environ.update({'PROCURATOR_TEST_PROFILE': '1', 'PROCURATOR_MOCK_HOME': base})
        self.p = Provider()
        self.b = Broker(Path(self.tmp.name) / 'browser.sqlite3', self.p)
        self.b.connectors = connectors.Registry()

    def tearDown(self):
        self.srv.shutdown()
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def cmd(self, device, action, params=None, origin='chat'):
        return self.b.call('home_command', {'device': device, 'action': action, 'params': params or {}, 'origin': origin}, False)

    def approve_last(self):
        line = next(m for m in reversed(self.p.messages) if '/homeapprove' in m)
        pid, h = line.split('/homeapprove ')[1].split('\n')[0].split()
        return self.b.call('home_approve', {'id': pid, 'hash': h}, True)

    # ---------- labels ----------
    def test_k04_presence_and_camera_never_public(self):
        self.home.occupancy = 'away'
        self.b.call('home_status', {}, False)
        for aud in ('public', 'workspace'):
            r = self.b.call('health_taint_check', {'text': 'Reminder: home is empty this weekend', 'destination': None, 'audience': aud}, False)
            self.assertFalse(r['allowed'])
            self.assertIn('presence', r['label_kinds'])
            self.assertNotIn('home is empty', r['reason'])
            r = self.b.call('health_taint_check', {'text': "We're away until Sunday, see you then", 'destination': None, 'audience': aud}, False)
            self.assertFalse(r['allowed'])
        r = self.b.call('health_taint_check', {'text': 'Heads up: home is empty', 'destination': ['friend@example.com'], 'audience': 'person'}, False)
        self.assertFalse(r['allowed'])
        snap = self.b.call('home_snapshot', {'device': 'd_cam'}, False)
        sha = self.b.social_parts()['media'].meta(snap['media'])['sha256']
        r = self.b.call('health_taint_check', {'text': 'Look at this', 'destination': None, 'audience': 'public', 'media': [sha]}, False)
        self.assertFalse(r['allowed'])
        self.assertIn('camera', r['label_kinds'])
        ok = self.b.call('health_taint_check', {'text': 'Nice weather today', 'destination': None, 'audience': 'public'}, False)
        self.assertTrue(ok['allowed'])

    # ---------- physical policy ----------
    def test_k06_k07_comfort_inside_and_outside_the_grant(self):
        self.b.call('home_grant', {'kind': 'thermostat', 'min': 19, 'max': 25}, True)
        r = self.cmd('d_thermo', 'set', {'setpoint': 21})
        self.assertEqual(r['status'], 'confirmed')
        self.assertEqual(self.home.devices['d_thermo']['state']['setpoint'], 21)
        r = self.cmd('d_thermo', 'set', {'setpoint': 27})
        self.assertEqual(r['status'], 'awaiting_approval')
        self.assertEqual(self.home.devices['d_thermo']['state']['setpoint'], 21)
        self.assertEqual(self.approve_last()['status'], 'confirmed')
        r = self.cmd('d_thermo', 'set', {'setpoint': 40})            # outside the manifest bounds too: asks, never runs
        self.assertEqual(r['status'], 'awaiting_approval')

    def test_k08_access_never_from_packets_or_events(self):
        for origin in ('packet', 'schedule', 'event'):
            r = self.cmd('d_lock', 'unlock', origin=origin)
            self.assertEqual(r['status'], 'refused', origin)
        self.assertTrue(self.home.devices['d_lock']['state']['locked'])
        r = self.cmd('d_lock', 'unlock')
        self.assertEqual(r['status'], 'awaiting_approval')
        self.assertTrue(self.home.devices['d_lock']['state']['locked'])
        self.assertEqual(self.approve_last()['status'], 'confirmed')
        self.assertFalse(self.home.devices['d_lock']['state']['locked'])
        line = next(m for m in reversed(self.p.messages) if '/homeapprove' in m)
        pid, h = line.split('/homeapprove ')[1].split('\n')[0].split()
        self.assertTrue(self.b.call('home_approve', {'id': pid, 'hash': h}, True)['already'])
        with self.assertRaises(ValueError):
            self.b.call('home_approve', {'id': pid, 'hash': h}, False)

    def test_k09_smoke_detector_never_disabled(self):
        for action in ('disable', 'mute'):
            r = self.cmd('d_smoke', action)
            self.assertEqual(r['status'], 'refused')
            self.assertTrue(r.get('hard_block'))
        self.assertTrue(self.home.devices['d_smoke']['state']['enabled'])
        self.assertEqual(self.cmd('d_smoke', 'test')['status'], 'awaiting_approval')

    def test_k10_dropped_readback_is_unconfirmed(self):
        self.b.call('home_grant', {'kind': 'light', 'param': 'brightness', 'min': 0, 'max': 80}, True)
        self.home.faults['drop_readback'] = True
        r = self.cmd('d_light', 'set', {'brightness': 40})
        self.assertEqual(r['status'], 'unconfirmed')
        self.assertIn('NOT confirmed', r['note'])
        self.home.faults['drop_readback'] = False
        self.home.faults['unreachable'] = True
        with self.assertRaisesRegex(ValueError, 'did not answer'):
            self.cmd('d_light', 'set', {'brightness': 40})

    def test_k11_homeoff_refuses_everything(self):
        self.b.call('home_grant', {'kind': 'thermostat', 'min': 19, 'max': 25}, True)
        self.b.call('home_off', {}, True)
        for dev, act, par in (('d_thermo', 'set', {'setpoint': 21}), ('d_lock', 'unlock', {}), ('d_light', 'on', {})):
            self.assertEqual(self.cmd(dev, act, par)['status'], 'refused')
        h2 = Broker(Path(self.tmp.name) / 'browser.sqlite3', self.p)        # survives a restart
        self.assertEqual(h2.call('home_command', {'device': 'd_thermo', 'action': 'set', 'params': {'setpoint': 21}, 'origin': 'chat'}, False)['status'], 'refused')
        self.b.call('home_on', {}, True)
        self.assertEqual(self.cmd('d_thermo', 'set', {'setpoint': 21})['status'], 'confirmed')

    def test_device_named_like_an_instruction_stays_fenced(self):
        devs = self.b.call('home_devices', {}, False)['devices']
        trick = next(d for d in devs if d['id'] == 'd_trick')
        self.assertTrue(trick['name'].startswith('<untrusted source="device">'))
        self.assertTrue(self.home.devices['d_lock']['state']['locked'])
        reg = self.b.call('devices_list', {}, False)
        self.assertTrue(any(d['kind'] == 'home_device' for d in reg['devices']) or reg['counts'])

    # ---------- live ----------
    def test_k12_k13_live_rules(self):
        rules = self.b.live_rules()
        for sel in ('input[type=password]', 'input[autocomplete^=cc-]', 'input[autocomplete=one-time-code]'):
            self.assertIn(sel, rules['mask_selectors'])
        self.assertEqual(site_policy.view('accounts.example.com', '/login')['view'], 'mask')
        self.assertIn('chase.com', rules['hide_domains'])
        self.assertIn('mychart', rules['hide_hints'])
        lv = live.Live()
        lv.frame('s1', 'viewer')
        with self.assertRaisesRegex(ValueError, 'hidden from Live'):
            lv.push('s1', jpeg=b'\xff\xd8\xff' + b'x' * 10, view='hide')
        self.assertTrue(lv.push('s1', placeholder={'domain': 'mychart.example.org', 'title': 'x', 'reason': 'health portal'}, view='hide')['stored'])
        self.assertIsNone(lv.frame('s1', 'viewer')['jpeg'])

    def test_k14_k15_capture_only_while_watched_and_never_on_disk(self):
        t = [1000.0]
        lv = live.Live(now=lambda: t[0])
        self.assertFalse(lv.wanted('s1')['capture'])
        self.assertFalse(lv.push('s1', jpeg=b'\xff\xd8\xff' + b'1' * 20)['stored'])
        lv.frame('s1', 'v1')
        self.assertTrue(lv.wanted('s1')['capture'])
        self.assertTrue(lv.push('s1', jpeg=b'\xff\xd8\xff' + b'2' * 20)['stored'])
        t[0] += 5.5
        self.assertFalse(lv.wanted('s1')['capture'])
        lv.end('s1')
        for p in Path(self.tmp.name).rglob('*'):
            if p.is_file():
                self.assertNotIn(b'\xff\xd8\xff' + b'2' * 20, p.read_bytes(), p)

    def test_k16_no_approval_endpoint_in_the_api(self):
        import dashboard
        src = Path(dashboard.__file__).read_text()
        for action in ("'send'", "'social_post_send'", "'home_approve'", "'trash'", "'cal_approve'", "'sms_send'", "'execute'", "'live_reveal_confirm'"):
            self.assertNotIn("call(%s" % action, src.replace(' ', ''), action)
        dash, _srv = dashboard.start(self.b, self.tmp.name, self.tmp.name, port=0, sample=False)
        for path in ('home/approve', 'social/post/send', 'mail/send', 'approve', 'live/s1/reveal/confirm'):
            with self.assertRaisesRegex(ValueError, 'Not found'):
                dash.api('POST', path, {}, 'sid')
        _srv.shutdown()

    # ---------- stream ----------
    def test_stream_summaries_withhold_labeled_details(self):
        self.home.occupancy = 'away'
        self.b.call('home_status', {}, False)
        self.b.stream.emit('web', 'tool', 'note: home is empty')
        self.b.stream.emit('web', 'browser_nav', 'browser open example.com')
        rec = self.b.stream.recent(5)
        self.assertEqual(rec[-2]['summary'], 'tool (presence details withheld)')
        self.assertEqual(rec[-1]['summary'], 'browser open example.com')
        self.assertEqual(self.b.stream.since(rec[-2]['id'])[0]['id'], rec[-1]['id'])

    # ---------- library ----------
    def lib_add(self, data, name, **a):
        return self.b.call('library_add', {'data_b64': base64.b64encode(data).decode(), 'name': name, **a}, True)

    def test_library_chat_exports_and_scoping(self):
        g = self.lib_add(json.dumps(CHATGPT).encode(), 'conversations.json', collection='zenithiot')
        self.assertEqual((g['kind'], g['parts']), ('chat', 1))
        self.assertIn('ChatGPT', g['note'])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('export/conversations.json', json.dumps(CLAUDE))
        c = self.lib_add(buf.getvalue(), 'claude-export.zip', collection='acme-engineering', use_chat='0')
        self.assertIn('Claude', c['note'])
        hits = self.b.call('library_search', {'query': 'starter tier technician'}, False)['hits']
        self.assertEqual(hits[0]['item'], g['id'])
        self.assertEqual(self.b.call('library_search', {'query': 'Skretting tender'}, False)['hits'], [])    # not shared with chat
        r = self.b.call('library_read', {'item': g['id']}, False)
        self.assertTrue(r['text'].startswith('<untrusted source="library:'))
        self.assertIn('Human: What should the CMMS starter tier cost?', r['text'])
        with self.assertRaisesRegex(ValueError, 'not shared'):
            self.b.call('library_read', {'item': c['id']}, False)
        # A packet that names the acme-engineering collection reads it; the other collection is out of its scope.
        link = self.b.missions.call('mission_upload_link', {'return_link': True}, True)['link']
        start = '# Tender packet\n\n**Kind:** administrative\n**Goal:** Prepare the tender.\n**References:** acme-engineering\n**Triggers:** mock-social.comment\n**Done when:**\n- outline saved\n'
        self.b.missions.call('mission_upload', {'token': link.rsplit('/', 1)[1], 'files': {'00_START_HERE.md': start}, 'mission': 'tender', 'activate': True}, True)
        ph = self.b.call('library_search', {'query': 'Skretting tender', 'scope': 'packets', 'packet': 'tender'}, False)['hits']
        self.assertEqual(ph[0]['item'], c['id'])
        self.assertEqual(self.b.call('library_search', {'query': 'starter tier', 'scope': 'packets', 'packet': 'tender'}, False)['hits'], [])
        mg = self.b.missions.call('mission_get', {'mission': 'tender'}, False)
        self.assertEqual(mg.get('references'), ['acme-engineering'])
        self.assertEqual(self.b.missions.triggered('mock-social.comment'), ['tender'])
        # Only the owner adds or deletes.
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('library_add', {'data_b64': base64.b64encode(b'x').decode(), 'name': 'x.txt'}, False)
        self.b.call('library_delete', {'item': c['id']}, True)
        self.assertFalse((Path(self.tmp.name) / 'library' / 'files' / c['sha256']).exists())

    def test_library_notes_pinning_and_limits(self):
        n = self.b.call('library_note', {'text': 'Primaris Health Institute: non-profit for AI-assisted primary care workflows. Pilot sites in Q1.',
                                         'title': 'Primaris context', 'collection': 'context', 'pinned': '1'}, True)
        self.assertTrue(n['pinned'])
        ctx = self.b.call('library_pinned', {}, False)['context']
        self.assertIn('Primaris context', ctx)
        self.b.call('library_update', {'item': n['id'], 'use_chat': False}, True)
        self.assertEqual(self.b.call('library_pinned', {}, False)['context'], '')
        with self.assertRaisesRegex(ValueError, 'Unsupported file'):
            self.lib_add(b'\x00\x01\x02\xff\xfe binary', 'blob.bin')
        with self.assertRaisesRegex(ValueError, 'short name'):
            self.lib_add(b'hello', 'a.txt', collection='Bad Name!')
        d = self.lib_add(b'# Notes\n\nplain markdown body', 'notes.md')
        self.assertTrue(self.lib_add(b'# Notes\n\nplain markdown body', 'notes.md')['duplicate'])
        self.assertEqual(d['mime'], 'text/markdown')

    # ---------- Mini App ----------
    def test_miniapp_launch_data(self):
        token, now = '123456:TEST-token-synthetic', 1793000000
        good = miniapp.sign({'auth_date': str(now), 'user': json.dumps({'id': 5550001, 'first_name': 'Test'}), 'query_id': 'AAE1'}, token)
        self.assertEqual(miniapp.verify(good, token, '5550001', now=now + 10)['id'], 5550001)
        with self.assertRaisesRegex(ValueError, 'another Telegram account'):
            miniapp.verify(good, token, '5550002', now=now + 10)
        with self.assertRaisesRegex(ValueError, 'too old'):
            miniapp.verify(good, token, '5550001', now=now + 400)
        with self.assertRaisesRegex(ValueError, 'signature'):
            miniapp.verify(good.replace('5550001', '5550002'), token, '5550002', now=now + 10)
        with self.assertRaisesRegex(ValueError, 'signature'):
            miniapp.verify(good, token + 'x', '5550001', now=now + 10)

    def test_miniapp_session_lasts_12_hours(self):
        import dashboard
        t = [1000.0]
        s = dashboard.Sessions(Path(self.tmp.name) / 'dash.sqlite3', now=lambda: t[0])
        cookie = s.issue('telegram_miniapp', 'Telegram app', miniapp.SESSION_SECONDS)
        self.assertIsNotNone(s.session(cookie))
        self.assertEqual(s.devices()[0]['method'], 'telegram_miniapp')
        t[0] += miniapp.SESSION_SECONDS + 1
        self.assertIsNone(s.session(cookie))
        with self.assertRaises(ValueError):
            s.issue('device_token', 'x', 60)


class ManifestV2(unittest.TestCase):
    def m(self, **tool):
        return {'manifest_version': 2, 'id': 'demo', 'version': '1.0.0', 'title': 'Demo', 'plugin': 'procurator-web', 'auth': {'type': 'none'},
                'tools': {'demo_tool': tool}, 'actions': {'web': ['demo']}}

    def test_physical_and_inbound_rules(self):
        bad = lambda msg, m: self.assertRaisesRegex(connectors.ManifestError, msg, connectors.validate, m)
        bad('declares safety_tier', self.m(risk='own_write', effect='physical', confirm='readback'))
        bad('confirm: readback', self.m(risk='own_write', effect='physical', safety_tier='comfort'))
        bad('only for physical', self.m(risk='read', safety_tier='comfort'))
        bad('min exceeds max', self.m(risk='own_write', effect='physical', safety_tier='comfort', confirm='readback', bounds={'setpoint': {'min': 30, 'max': 10}}))
        bad('window is HH:MM-HH:MM', self.m(risk='own_write', effect='physical', safety_tier='comfort', confirm='readback', bounds={'setpoint': {'window': 'mornings'}}))
        m = self.m(risk='read')
        m['inbound'] = {'types': ['comment'], 'signature': 'carrier-pigeon'}
        bad('inbound.signature', m)
        h = self.m(risk='draft', outbound=True, audience='public', account='account')
        h.update({'class': 'health', 'min_data': ['appointments'], 'fixtures': ['x']})
        bad('cannot reach a public', h)
        ok = connectors.validate(self.m(risk='own_write', effect='physical', safety_tier='comfort', confirm='readback', bounds={'setpoint': {'min': 16, 'max': 28, 'window': '07:00-23:00'}}))
        self.assertEqual(ok['tools']['demo_tool']['safety_tier'], 'comfort')
        pol = connectors.Registry().policy()
        self.assertEqual(pol['tools']['home_command']['effect'], 'physical')
        self.assertEqual(pol['manifest_version'], 2)

    def test_site_policy_social_sites_never(self):
        for host in ('www.tiktok.com', 'linkedin.com', 'instagram.com'):
            self.assertEqual(site_policy.policy(host)['mode'], 'never', host)


if __name__ == '__main__':
    unittest.main()
