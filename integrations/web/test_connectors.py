"""Connector framework (0.25.0): manifests validate, the gate refuses undeclared actions, packs need a pinned hash, health
connectors carry min_data and fixtures from the core repo, and the plugin policy files match the manifests."""
import glob
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import connectors

HERE = Path(__file__).resolve().parent
PLUGINS = HERE.parent

# Actions plugins call only through the approval socket (owner commands); they are deliberately absent from the manifests.
OWNER_ONLY = {
    'web': set('''activate activity_owner bench_list bench_start composer_bench composer_daily dashboard_link dashboard_logout execute
health_connection_add health_effect_owner_confirm health_grant_approve health_grant_revoke health_portal_authorize health_sync_pair_code
home_profile home_ready home_signin in_progress injection_screen_set lane_set look_budget_set model_settings pause recall_digest
recall_forget recall_set recall_status recall_turn resolve resume site_connect site_disconnect site_ready takeover task_speed
tracing_set viewport_list webpolicy_set health_read_allow health_adult_confirm health_forget_connection mcp_accept health_visit_packet health_visit_note health_coach_settings health_coach_due
social_post_send oauth_begin oauth_complete oauth_disconnect oauth_rename events_route events_status events_receive
home_approve home_grant home_off home_on home_revoke library_delete library_ingest library_status library_add library_note library_update library_refresh
live_reveal_confirm live_reveal_request'''.split()),
    'mail': {'assistant_address_set', 'cal_approve', 'send', 'trash', 'sms_send', 'auto_cc_set'},
}


def base_manifest(**kw):
    m = {'id': 'demo', 'version': '1.0.0', 'title': 'Demo', 'plugin': 'procurator-web', 'auth': {'type': 'none'},
         'tools': {'demo_read': {'risk': 'read'}}, 'actions': {'web': ['demo_action']}}
    m.update(kw)
    return m


class Manifests(unittest.TestCase):
    def test_every_core_manifest_is_valid_and_loaded(self):
        r = connectors.Registry()
        self.assertEqual(r.problems, [])
        self.assertGreaterEqual(len(r.connectors), 20)
        for m in r.connectors.values():
            self.assertEqual(m['source'], 'core')

    def test_health_connectors_name_real_fixtures(self):
        r = connectors.Registry()
        health = [m for m in r.connectors.values() if m['class'] == 'health']
        self.assertGreaterEqual(len(health), 5)
        root = PLUGINS.parent
        for m in health:
            self.assertTrue(m['min_data'])
            for f in m['fixtures']:
                self.assertTrue((root / f).exists(), '%s names missing fixture %s' % (m['id'], f))

    def test_health_connector_needs_min_data_fixtures_and_core(self):
        with self.assertRaises(connectors.ManifestError):
            connectors.validate(base_manifest(**{'class': 'health'}))
        with self.assertRaises(connectors.ManifestError):
            connectors.validate(base_manifest(**{'class': 'health', 'min_data': ['appointments'], 'fixtures': ['x'], 'source': 'pack', 'sha256': 'a' * 64}))
        ok = connectors.validate(base_manifest(**{'class': 'health', 'min_data': ['appointments'], 'fixtures': ['x']}))
        self.assertEqual(ok['class'], 'health')

    def test_rejects_unknown_risk_keys_and_bad_outbound(self):
        for bad in ({'tools': {'x_y': {'risk': 'write'}}}, {'tools': {'x_y': {'risk': 'read', 'outbound': True}}},
                    {'tools': {'x_y': {'risk': 'read', 'sneaky': 1}}}, {'actions': {'shell': ['x_y']}}, {'extra': 1}):
            with self.assertRaises(connectors.ManifestError, msg=bad):
                connectors.validate(base_manifest(**bad))

    def test_every_registered_tool_is_declared_once(self):
        r = connectors.Registry()
        tools = r.tools()
        # Each declared tool name appears as a registration in some plugin source.
        sources = ''.join(Path(f).read_text() for f in glob.glob(str(PLUGINS / '*-plugin' / '*.mjs')) if not f.endswith('.test.mjs'))
        for name in tools:
            self.assertIn(name, sources, 'manifest declares %s but no plugin registers it' % name)

    def test_external_tools_are_approval_bound(self):
        r = connectors.Registry()
        external = {n for n, s in r.tools().items() if s['risk'] == 'external'}
        self.assertIn('web_task_prepare', external)
        self.assertIn('health_portal_book', external)
        for n, s in r.tools().items():
            if n in ('mail_create_draft', 'phone_prepare_call', 'web_task_step', 'web_browser_prepare', 'mail_save_icloud_draft'):
                self.assertTrue(s.get('outbound'), n + ' must be checked for health details')


class Gate(unittest.TestCase):
    def test_undeclared_action_refused_on_tools_socket_only(self):
        r = connectors.Registry(mode='enforce')
        self.assertIsNone(r.gate('snapshot', False, 'web'))
        self.assertIn('not declared', r.gate('execute', False, 'web'))
        self.assertIsNone(r.gate('execute', True, 'web'))   # owner commands are not gated here
        self.assertIn('not declared', r.gate('send', False, 'mail'))
        self.assertIn('not declared', r.gate(None, False, 'web'))
        self.assertIn('execute', r.summary()['refused_recent'])

    def test_audit_mode_logs_and_allows(self):
        r = connectors.Registry(mode='audit')
        self.assertIsNone(r.gate('made_up_action', False, 'web'))

    def test_every_non_privileged_plugin_call_is_declared(self):
        r = connectors.Registry()
        declared = r.actions('web') | r.actions('mail')
        missing = set()
        for f in glob.glob(str(PLUGINS / '*-plugin' / '*.mjs')):
            if f.endswith('.test.mjs'):
                continue
            s = Path(f).read_text()
            for m in re.finditer(r"\b(?:call|broker|webCall|mailCall|mail|this\.call|mailBroker|callBroker)\(\s*'([a-z_]+)'", s):
                i, depth, j = m.end(), 1, m.end()
                while j < len(s) and depth:
                    depth += {'(': 1, ')': -1}.get(s[j], 0)
                    j += 1
                if re.search(r',\s*true\s*$', s[i:j - 1]):
                    continue
                if m.group(1) not in declared:
                    missing.add('%s: %s' % (os.path.basename(f), m.group(1)))
        self.assertEqual(sorted(missing), [])

    def test_literal_handler_names_are_declared_or_owner_only(self):
        r = connectors.Registry()
        for broker, pydir in (('web', 'web'), ('mail', 'mail')):
            handlers = set()
            for f in glob.glob(str(PLUGINS / pydir / '*.py')):
                if os.path.basename(f).startswith('test_'):
                    continue
                s = Path(f).read_text()
                handlers |= set(re.findall(r"action\s*==\s*'([a-z0-9_]+)'", s))
                for grp in re.findall(r"action\s+in\s*\(([^)]*)\)", s):
                    handlers |= set(re.findall(r"'([a-z0-9_]+)'", grp))
            js = set()
            for f in glob.glob(str(PLUGINS / '*-plugin' / '*.mjs')):
                if not f.endswith('.test.mjs'):
                    js |= set(re.findall(r"'([a-z][a-z0-9_]*)'", Path(f).read_text()))
            unclassified = sorted((js & handlers) - r.actions(broker) - OWNER_ONLY[broker] - (r.actions('mail') if broker == 'web' else r.actions('web')))
            self.assertEqual(unclassified, [], 'classify these %s actions: declare them in a manifest or add them to OWNER_ONLY' % broker)


class Packs(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.packs = self.d / 'packs'
        (self.packs / 'notes').mkdir(parents=True)
        (self.packs / 'notes' / 'tool.mjs').write_text('export const x=1;\n')
        self.m = base_manifest(id='notes-pack', source='pack', tools={'notes_search': {'risk': 'read'}}, actions={'web': ['notes_search']}, sha256='0' * 64)
        self.write()

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self):
        (self.packs / 'notes' / 'manifest.json').write_text(json.dumps(self.m))

    def test_unpinned_or_altered_pack_is_refused(self):
        h = connectors.tree_hash(self.packs / 'notes')
        self.m['sha256'] = h
        self.write()
        r = connectors.Registry(packs=self.packs, lock=self.d / 'packs.lock')
        self.assertNotIn('notes-pack', r.connectors)
        self.assertTrue(any('not pinned' in p for p in r.problems))
        (self.d / 'packs.lock').write_text(h + '  notes-pack 1.0.0\n')
        r = connectors.Registry(packs=self.packs, lock=self.d / 'packs.lock')
        self.assertIn('notes-pack', r.connectors)
        self.assertIsNone(r.gate('notes_search', False, 'web'))
        (self.packs / 'notes' / 'tool.mjs').write_text('export const x=2; // changed after pinning\n')
        r = connectors.Registry(packs=self.packs, lock=self.d / 'packs.lock')
        self.assertNotIn('notes-pack', r.connectors)
        self.assertTrue(any('do not match' in p for p in r.problems))

    def test_pack_cannot_be_health_or_owner_or_shadow_core(self):
        for change in ({'class': 'health', 'min_data': ['appointments'], 'fixtures': ['x']}, {'tools': {'notes_cmd': {'risk': 'owner'}}},
                       {'tools': {'mail_create_draft': {'risk': 'read'}}}):
            m = {**self.m, **change}
            (self.packs / 'notes' / 'manifest.json').write_text(json.dumps(m))
            m['sha256'] = connectors.tree_hash(self.packs / 'notes')
            (self.packs / 'notes' / 'manifest.json').write_text(json.dumps(m))
            (self.d / 'packs.lock').write_text(m['sha256'] + '\n')
            r = connectors.Registry(packs=self.packs, lock=self.d / 'packs.lock')
            self.assertNotIn('notes-pack', r.connectors, change)


class PolicyFiles(unittest.TestCase):
    def test_plugin_policy_files_match_manifests(self):
        want = connectors.Registry(mode='enforce').policy()
        for plugin in ('web-plugin', 'mail-plugin', 'voice-plugin'):
            got = json.loads((PLUGINS / plugin / 'connector-policy.json').read_text())
            self.assertEqual(got, want, plugin + '/connector-policy.json is stale: run python3 integrations/web/connectors.py policy ...')

    def test_gate_module_copies_are_identical(self):
        src = (PLUGINS / 'web-plugin' / 'connector-gate.mjs').read_bytes()
        for plugin in ('mail-plugin', 'voice-plugin'):
            self.assertEqual((PLUGINS / plugin / 'connector-gate.mjs').read_bytes(), src, plugin + ' carries a stale connector-gate.mjs')


if __name__ == '__main__':
    unittest.main()


class Contracts(unittest.TestCase):
    def test_every_manifest_tool_is_declared_in_its_plugin_contracts(self):
        # 0.27.0: the gateway refuses a tool its plugin does not declare in openclaw.plugin.json contracts.tools (deploy check A07).
        # 0.25.0/0.26.0 shipped tools that were missing there; this keeps manifests and contracts in step.
        import json
        here = Path(__file__).resolve().parent.parent
        r = connectors.Registry()
        for plugin, d in (('procurator-web', 'web-plugin'), ('procurator-mail', 'mail-plugin'), ('procurator-voice', 'voice-plugin')):
            declared = set(json.loads((here / d / 'openclaw.plugin.json').read_text())['contracts']['tools'])
            tools = {n for m in r.connectors.values() if m['plugin'] == plugin for n in m['tools']}
            self.assertEqual(sorted(tools - declared), [], plugin + ' tools missing from contracts.tools')


class EditionTests(unittest.TestCase):
    """0.30.0: PROCURATOR (standard) and PROCURATOR-X (x) share one policy file; x-only connectors are inert on a standard box."""

    def test_builds_is_x_only_and_policy_marks_it(self):
        pol = connectors.Registry(mode='enforce', edition='x').policy()
        self.assertEqual(pol['tools']['build_prepare'].get('edition'), 'x')
        self.assertNotIn('edition', pol['tools']['web_search'] if 'web_search' in pol['tools'] else {})
        std = connectors.Registry(mode='enforce', edition='standard')
        self.assertNotIn('builds', [m['id'] for m in std.active()])
        self.assertIn('builds', [m['id'] for m in connectors.Registry(mode='enforce', edition='x').active()])
        # the policy file itself does not depend on the edition (it is committed once)
        self.assertEqual(std.policy(), connectors.Registry(mode='enforce', edition='x').policy())

    def test_instance_edition_reads_env_then_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'instance.env'
            f.write_text('DOMAIN=box.example.com\nEDITION=x\n')
            old = os.environ.pop('PROCURATOR_EDITION', None)
            try:
                self.assertEqual(connectors.instance_edition(f), 'x')
                f.write_text('DOMAIN=box.example.com\n')
                self.assertEqual(connectors.instance_edition(f), 'standard')
                os.environ['PROCURATOR_EDITION'] = 'x'
                self.assertEqual(connectors.instance_edition(f), 'x')
                os.environ['PROCURATOR_EDITION'] = 'nonsense'
                self.assertEqual(connectors.instance_edition(Path(td) / 'missing'), 'standard')
            finally:
                os.environ.pop('PROCURATOR_EDITION', None)
                if old is not None:
                    os.environ['PROCURATOR_EDITION'] = old

