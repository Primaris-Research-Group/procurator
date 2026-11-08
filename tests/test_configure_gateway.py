"""bin/configure-gateway.py: a fresh box gets the PROCURATOR plugins loaded, their tools allow-listed (experimental modules off),
the main agent's defaults, and nothing personal; a refused batch falls back to one setting at a time."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_gateway', ROOT / 'bin' / 'configure-gateway.py')
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)


class Result:
    def __init__(self, rc):
        self.returncode = rc


class ConfigureTests(unittest.TestCase):
    def test_batch_loads_plugins_allows_their_tools_and_is_impersonal(self):
        items = cg.batch('America/Chicago')
        by = {i['path']: i['value'] for i in items}
        self.assertEqual([p.rsplit('/', 1)[1] for p in by['plugins.load.paths']], ['web-plugin', 'mail-plugin'])
        web_id = cg.plugin_manifest('web-plugin')['id']
        self.assertEqual(by['plugins.entries.' + web_id]['hooks'], {'allowConversationAccess': True, 'allowPromptInjection': True})
        also = set(by['tools.alsoAllow'])
        self.assertTrue({'owner_welcome_save', 'accounts_list', 'mail_request_review', 'web_search'} <= also)
        self.assertFalse(any(t.startswith(('social_', 'home_')) for t in also), 'experimental modules stay off')
        self.assertEqual(by['agents.defaults.userTimezone'], 'America/Chicago')
        self.assertEqual(by['tools.deny'], ['voice_call'])
        self.assertEqual(by['skills.entries.1password'], {'enabled': False})
        self.assertEqual(by['plugins.entries.duckduckgo'], {'enabled': True})
        no_ddg = {i['path']: i['value'] for i in cg.batch('UTC', ddg=False)}
        self.assertNotIn('plugins.entries.duckduckgo', no_ddg, 'search is only configured when the plugin installed')
        self.assertNotIn('search', no_ddg['tools.web'])
        text = json.dumps(items)
        self.assertNotRegex(text, r'\d{9,}', 'no chat ids or phone numbers')
        self.assertNotIn('channels.telegram', text, 'Telegram and the owner are set by setup, not here')
        self.assertNotIn('voice-plugin', text)
        self.assertIn('voice-plugin', json.dumps(cg.batch('UTC', with_voice=True)))

    def test_existing_allow_list_is_kept(self):
        items = cg.batch('UTC', existing={'tools': {'alsoAllow': ['my_custom_tool']}})
        self.assertIn('my_custom_tool', next(i['value'] for i in items if i['path'] == 'tools.alsoAllow'))

    def test_refused_batch_falls_back_to_single_settings(self):
        calls = []

        def run(cmd):
            calls.append(json.loads(Path(state, 'procurator-config.json').read_text()))
            batch = calls[-1]
            if len(batch) > 1:
                return Result(1)
            return Result(1 if batch[0]['path'] == 'tools.loopDetection' else 0)
        with tempfile.TemporaryDirectory() as state:
            items = cg.batch('UTC')
            refused = cg.apply(items, run, Path(state))
            self.assertEqual(refused, ['tools.loopDetection'])
            self.assertEqual(len(calls), 1 + len(items))
            self.assertFalse(Path(state, 'procurator-config.json').exists(), 'the batch file is removed afterwards')


    def test_fresh_box_gets_a_config_file_first(self):
        with tempfile.TemporaryDirectory() as state:
            seen = []

            def run_ok(cmd):
                seen.append(cmd)
                if 'onboard' in cmd:
                    Path(state, 'openclaw.json').write_text('{}')
                return Result(0)
            self.assertEqual(cg.ensure_config(run_ok, Path(state)), 'onboard')
            self.assertIn('--non-interactive', seen[0])
            self.assertIsNone(cg.ensure_config(run_ok, Path(state)), 'an existing config is left alone')
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(cg.ensure_config(lambda cmd: Result(2), Path(state)), 'seeded')
            self.assertEqual(json.loads(Path(state, 'openclaw.json').read_text()), {'gateway': {'mode': 'local'}})


    def test_installer_rerun_configures_a_running_gateway_from_inside(self):
        class Out(Result):
            def __init__(self, rc, out=''):
                super().__init__(rc)
                self.stdout = out
        seen = []

        def run(cmd):
            seen.append(cmd)
            return Out(0, 'abc\n' if cmd[:3] == ['docker', 'compose', 'ps'] else '')
        live = cg.live_runner(run)
        live(cg.ONE_OFF + ['dist/index.js', 'config', 'set', '--batch-file', 'x'])
        self.assertEqual(seen[-1], ['docker', 'compose', 'exec', '-T', 'openclaw-gateway', 'node', 'dist/index.js', 'config', 'set', '--batch-file', 'x'])
        idle = lambda cmd: (seen.append(cmd), Out(0, ''))[1]
        self.assertIs(cg.live_runner(idle), idle, 'a stopped gateway keeps the one-off container')

if __name__ == '__main__':
    unittest.main()
