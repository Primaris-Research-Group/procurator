"""Packet Composer on the dashboard (web 0.24.0): pages, API under session + CSRF + Origin, save → draft packet + Telegram line,
usage source, /compose link landing on the session, the assistant's mission_compose rate limit."""
import json
import re
import unittest

import dashboard
import test_dashboard as T
from test_composer import FakeModel, SCENARIOS


class ComposeDashboardTests(unittest.TestCase):
    # Reuse the dashboard fixture (setUp, req, login), not its tests.
    req = T.Tests.req
    login = T.Tests.login

    def setUp(self):
        T.Tests.setUp(self)
        self.model = FakeModel([])
        self.b.composer.caller = self.model
        self.b.composer.key_reader = lambda: 'test-key'
        self.b.composer.background = False

    def test_compose_api_requires_session_csrf_and_origin(self):
        self.assertEqual(self.req('/api/compose', {'seed': None})[0], 401)
        self.login()
        good = self.csrf
        self.csrf = 'wrong'
        self.assertEqual(self.req('/api/compose', {})[0], 403)
        self.csrf = good
        self.assertEqual(self.req('/api/compose', {}, headers={'Origin': 'https://evil.example'})[0], 403)
        st, j = self.req('/api/compose', {})
        self.assertEqual(st, 200); self.assertTrue(re.fullmatch(r'[a-f0-9]{16}', j['id']))
        with self.assertRaises(ValueError):
            self.b.call('composer_list', {}, False)

    def test_a_session_from_the_page_to_a_saved_draft(self):
        self.login()
        st, page = self.req('/compose')
        self.assertEqual(st, 200); self.assertIn('New packet', page); self.assertIn('href="/assistant/dashboard/compose" class="on"', page)
        sid = self.req('/api/compose', {})[1]['id']
        st, page = self.req('/compose/' + sid)
        self.assertIn('id="compose" data-id="%s"' % sid, page)
        spec = dict(SCENARIOS['research'])
        self.model.script = [[('update_spec', {'patch': spec})], [('reply', {'message': 'Ready to save.', 'status': 'draft_ready'})]]
        st, j = self.req('/api/compose/%s/message' % sid, {'text': 'compare home batteries; my password is hunter2!!'})
        self.assertEqual(st, 200)
        st, g = self.req('/api/compose/' + sid)
        self.assertTrue(g['ready']); self.assertNotIn('hunter2', json.dumps(g)); self.assertIn('**Kind:** research', g['files']['00-start.md'])
        st, pv = self.req('/api/compose/%s/preview' % sid, {})
        self.assertIn('DRY RUN', pv['text'])
        st, out = self.req('/api/compose/%s/save' % sid, {})
        self.assertEqual((st, out['status']), (200, 'draft'))
        self.assertIn('saved from the Packet Composer as a DRAFT', self.provider.messages[-1])
        st, page = self.req('/packets/' + out['mission'])
        self.assertIn('Revise with the Composer', page)
        st, page = self.req('/compose/' + sid)
        self.assertIn('It was saved as', page)
        st, page = self.req('/usage')
        self.assertIn('Packet Composer', page)
        self.assertGreater(self.dash.usage(14)['by']['composer']['cost'], 0)

    def test_compose_in_telegram_lands_on_the_session_after_sign_in(self):
        out = self.b.call('composer_start', {}, True)
        msg = self.provider.messages[-1]
        self.assertIn('Packet Composer', msg)
        link = re.search(r'https://\S+', msg).group(0)
        token, nxt = re.search(r'/login/([A-Za-z0-9_-]+)\?next=(/compose/[a-f0-9]{16})', link).groups()
        self.assertEqual(nxt, '/compose/' + out['session'])
        st, _, headers = self.req('/login/%s?next=%s' % (token, nxt), raw=True)
        self.assertEqual((st, headers['Location']), (302, dashboard.ROOT + nxt))
        link2 = self.dash.sessions.login_link('/settings')
        self.assertNotIn('next=', link2)

    def test_the_assistant_can_offer_at_most_five_links_a_day(self):
        self.model.script = [[('reply', {'message': 'Which date?', 'status': 'collecting'})]] * 5
        for _ in range(5):
            self.assertTrue(self.b.call('composer_start', {'seed': 'Report my absence'}, False)['sent'])
        with self.assertRaisesRegex(ValueError, 'Five Composer links'):
            self.b.call('composer_start', {'seed': 'again'}, False)

    def test_model_choices_on_settings_and_per_session(self):
        self.login()
        st, page = self.req('/settings')
        self.assertIn('Models for packets and the Composer', page); self.assertIn('name="composer_model"', page)
        self.assertIn('<option value="sonnet" selected>Claude Sonnet 5.5</option>', page)
        st, j = self.req('/api/settings', {'composer_model': 'opus', 'packet_model': 'sonnet'})
        self.assertEqual(st, 200); self.assertIn('Packet Composer model opus', j['message'])
        self.assertEqual(self.b.model_settings.all(), {'composer': 'opus', 'packets': 'sonnet'})
        self.assertEqual(self.b.missions.default_model(), 'sonnet')
        st, j = self.req('/api/settings', {'composer_model': 'gpt4'})
        self.assertEqual(st, 400)
        sid = self.req('/api/compose', {})[1]['id']
        self.assertEqual(self.req('/api/compose/' + sid)[1]['model'], 'opus')
        st, j = self.req('/api/compose/%s/model' % sid, {'model': 'main'})
        self.assertEqual(j['model'], 'terra'); self.assertIn('GPT-6.1 Terra (same as the chat)', j['message'])
        self.assertEqual(self.req('/api/compose/' + sid)[1]['model_choice'], 'main')
        with self.assertRaises(ValueError):
            self.b.call('model_settings', {'set': {'composer': 'sonnet'}}, False)
        self.assertEqual(self.b.call('model_settings', {}, False)['composer']['model'], 'opus')

    def test_attachments_and_revisions_through_the_api(self):
        self.login()
        sid = self.req('/api/compose', {})[1]['id']
        st, page = self.req('/compose/' + sid)
        self.assertIn('id="c-attach"', page); self.assertIn('Changes from the current revision', page)
        st, j = self.req('/api/compose/%s/attach' % sid, {'name': 'notes.md', 'text': 'the password is hunter2!!'})
        self.assertEqual(st, 400); self.assertIn('looks like it contains a password', j['error'])
        st, j = self.req('/api/compose/%s/attach' % sid, {'name': 'notes.md', 'text': '# Notes\nBackground.'})
        self.assertEqual((st, j['attachments']), (200, ['notes.md']))
        self.model.script = [[('update_spec', {'patch': SCENARIOS['research']})], [('reply', {'message': 'ok', 'status': 'draft_ready'})]]
        self.req('/api/compose/%s/message' % sid, {'text': 'compare batteries'})
        saved = self.req('/api/compose/%s/save' % sid, {})[1]
        st, j = self.req('/api/compose', {'from_mission': saved['mission']})
        rid = j['id']
        g = self.req('/api/compose/' + rid)[1]
        self.assertEqual((g['from_mission'], g['changed']), (saved['mission'], False))
        self.assertEqual([a['name'] for a in g['attachments']], ['notes.md'])
        self.assertEqual(self.req('/api/compose/%s/save' % rid, {})[0], 400)
        st, j = self.req('/api/compose/%s/attach' % rid, {'name': 'notes.md', 'remove': True})
        g = self.req('/api/compose/' + rid)[1]
        self.assertTrue(g['changed']); self.assertIn({'file': 'notes.md', 'change': 'removed'}, g['changed_files'])

    def test_the_assistant_can_attach_a_document_to_the_session_it_opened(self):
        sid = self.b.call('composer_start', {}, False)['session']
        out = self.b.call('composer_offer_attach', {'session': sid, 'name': '01-policy.md', 'text': '# Policy\nAnswers within 30 days.'}, False)
        self.assertEqual(out['attachments'], ['01-policy.md'])
        owner_sid = self.b.call('composer_start', {}, True)['session']
        with self.assertRaises(ValueError):
            self.b.call('composer_offer_attach', {'session': owner_sid, 'name': 'x.md', 'text': 'y'}, False)
        with self.assertRaises(ValueError):
            self.b.call('composer_bench', {}, False)


if __name__ == '__main__':
    unittest.main()
