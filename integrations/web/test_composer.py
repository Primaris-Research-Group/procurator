"""Packet Composer (web 0.24.0): spec → format-v1 packet, validator + coverage, the model loop with a scripted fake model,
redaction, catalog hygiene, caps, save as draft, preview. No network."""
import json
import os
import tempfile
import unittest
import urllib.error

import packets
from composer import (Catalog, Composer, ComposerError, SESSION_CAP_USD, apply_patch, coverage, next_revision, redact, render, spec_from_files)
from missions import Missions
from model_settings import ModelSettings

TODAY = '2026-11-08'
NOW = 1_794_200_000.0

LOGINS = [{'site': 'leaveco', 'origin': 'https://www.leaveco.example', 'start_url': 'https://www.leaveco.example/login?session=abc123', 'status': 'owner_connected'},
          {'site': 'cvs', 'origin': 'https://www.cvs.com', 'status': 'home_only'}]
BUILDERS = [{'name': 'mac', 'online': True, 'repos': ['example-org/procurator-sandbox'], 'agents': ['claude'], 'vitals': {'mem_total': 1}}]
HEALTH = {'connections': [{'id': 'c0ffee0000000001', 'provider': 'mock_ehr', 'institution': 'Riverside Health (synthetic)', 'environment': 'fake', 'status': 'active',
                           'token_ref': 'secret-ref-should-not-appear'}],
          'grants': [{'id': 'aaaabbbbccccdddd', 'title': 'Derm follow-up', 'connection': 'c0ffee0000000001', 'actions': ['appointment.search', 'appointment.book'],
                      'constraints': {'clinician': 'Rivera'}, 'valid_to': 1800000000}]}
LANES = {'machines': [{'name': 'home', 'online': True}]}

SCENARIOS = {
    'leaveco': {'title': 'Leaveco absence report for Nov 20', 'kind': 'administrative', 'goal': 'Report the Nov 20 intermittent-leave absence on Leaveco and keep the confirmation.',
             'done_when': ['Leaveco shows a confirmation number for Nov 20', 'The confirmation number is in the packet journal'],
             'budget_usd': 5, 'hours': '08:00-20:00', 'sites': ['leaveco'],
             'facts': [{'label': 'Absence date', 'value': '2026-11-20', 'source': 'owner'}, {'label': 'Hours missed', 'value': 'full day (8 hours)', 'source': 'owner'},
                       {'label': 'Reason category', 'source': 'ask'}],
             'steps': [{'type': 'browse', 'title': 'Report the absence in the Leaveco portal', 'site': 'leaveco', 'detail': ['Use the intermittent leave claim', 'Enter the date and hours from Facts'],
                        'evidence': 'Leaveco confirmation number', 'on_fail': 'If the portal is down or asks for something not in Facts, ask me; do not call Leaveco.'},
                       {'type': 'wait', 'title': 'Wait for the Leaveco confirmation email', 'who': 'Leaveco (noreply@leaveco.example)', 'when': 'up to 2 days'}],
             'rules': ['Never contact my employer.']},
    'derm': {'title': 'Dermatology follow-up', 'kind': 'health', 'goal': 'Book a routine follow-up with Dr. Rivera on a Tuesday afternoon.',
             'grant': 'aaaabbbbccccdddd', 'connection': 'c0ffee0000000001', 'budget_usd': 5,
             'done_when': ['The portal shows the booked visit', 'The visit is on my calendar'],
             'steps': [{'type': 'appointment', 'title': 'Find and book a Tuesday afternoon follow-up', 'on_fail': 'If nothing fits in 6 weeks, ask me; never take a waitlist.'},
                       {'type': 'calendar', 'title': 'Put the confirmed visit on my calendar', 'when': 'the booked time', 'on_fail': 'Tell me the time so I add it.'}]},
    'refill': {'title': 'Atorvastatin refill', 'kind': 'health', 'goal': 'Request the Atorvastatin 20 mg refill for pickup at Midtown.',
               'grant': 'aaaabbbbccccdddd', 'connection': 'c0ffee0000000001', 'budget_usd': 3, 'done_when': ['The pharmacy shows the refill ready'],
               'steps': [{'type': 'refill', 'title': 'Request the refill of the exact prescription', 'on_fail': 'If it needs a renewal, tell me; do not contact the prescriber.'},
                         {'type': 'wait', 'title': 'Wait until the pharmacy says ready', 'when': 'up to 3 days'}]},
    'legal': {'title': 'Records request letter', 'kind': 'legal', 'goal': 'Draft a records request to the former employer and research the response deadline.',
              'done_when': ['A draft letter is in Drive', 'The response deadline is noted with its source'], 'budget_usd': 15,
              'deliverables': ['letter draft in Drive', 'summary in Telegram'], 'context': 'Background the owner pasted.\n## Not a heading\nSecond line.',
              'steps': [{'type': 'research', 'title': 'Find the program policy on records requests'},
                        {'type': 'write', 'title': 'Draft the request letter', 'evidence': 'Drive link'},
                        {'type': 'mail', 'title': 'Prepare the email to the program coordinator for my review', 'who': 'program coordinator (address from the program site)',
                         'on_fail': 'If no address is published, ask me.'}]},
    'coding': {'title': 'Sandbox README badge', 'kind': 'coding', 'goal': 'Add a test-status badge to the sandbox README.', 'repo': 'example-org/procurator-sandbox',
               'builder': 'mac', 'agent': 'claude', 'budget_usd': 10, 'done_when': ['The draft PR adds the badge', 'Tests pass'],
               'steps': [{'type': 'build', 'title': 'Add the badge and open a draft PR', 'on_fail': 'If tests fail, report the failure; do not merge.'}]},
    'research': {'title': 'Home battery options', 'kind': 'research', 'goal': 'Compare three home battery systems for a 2,000 sq ft house in Florida.', 'budget_usd': 8,
                 'deliverables': ['memo in Drive'], 'done_when': ['A memo comparing three systems with sources is in Drive'],
                 'steps': [{'type': 'research', 'title': 'Collect prices, capacity and warranties for three systems'},
                           {'type': 'write', 'title': 'Write the comparison memo', 'evidence': 'Drive link'}]},
}


class FakeModel:
    """Returns scripted API responses: each item is a list of (tool, input) or a plain string."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, key, system, messages, tools, model=None):
        self.calls.append({'system': system, 'messages': json.loads(json.dumps(messages)), 'tools': [t['name'] for t in tools], 'model': model, 'key': key})
        item = self.script.pop(0) if self.script else [('reply', {'message': 'ok', 'status': 'collecting'})]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            content = [{'type': 'text', 'text': item}]
        else:
            content = [{'type': 'tool_use', 'id': 't%d' % i, 'name': n, 'input': inp} for i, (n, inp) in enumerate(item)]
        return {'content': content, 'usage': {'input_tokens': 5000, 'output_tokens': 400}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [NOW]
        now = lambda: self.clock[0]
        self.missions = Missions(os.path.join(self.tmp.name, 'missions.sqlite3'), None, now=now,
                                 checks=lambda: {'saved_logins': ['leaveco', 'cvs'], 'builders': BUILDERS})
        self.catalog = Catalog(logins=lambda: LOGINS, builders=lambda: BUILDERS, health=lambda: HEALTH, lanes=lambda: LANES, now=now)
        self.model = FakeModel([])
        self.c = Composer(os.path.join(self.tmp.name, 'composer.sqlite3'), missions=self.missions, catalog=self.catalog, key_reader=lambda: 'test-key',
                          caller=self.model, checks=self.missions.checks, now=now, background=False)

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, sid, text, *script):
        self.model.script = list(script)
        return self.c.message(sid, text)


class RenderTests(Base):
    def test_every_golden_scenario_validates_clean_round_trips_and_has_no_missing_gaps(self):
        cat = self.catalog.build()
        for name, spec in SCENARIOS.items():
            with self.subTest(name):
                s, errs = apply_patch({}, spec)
                self.assertEqual(errs, [])
                files = render(s, TODAY)
                v = packets.validate(files, saved_logins=['leaveco', 'cvs'], builders=BUILDERS)
                self.assertEqual(v['errors'], [], files['00-start.md'])
                p = v['packet']
                self.assertEqual((p['kind'], p['kind_source'], p['goal'], p['done_when']), (s['kind'], 'stated', s['goal'], s['done_when']))
                self.assertEqual([(x['type'], x['title']) for x in p['steps']], [(x['type'], x['title']) for x in s['steps']])
                self.assertEqual(p['budget_usd'], s['budget_usd'])
                self.assertEqual([g for g in coverage(s, cat) if g['level'] == 'missing'], [])
                self.assertEqual(packets.credential_scan(files), [])

    def test_front_matter_survives_many_done_when_items_and_odd_text(self):
        s, _ = apply_patch({}, dict(SCENARIOS['leaveco'], done_when=['item %d' % i for i in range(12)], title='## **Bold** title',
                                    goal='Line one\nline two **x**', sites=['leaveco']))
        p = packets.validate(render(s, TODAY), saved_logins=['leaveco'])['packet']
        self.assertEqual(len(p['done_when']), 12); self.assertEqual(p['sites'], ['leaveco']); self.assertEqual(p['budget_usd'], 5)
        self.assertEqual(p['goal'], 'Line one line two x'); self.assertEqual(p['title'], 'Bold title')

    def test_legal_packets_always_carry_the_draft_only_rule_and_context_cannot_add_headings(self):
        s, _ = apply_patch({}, SCENARIOS['legal'])
        text = render(s, TODAY)['00-start.md']
        self.assertIn('never file with a court', text); self.assertNotIn('## Not a heading', text)

    def test_ask_facts_become_questions_and_assumptions_are_flagged(self):
        s, _ = apply_patch({}, dict(SCENARIOS['leaveco'], facts=[{'label': 'Employer', 'value': 'Hospital', 'source': 'assumed'}, {'label': 'Reason', 'source': 'ask'}]))
        text = render(s, TODAY)['00-start.md']
        self.assertIn('Employer: Hospital (assumed', text); self.assertIn('- Ask me: Reason', text)

    def test_patch_rejects_bad_fields_but_keeps_the_good_ones(self):
        s, errs = apply_patch({'title': 'x'}, {'kind': 'shopping', 'budget_usd': 900, 'steps': [{'type': 'teleport', 'title': 'go'}, {'type': 'call', 'title': 'Call the clinic'}],
                                              'goal': 'Fine goal', 'nonsense': 1, 'title': None})
        self.assertEqual(s['goal'], 'Fine goal'); self.assertNotIn('title', s); self.assertNotIn('kind', s)
        self.assertEqual([x['type'] for x in s['steps']], ['call'])
        self.assertEqual(len(errs), 4)


class CoverageTests(Base):
    def test_gaps_name_what_the_orchestrator_would_miss(self):
        cat = self.catalog.build()
        s, _ = apply_patch({}, {'goal': 'g', 'done_when': ['x'], 'kind': 'health', 'sites': ['chase'],
                                'steps': [{'type': 'call', 'title': 'Call the clinic'}, {'type': 'browse', 'title': 'Open the form', 'site': 'irs'},
                                          {'type': 'appointment', 'title': 'Book it', 'on_fail': 'ask me'}]})
        miss = ' | '.join(g['text'] for g in coverage(s, cat) if g['level'] == 'missing')
        for needle in ('"chase", which is not a saved login', 'Step 1 (call): what to do if', 'Step 1 (call): who to call', '"irs" is not a saved login',
                       'Step 3 (appointment): an active health grant', 'cannot create grants', 'health connection id'):
            self.assertIn(needle, miss)

    def test_a_repo_no_builder_allows_is_missing(self):
        s, _ = apply_patch({}, dict(SCENARIOS['coding'], repo='someone/else'))
        self.assertIn('not on any builder', ' '.join(g['text'] for g in coverage(s, self.catalog.build())))


class CatalogTests(Base):
    def test_names_and_ids_only(self):
        blob = json.dumps(self.catalog.build())
        for secret in ('session=abc123', 'secret-ref-should-not-appear', 'mem_total'):
            self.assertNotIn(secret, blob)
        cat = self.catalog.build()
        self.assertEqual(cat['saved_logins'][0], {'name': 'leaveco', 'site': 'www.leaveco.example', 'status': 'owner_connected', 'where': 'cloud browser'})
        self.assertEqual(cat['saved_logins'][1]['where'], 'home PC'); self.assertTrue(cat['home_pc']['online'])
        self.assertIn('browse', cat['step_types']); self.assertIn('/webtask', cat['step_types']['browse']['approval'])

    def test_a_failing_source_does_not_break_the_catalog(self):
        def boom():
            raise RuntimeError('down')
        cat = Catalog(logins=boom, builders=None, health=boom).build()
        self.assertEqual((cat['saved_logins'], cat['builders'], cat['health']['grants']), ([], [], []))


class RedactionTests(Base):
    def test_credentials_are_removed_before_storage_and_the_model(self):
        clean, kinds = redact('my password is hunter2!! and card 4111 1111 1111 1111, ssn 123-45-6789, key sk-ant-' + 'a' * 30)
        self.assertNotIn('hunter2', clean); self.assertNotIn('4111', clean); self.assertNotIn('123-45-6789', clean); self.assertNotIn('sk-ant', clean)
        self.assertEqual(set(kinds), {'password', 'card number', 'Social Security number', 'API key'})
        self.assertEqual(redact('call 404-555-0100 on 2026-11-20')[1], [])
        sid = self.c.new()['id']
        self.say(sid, 'Log into Leaveco, my password is hunter2!!', [('reply', {'message': 'Noted', 'status': 'collecting'})])
        g = self.c.get(sid)
        self.assertNotIn('hunter2', json.dumps(g))
        sent = json.dumps(self.model.calls[-1]['messages'])
        self.assertNotIn('hunter2', sent); self.assertIn('it was removed before you saw it', sent)

    def test_the_model_cannot_put_a_credential_into_the_spec(self):
        sid = self.c.new()['id']
        self.say(sid, 'set it up', [('update_spec', {'patch': {'facts': [{'label': 'Login', 'value': 'password: hunter2xx', 'source': 'owner'}], 'goal': 'g'}})],
                 [('reply', {'message': 'ok', 'status': 'collecting'})])
        self.assertEqual(self.c.get(sid)['spec'], {})
        res = json.loads(self.model.calls[1]['messages'][-1]['content'][0]['content'])
        self.assertFalse(res['applied']); self.assertIn('Credentials never go in packets', res['patch_errors'][-1])


class LoopTests(Base):
    def test_a_full_conversation_builds_a_ready_draft_and_saves_it_as_a_draft_packet(self):
        sid = self.c.new()['id']
        out = self.say(sid, 'I need to report my Nov 20 absence on Leaveco',
                       [('lookup', {'what': 'saved_logins'})],
                       [('update_spec', {'patch': {k: SCENARIOS['leaveco'][k] for k in ('title', 'kind', 'goal', 'sites')}})],
                       [('reply', {'message': 'Got it. How many hours, and what should happen if the portal asks for something else?', 'questions': ['Hours missed?', 'Fallback?'], 'status': 'collecting'})])
        self.assertEqual(out['status'], 'collecting'); self.assertFalse(out['ready'])
        sys0 = self.model.calls[0]['system']
        self.assertIn('CATALOG', sys0); self.assertIn('"name":"leaveco"', sys0); self.assertIn('TODAY (Eastern): 2026-11-08', sys0)
        # The model claims draft_ready too early: the code downgrades it.
        out = self.say(sid, '8 hours; ask me if anything else comes up', [('reply', {'message': 'Ready!', 'status': 'draft_ready'})])
        self.assertEqual(out['status'], 'collecting')
        out = self.say(sid, 'yes', [('update_spec', {'patch': {k: v for k, v in SCENARIOS['leaveco'].items() if k not in ('title', 'kind', 'goal', 'sites')}})],
                       [('reply', {'message': 'The packet is ready to save.', 'status': 'draft_ready'})])
        self.assertEqual((out['status'], out['ready']), ('draft_ready', True))
        g = self.c.get(sid)
        self.assertEqual(len([m for m in g['messages'] if m['role'] == 'owner']), 3)
        self.assertEqual(g['messages'][1]['role'], 'owner')
        self.assertIn('Packet Composer session ' + sid, g['files']['00-start.md'])
        pv = self.c.preview(sid)
        self.assertTrue(pv['ok']); self.assertIn('DRY RUN', pv['text']); self.assertIn('browser tasks (/webtask', pv['text']); self.assertIn('Nothing was done', pv['text'])
        saved = self.c.save(sid)
        self.assertEqual((saved['mission'], saved['status'], saved['kind']), ('leaveco', 'draft', 'administrative'))
        m = self.missions.call('mission_get', {'mission': 'leaveco'}, True)
        self.assertEqual(m['status'], 'draft'); self.assertEqual([s['type'] for s in m['threads'][0]['steps']], ['browse', 'wait'])
        with self.assertRaises(ComposerError):
            self.c.message(sid, 'more')
        # A second new packet with the same name never becomes a revision of the first.
        sid2 = self.c.new()['id']
        self.say(sid2, 'another', [('update_spec', {'patch': SCENARIOS['leaveco']})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        self.assertEqual(self.c.save(sid2)['mission'], 'leaveco-2')
        self.assertEqual(self.missions.call('mission_get', {'mission': 'leaveco'}, True)['revision'], saved['revision'])

    def test_a_health_session_keeps_no_transcript_after_saving(self):
        sid = self.c.new()['id']
        self.say(sid, 'book my derm follow up with Rivera, Tuesday afternoons', [('update_spec', {'patch': SCENARIOS['derm']})],
                 [('reply', {'message': 'Ready.', 'status': 'draft_ready'})])
        out = self.c.save(sid)
        self.assertTrue(out['transcript_removed'])
        g = self.c.get(sid)
        self.assertEqual((g['messages'], g['spec'], g['status']), ([], {}, 'saved'))
        self.assertNotIn('Rivera', json.dumps(self.c.list()))
        files = self.missions.call('mission_get', {'mission': out['mission']}, True)
        self.assertEqual(files['kind'], 'health')

    def test_a_plain_text_answer_ends_the_turn_and_tool_calls_are_capped(self):
        sid = self.c.new()['id']
        out = self.say(sid, 'hi', 'Hello! What do you need done?')
        self.assertEqual(out['reply'], 'Hello! What do you need done?')
        out = self.say(sid, 'loop', *([[('lookup', {'what': 'all'})]] * 20))
        self.assertLessEqual(len(self.model.calls), 2 + 9)
        self.assertIn('updated the draft', out['reply'])

    def test_model_errors_leave_the_draft_unchanged_and_the_session_usable(self):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('update_spec', {'patch': {'goal': 'Keep me'}})], [('reply', {'message': 'ok', 'status': 'collecting'})])
        err = urllib.error.HTTPError('u', 400, 'bad', {}, None)
        out = self.say(sid, 'y', err)
        self.assertIn('answered 400', out['reply']); self.assertEqual(self.c.get(sid)['spec']['goal'], 'Keep me'); self.assertFalse(self.c.get(sid)['busy'])
        self.say(sid, 'z', urllib.error.URLError('down'))
        self.assertIn('could not reach', self.c.get(sid)['messages'][-1]['text'])

    def test_no_key_and_the_session_cap(self):
        self.c.key_reader = lambda: ''
        sid = self.c.new()['id']
        self.assertEqual(self.c.message(sid, 'hi')['error'], 'no_key')
        self.c.key_reader = lambda: 'k'
        with self.c.db() as db:
            db.execute('UPDATE sessions SET cost_usd=? WHERE id=?', (SESSION_CAP_USD, sid))
        with self.assertRaisesRegex(ComposerError, 'model limit'):
            self.c.message(sid, 'more')

    def test_spend_is_recorded_per_day(self):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('lookup', {'what': 'health'})], [('reply', {'message': 'ok', 'status': 'collecting'})])
        d = self.c.daily(7)[TODAY]
        self.assertEqual(d['calls'], 2); self.assertAlmostEqual(d['cost'], 2 * (5000 * 2 + 400 * 10) / 1e6)
        self.assertAlmostEqual(self.c.get(sid)['cost_usd'], d['cost'])

    def test_save_refuses_a_failing_draft_and_owner_only_actions(self):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('update_spec', {'patch': {'goal': 'g', 'sites': ['chase'], 'kind': 'administrative'}})], [('reply', {'message': 'ok', 'status': 'collecting'})])
        with self.assertRaisesRegex(ComposerError, 'does not pass'):
            self.c.save(sid)
        with self.assertRaises(ComposerError):
            self.c.call('composer_new', {}, privileged=False)
        self.assertEqual(self.c.call('composer_discard', {'id': sid}, True)['status'], 'discarded')
        self.assertEqual(self.c.get(sid)['messages'], [])

    def test_revising_an_existing_packet_makes_the_next_revision(self):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('update_spec', {'patch': SCENARIOS['research']})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        first = self.c.save(sid)
        sid2 = self.c.new(from_mission=first['mission'])['id']
        self.say(sid2, 'add a fourth system', [('update_spec', {'patch': dict(SCENARIOS['research'], goal='Compare four home battery systems.')})],
                 [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        second = self.c.save(sid2)
        self.assertEqual(second['mission'], first['mission']); self.assertTrue(second['revision'].endswith('-r2'))
        self.assertEqual(self.missions.call('mission_get', {'mission': first['mission']}, True)['goal'], 'Compare four home battery systems.')

    def test_a_seeded_session_starts_with_the_first_turn_and_old_sessions_expire(self):
        self.model.script = [[('reply', {'message': 'Which date?', 'status': 'collecting'})]]
        out = self.c.new(seed='Report my absence on Leaveco')
        self.assertEqual(out['started']['reply'], 'Which date?')
        self.clock[0] += 31 * 86400
        self.c.new()
        self.assertEqual(len(self.c.list()['sessions']), 1)


class FakeOpenAI:
    """Responses API shape: function_call items, then a message."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, key, body):
        self.calls.append(json.loads(json.dumps(body)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            out = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': item}]}]
        else:
            out = [{'type': 'reasoning', 'id': 'rs1', 'summary': []}] + [{'type': 'function_call', 'call_id': 'c%d' % i, 'name': n, 'arguments': json.dumps(a)} for i, (n, a) in enumerate(item)]
        return {'output': out, 'usage': {'input_tokens': 6000, 'output_tokens': 300, 'input_tokens_details': {'cached_tokens': 1000}}}


class ModelChoiceTests(Base):
    def setUp(self):
        super().setUp()
        self.settings = ModelSettings(os.path.join(self.tmp.name, 'model-settings.json'))
        self.main = ['openai/gpt-6.1-terra']
        self.oa = FakeOpenAI([])
        self.c.settings, self.c.main_model = self.settings, lambda: self.main[0]
        self.c.openai_reader, self.c.openai_caller = (lambda: 'oa-key'), self.oa

    def test_default_is_sonnet_and_the_owner_default_applies_to_new_replies(self):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('reply', {'message': 'ok', 'status': 'collecting'})])
        self.assertEqual(self.model.calls[-1]['model'], 'claude-sonnet-5-5')
        self.settings.set('composer', 'opus')
        self.say(sid, 'y', [('reply', {'message': 'ok', 'status': 'collecting'})])
        self.assertEqual(self.model.calls[-1]['model'], 'claude-opus-5-5')
        g = self.c.get(sid)
        self.assertEqual((g['model'], g['model_choice'], g['model_why']), ('opus', 'default', 'your Composer default'))
        with self.c.db() as db:
            rows = db.execute('SELECT model,cost FROM spend ORDER BY at').fetchall()
        self.assertEqual([r['model'] for r in rows], ['sonnet', 'opus'])
        self.assertAlmostEqual(rows[1]['cost'], (5000 * 4 + 400 * 20) / 1e6)      # Opus price

    def test_a_session_on_gpt_uses_the_responses_api_with_function_calls(self):
        sid = self.c.new()['id']
        self.assertEqual(self.c.set_model(sid, 'terra')['model_label'], 'GPT-6.1 Terra')
        self.oa.script = [[('update_spec', {'patch': SCENARIOS['research']})], [('reply', {'message': 'Ready to save.', 'status': 'draft_ready'})]]
        out = self.c.message(sid, 'compare home batteries')
        self.assertEqual((out['status'], out['ready'], out['model']), ('draft_ready', True, 'terra'))
        first, second = self.oa.calls
        self.assertEqual(first['model'], 'gpt-6.1-terra'); self.assertIn('CATALOG', first['instructions'])
        self.assertEqual([t['name'] for t in first['tools']], ['lookup', 'update_spec', 'read_attachment', 'reply']); self.assertEqual(first['tools'][0]['type'], 'function')
        self.assertEqual(first['input'][-1], {'role': 'user', 'content': 'compare home batteries'})
        kinds = [i.get('type') for i in second['input'] if isinstance(i, dict) and i.get('type')]
        self.assertEqual(kinds[-3:], ['reasoning', 'function_call', 'function_call_output'])
        self.assertEqual(self.model.calls, [])                        # Claude was not called
        self.assertAlmostEqual(self.c.get(sid)['cost_usd'], 2 * (5000 * 1 + 1000 * 0.1 + 300 * 5) / 1e6)

    def test_main_follows_the_chat_model_and_falls_back_to_sonnet(self):
        sid = self.c.new()['id']
        self.c.set_model(sid, 'main')
        self.oa.script = ['Hi! What do you need?']
        self.assertEqual(self.c.message(sid, 'hello')['model'], 'terra')
        self.main[0] = 'anthropic/claude-opus-5-5'
        self.say(sid, 'x', [('reply', {'message': 'ok', 'status': 'collecting'})])
        self.assertEqual(self.model.calls[-1]['model'], 'claude-opus-5-5')
        self.main[0] = 'somevendor/unknown'
        g = self.c.get(sid)
        self.assertEqual(g['model'], 'sonnet'); self.assertIn('cannot run', g['model_why'])
        self.assertEqual(self.c.set_model(sid, 'default')['model_choice'], 'default')
        with self.assertRaises(ComposerError):
            self.c.set_model(sid, 'gpt4')

    def test_a_gpt_session_without_an_openai_key_says_so(self):
        self.c.openai_reader = lambda: None
        sid = self.c.new()['id']
        self.c.set_model(sid, 'sol')
        self.assertEqual(self.c.message(sid, 'x')['error'], 'no_key')
        self.assertIn('no OpenAI API key', self.c.get(sid)['messages'][-1]['text'])

    def test_settings_validation_and_packet_default_reaches_due_work_sessions(self):
        with self.assertRaises(ValueError):
            self.settings.set('composer', 'gpt4')
        with self.assertRaises(ValueError):
            self.settings.set('chat', 'opus')
        self.assertEqual(self.settings.all(), {'composer': 'sonnet', 'packets': 'main'})
        sid = self.c.new()['id']
        self.say(sid, 'x', [('update_spec', {'patch': SCENARIOS['research']})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        slug = self.c.save(sid)['mission']
        self.missions.call('mission_owner', {'mission': slug, 'action': 'activate'}, True)
        self.missions.default_model = lambda: self.settings.get('packets')
        due = self.missions.call('mission_due', {})['due']
        self.assertEqual((due[0]['model'], due[0]['model_source']), (None, 'main'))
        self.settings.set('packets', 'sonnet')
        with self.missions.db() as db:
            db.execute('UPDATE missions SET next_run_at=0,lease_until=NULL WHERE slug=?', (slug,))
        due = self.missions.call('mission_due', {})['due']
        self.assertEqual((due[0]['model'], due[0]['model_source']), ('sonnet', 'default'))
        self.missions.call('mission_owner', {'mission': slug, 'action': 'model', 'model': 'opus'}, True)
        with self.missions.db() as db:
            db.execute('UPDATE missions SET next_run_at=0,lease_until=NULL WHERE slug=?', (slug,))
        due = self.missions.call('mission_due', {})['due']
        self.assertEqual((due[0]['model'], due[0]['model_source']), ('opus', 'packet'))


HAND_START = """# legal matter — New Jersey
**Revision:** BG-NJ-2026-11-06-r1
**Owner:** The owner
**Case:** synthetic-001
**Budget:** $20

Background paragraph the owner wrote before any section.

## Mission
Prepare and file-ready a grievance packet for review; nothing is filed by the assistant.

## Definition of success
- The draft grievance is in Drive
- Every exhibit is listed with its source

## Journals
| Journal | Steps |
|---|---|
| BG-NJ-r1 | collect exhibits; draft the narrative; check the rules |

## Rules
- Never contact the respondent.
"""


class ReviseTests(Base):
    def test_composer_packets_round_trip_exactly(self):
        for name, spec in SCENARIOS.items():
            with self.subTest(name):
                s, _ = apply_patch({}, spec)
                files = render(s, TODAY)
                back = spec_from_files(files)
                self.assertEqual(render(back, TODAY), files)

    def test_a_hand_written_long_packet_keeps_everything_it_does_not_manage(self):
        files = {'00-START-HERE.md': HAND_START, '01-exhibits.md': '# Exhibits\n- A: letter of 2026-08-01\n', '02-timeline.md': '# Timeline\n2026-08 events\n'}
        spec = spec_from_files(files)
        self.assertEqual(spec['start_name'], '00-START-HERE.md'); self.assertEqual(set(spec['files_extra']), {'01-exhibits.md', '02-timeline.md'})
        self.assertEqual(spec['front_extra'], {'owner': 'The owner', 'case': 'synthetic-001'}); self.assertEqual(spec['rules'], ['Never contact the respondent.'])
        again = render(spec, TODAY)
        self.assertEqual(again['01-exhibits.md'], files['01-exhibits.md']); self.assertEqual(again['02-timeline.md'], files['02-timeline.md'])
        text = again['00-START-HERE.md']
        for kept in ('**Owner:** The owner', '**Case:** synthetic-001', 'Background paragraph', '## Mission', '## Journals', '| BG-NJ-r1 | collect exhibits'):
            self.assertIn(kept, text)
        a, b = packets.validate(files)['packet'], packets.validate(again)['packet']
        self.assertEqual((b['goal'], b['done_when'], b['budget_usd']), (a['goal'], a['done_when'], a['budget_usd']))
        self.assertEqual([x['steps'] for x in b['threads']], [x['steps'] for x in a['threads']])
        self.assertEqual(b['cadence'], a['cadence'])            # an inferred kind keeps its timing

    def test_next_revision(self):
        self.assertEqual(next_revision('LEAVECO-2026-11-06-r1'), 'LEAVECO-2026-11-06-r2')
        self.assertEqual(next_revision('BG-NJ-r9'), 'BG-NJ-r10'); self.assertEqual(next_revision('ALPHA'), 'ALPHA-r2')

    def saved(self, scenario='research'):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('update_spec', {'patch': SCENARIOS[scenario]})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        return self.c.save(sid)

    def test_revise_loads_the_packet_shows_the_diff_and_saves_the_next_revision(self):
        first = self.saved()
        sid = self.c.new(from_mission=first['mission'])['id']
        g = self.c.get(sid)
        self.assertIn('I loaded packet %s (revision %s)' % (first['mission'], first['revision']), g['messages'][0]['text'])
        self.assertEqual(g['spec']['goal'], SCENARIOS['research']['goal']); self.assertFalse(g['changed'])
        with self.assertRaisesRegex(ComposerError, 'Nothing changed'):
            self.c.save(sid)
        self.say(sid, 'add a fourth system', [('update_spec', {'patch': {'goal': 'Compare four home battery systems.'}})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        g = self.c.get(sid)
        self.assertTrue(g['changed']); self.assertEqual(g['changed_files'], [{'file': '00-start.md', 'change': 'changed'}])
        self.assertIn('-**Goal:** Compare three', g['diff']); self.assertIn('+**Goal:** Compare four', g['diff'])
        self.assertIn('+**Revision:** ' + next_revision(first['revision']), g['diff'])
        sys_prompt = self.model.calls[-1]['system']
        self.assertIn('REVISING packet ' + first['mission'], sys_prompt)
        out = self.c.save(sid)
        self.assertEqual(out['revision'], next_revision(first['revision']))

    def test_a_revision_saved_meanwhile_blocks_an_old_session(self):
        first = self.saved()
        sid = self.c.new(from_mission=first['mission'])['id']
        sid2 = self.c.new(from_mission=first['mission'])['id']
        for s_ in (sid, sid2):
            self.say(s_, 'change', [('update_spec', {'patch': {'budget_usd': 9}})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        self.c.save(sid2)
        with self.assertRaisesRegex(ComposerError, 'changed .* since this session loaded'):
            self.c.save(sid)

    def test_revising_a_hand_written_packet_keeps_its_other_files(self):
        files = {'00-START-HERE.md': HAND_START, '01-exhibits.md': '# Exhibits\n- A: letter\n'}
        self.missions.save_packet('bg', files, 'owner_upload')
        sid = self.c.new(from_mission='bg')['id']
        self.assertIn('with 1 more file kept as they are', self.c.get(sid)['messages'][0]['text'])
        self.say(sid, 'raise the budget to 25', [('update_spec', {'patch': {'budget_usd': 25}})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        out = self.c.save(sid)
        rev, now = self.missions.current_files('bg')
        self.assertEqual((rev, out['revision']), ('BG-NJ-2026-11-06-r2', 'BG-NJ-2026-11-06-r2'))
        self.assertEqual(now['01-exhibits.md'], files['01-exhibits.md']); self.assertIn('**Budget:** $25', now['00-START-HERE.md'])
        self.assertIn('| BG-NJ-r1 | collect exhibits', now['00-START-HERE.md'])


class AttachmentTests(Base):
    def test_attach_read_detach_and_credentials_are_refused(self):
        sid = self.c.new()['id']
        with self.assertRaisesRegex(ComposerError, 'line 2 looks like it contains a password'):
            self.c.attach(sid, 'notes.md', 'hello\nmy password is hunter2!!')
        with self.assertRaisesRegex(ComposerError, 'File names'):
            self.c.attach(sid, '../etc/passwd', 'x')
        out = self.c.attach(sid, 'policy.md', '# Records policy\n' + 'Requests are answered within 30 days. ' * 400)
        self.assertEqual(out['attachments'], ['policy.md'])
        self.say(sid, 'use the policy', [('read_attachment', {'name': 'policy.md', 'offset': 6000})],
                 [('update_spec', {'patch': SCENARIOS['legal']})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        sent = self.model.calls[0]['system']
        self.assertIn('ATTACHED FILES', sent); self.assertIn('policy.md (', sent)
        res = json.loads(self.model.calls[1]['messages'][-1]['content'][0]['content'])
        self.assertEqual((res['offset'], res['next_offset']), (6000, 12000))
        g = self.c.get(sid)
        self.assertIn('policy.md', g['files']); self.assertIn('## Read these files\n- policy.md', g['files']['00-start.md'])
        self.assertEqual(g['attachments'], [{'name': 'policy.md', 'chars': len(g['files']['policy.md'])}])
        self.assertEqual(packets.validate(g['files'])['errors'], [])
        out = self.c.save(sid)
        _, files = self.missions.current_files(out['mission'])
        self.assertIn('policy.md', files)
        sid2 = self.c.new()['id']
        self.c.attach(sid2, 'a.md', 'text')
        self.assertEqual(self.c.detach(sid2, 'a.md')['attachments'], [])

    def test_the_assistant_attaches_only_to_its_own_recent_session(self):
        own = self.c.new()['id']
        with self.assertRaisesRegex(ComposerError, 'only attach files to a Composer session it opened'):
            self.c.attach(own, 'doc.md', 'text', by_assistant=True)
        mine = self.c.new(origin='assistant')['id']
        self.assertEqual(self.c.attach(mine, 'doc.md', 'Policy text', by_assistant=True)['attached'], 'doc.md')
        self.assertIn('from your assistant', self.c.get(mine)['messages'][-1]['text'])
        self.clock[0] += 3601
        with self.assertRaisesRegex(ComposerError, 'last hour'):
            self.c.attach(mine, 'doc2.md', 'more', by_assistant=True)

    def test_preview_lists_what_he_will_be_asked_to_approve(self):
        sid = self.c.new()['id']
        self.say(sid, 'x', [('update_spec', {'patch': SCENARIOS['leaveco']})], [('reply', {'message': 'ok', 'status': 'draft_ready'})])
        pv = self.c.preview(sid)
        self.assertEqual(len(pv['approvals']), 1); self.assertIn('Step 1 (browse)', pv['approvals'][0]); self.assertIn('/webtask', pv['approvals'][0])


class BenchTests(Base):
    def test_a_scripted_bench_run_grades_pass_warn_and_fail(self):
        import composer_bench as CB
        persona = dict(CB.PERSONAS[0])
        good = [[('update_spec', {'patch': SCENARIOS['leaveco']})], [('reply', {'message': 'Ready.', 'status': 'draft_ready'})]]
        owner_lines = []
        def owner(key, p, transcript):
            owner_lines.append(transcript[-1][1])
            return 'It is 2026-11-20, 8 hours.', 0.001
        def make():
            self.model.script = list(good)
            return self.c
        r = CB.run_persona(persona, make, 'k', owner)
        self.assertEqual((r['verdict'], r['turns'], r['facts']), ('PASS', 1, '2/2'))
        # Not ready after the turn limit → FAIL; ready but a needed step type is missing → WARN.
        def make_slow():
            self.model.script = [[('reply', {'message': 'Which date?', 'questions': ['Date?'], 'status': 'collecting'})]] * 10
            return self.c
        r = CB.run_persona(persona, make_slow, 'k', owner)
        self.assertEqual((r['verdict'], r['turns']), ('FAIL', CB.MAX_TURNS)); self.assertIn('not ready', r['why']); self.assertEqual(r['questions'], CB.MAX_TURNS)
        warn = dict(persona, expect_types=['call'])
        r = CB.run_persona(warn, make, 'k', owner)
        self.assertEqual(r['verdict'], 'WARN'); self.assertIn('step types missing: call', r['why'])
        text = CB.report([r], 'Claude Sonnet 5.5', 0)
        self.assertIn('WARN P1-leaveco', text)

    def test_owner_simulator_sees_the_conversation_from_the_owner_side(self):
        import composer_bench as CB
        seen = {}
        def fake_urlopen(req, timeout=None):
            seen['body'] = json.loads(req.data)
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return json.dumps({'content': [{'type': 'text', 'text': 'SAVE'}], 'usage': {'input_tokens': 100, 'output_tokens': 5}}).encode()
            return R()
        orig = CB.urllib.request.urlopen
        CB.urllib.request.urlopen = fake_urlopen
        try:
            text, cost = CB.owner_call('k', CB.PERSONAS[0], [('owner', 'I need to report'), ('assistant', 'Which date?')])
        finally:
            CB.urllib.request.urlopen = orig
        self.assertEqual(text, 'SAVE'); self.assertAlmostEqual(cost, (100 * 1 + 5 * 5) / 1e6)
        self.assertEqual([m['role'] for m in seen['body']['messages']], ['user'])     # the first owner line is the opening; the Composer's reply is the user turn
        self.assertIn('2026-11-20', seen['body']['system']); self.assertEqual(seen['body']['model'], 'claude-haiku-5-5')


    def test_an_empty_owner_answer_is_retried_then_replaced_never_sent_as_an_empty_message(self):
        import composer_bench as CB
        answers, sent = [[], [{'type': 'text', 'text': 'The 20th, 8 hours.'}]], []
        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data)['max_tokens'])
            content = answers.pop(0) if answers else []
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return json.dumps({'content': content, 'usage': {'input_tokens': 10, 'output_tokens': 300}}).encode()
            return R()
        orig = CB.urllib.request.urlopen
        CB.urllib.request.urlopen = fake_urlopen
        try:
            text, cost = CB.owner_call('k', CB.PERSONAS[0], [('owner', 'hi'), ('assistant', 'Which date?')])
            self.assertEqual((text, sent), ('The 20th, 8 hours.', [1024, 2048])); self.assertGreater(cost, 0)
            answers.clear(); sent.clear()
            text, _ = CB.owner_call('k', CB.PERSONAS[0], [('owner', 'hi'), ('assistant', 'Which date?')])
            self.assertEqual((text, sent), (CB.OWNER_FALLBACK, [1024, 2048]))
        finally:
            CB.urllib.request.urlopen = orig

    def test_ready_with_open_questions_keeps_talking_and_errors_say_what_and_when(self):
        import composer_bench as CB
        persona = dict(CB.PERSONAS[0])
        asked = [('update_spec', {'patch': SCENARIOS['leaveco']})], [('reply', {'message': 'Almost.', 'questions': ['Which date?'], 'status': 'draft_ready'})]
        done = [('reply', {'message': 'Ready.', 'status': 'draft_ready'})],
        def make():
            self.model.script = list(asked) + list(done)
            return self.c
        r = CB.run_persona(persona, make, 'k', lambda k, p, t: ('2026-11-20, 8 hours.', 0.001))
        self.assertEqual((r['verdict'], r['turns']), ('PASS', 2))
        g = self.c.get(self.c.list()['sessions'][0]['id'])
        self.assertEqual([m['role'] for m in g['messages']][-4:], ['owner', 'assistant', 'owner', 'assistant'])
        r = CB.run_persona(persona, make, 'k', lambda k, p, t: ('', 0.0))      # an empty owner line becomes the neutral default
        self.assertEqual(r['turns'], 2)
        def boom(k, p, t):
            raise ValueError('owner service down')
        r = CB.run_persona(persona, make, 'k', boom)
        self.assertEqual((r['verdict'], r['turns']), ('FAIL', 1)); self.assertIn('turn 1: ValueError: owner service down', r['why']); self.assertGreater(r['composer_cost'], 0)

    def test_a_reply_that_still_asks_is_never_draft_ready(self):
        sid = self.c.new()['id']
        out = self.say(sid, 'x', [('update_spec', {'patch': SCENARIOS['leaveco']})], [('reply', {'message': 'ok', 'questions': ['Half day or full?'], 'status': 'draft_ready'})])
        self.assertTrue(out['checks_pass']); self.assertFalse(out['ready']); self.assertEqual(out['status'], 'collecting')
        g = self.c.get(sid)                                                # 0.24.3 (B03): the page agrees and Save is refused
        self.assertEqual((g['ready'], g['checks_pass'], g['open_questions']), (False, True, ['Half day or full?']))
        self.assertIn('still asking: Half day or full?', g['save_blocked'])
        with self.assertRaisesRegex(ComposerError, 'still asking'):
            self.c.save(sid)
        out = self.say(sid, 'full', [('reply', {'message': 'Ready.', 'status': 'draft_ready'})])
        self.assertEqual((out['status'], out['ready']), ('draft_ready', True))
        self.assertIsNone(self.c.get(sid)['save_blocked'])

    def test_0243_bench_health_personas_have_synthetic_prerequisites_and_fair_matching(self):
        import composer_bench as CB
        cat = CB.BenchCatalog(Catalog()).build()
        self.assertEqual({g['connection'] for g in cat['health']['grants']}, {c['id'] for c in cat['health']['connections']})
        self.assertTrue(CB.has_fact('404-555-0142', 'Phone: (404) 555 0142')); self.assertTrue(CB.has_fact(('three', '3 systems'), 'Compare 3 systems.'))
        self.assertFalse(CB.has_fact('404-555-0142', 'Phone: 404-555-0199'))
        p2 = next(p for p in CB.PERSONAS if p['id'] == 'P2-dentist-call')
        self.assertIn(('call', 'appointment'), p2['expect_types'])

    def test_0243_a_placeholder_instead_of_a_fact_blocks_save(self):
        from composer import placeholders
        self.assertEqual(placeholders({'00-start.md': '- Who: Landlord (name and email to be confirmed)\n- TBD later'}), ['(name and email to be confirmed)', 'TBD'])
        self.assertEqual(placeholders({'00-start.md': 'Confirm the receipt arrives; to be safe, ask once.'}), [])


if __name__ == '__main__':
    unittest.main()
