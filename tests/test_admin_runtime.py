import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('admin_runtime', Path(__file__).parents[1]/'bin/optimize-admin-runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeTests(unittest.TestCase):
    def current(self):
        return {'agents': {'defaults': {'model': {'primary': 'anthropic/claude-sonnet-5-5'}, 'timeoutSeconds': 120},
                           'entries': {'phone': {'tools': {'allow': ['phone_line_info']}}}},
                'tools': {'alsoAllow': ['web_task_step'], 'deny': ['voice_call']},
                'plugins': {'entries': {'procurator-web': {'enabled': True}, 'voice-call': {'config': {'maxDurationSeconds': 110, 'realtime': {'enabled': False}}}}}}

    def test_retains_phone_transport_and_action_boundaries(self):
        old = self.current()
        result = module.build_config(old)
        self.assertEqual(old['agents']['defaults']['timeoutSeconds'], 120)
        self.assertEqual(result['agents']['defaults']['timeoutSeconds'], 600)
        self.assertEqual(result['agents']['defaults']['maxConcurrent'], 1)
        self.assertEqual(result['agents']['entries'], old['agents']['entries'])
        self.assertEqual(result['plugins']['entries']['voice-call'], old['plugins']['entries']['voice-call'])
        self.assertEqual(result['tools']['deny'], ['voice_call'])
        self.assertEqual(result['agents']['defaults']['contextPruning']['tools']['allow'], ['web_browser_snapshot', 'web_fetch', 'web_search'])
        self.assertEqual(module.build_config(result), result)

    def test_web_plugin_hooks_allow_conversation_access_and_prompt_injection(self):
        hooks = module.build_config(self.current())['plugins']['entries']['procurator-web']['hooks']
        self.assertTrue(hooks['allowConversationAccess'])
        self.assertTrue(hooks['allowPromptInjection'])

    def test_preserves_existing_context_and_model_tuning(self):
        old = self.current()
        defaults = old['agents']['defaults']
        defaults.update(contextPruning={'mode': 'off'}, compaction={'mode': 'default'}, timeoutSeconds=900,
                        models={'anthropic/claude-sonnet-5-5': {'params': {'cacheRetention': 'long'}}})
        patched = module.build_config(old)['agents']['defaults']
        self.assertEqual(patched['contextPruning'], {'mode': 'off'})
        self.assertEqual(patched['compaction']['mode'], 'default')
        self.assertEqual(patched['timeoutSeconds'], 900)
        self.assertEqual(patched['models'], defaults['models'])
        defaults['model']['primary'] = 'other/model'
        with self.assertRaises(ValueError):
            module.build_config(old)
