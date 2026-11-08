import importlib.util
import unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location('phone_assignments', Path(__file__).parents[1]/'bin/stage-phone-assignments.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AssignmentConfigTests(unittest.TestCase):
    def config(self):
        return {'plugins': {'entries': {'voice-call': {'config': {'provider': 'twilio', 'fromNumber': '+15550100100', 'maxDurationSeconds': 50}}, 'procurator-voice': {}, 'procurator-mail': {'enabled': True}}},
                'agents': {'entries': {'main': {'workspace': 'private-main'}, 'phone': {'tools': {'allow': ['phone_line_info']}}}}, 'tools': {'alsoAllow': ['mail_accounts'], 'deny': ['voice_call']}}

    def test_preserves_private_agents_and_denied_raw_call_control(self):
        old = self.config()
        new = module.build_config(old)
        self.assertEqual(old['plugins']['entries']['voice-call']['config']['maxDurationSeconds'], 50)
        self.assertEqual(new['agents'], old['agents'] | {'entries': old['agents']['entries'] | {'phone': old['agents']['entries']['phone'] | {'name': 'PROCURATOR Phone Assistant'}}})
        self.assertEqual(new['tools']['deny'], ['voice_call'])
        self.assertTrue(new['plugins']['entries']['procurator-mail']['enabled'])
        self.assertEqual(new['plugins']['entries']['voice-call']['config']['reports']['inboundSessionKey'], 'agent:main:main')
        self.assertFalse(new['plugins']['entries']['voice-call']['config']['reports']['includeTranscript'])
        self.assertEqual(new['tools']['alsoAllow'][0], 'mail_accounts')

    def test_rejects_transport_and_tool_scope_drift(self):
        for key, value in [('skipSignatureVerification', True), ('realtime', {'enabled': True}), ('fromNumber', '+15555550123')]:
            old = self.config()
            old['plugins']['entries']['voice-call']['config'][key] = value
            with self.assertRaises(ValueError):
                module.build_config(old)
        old = self.config()
        old['agents']['entries']['phone']['tools']['allow'].append('mail_accounts')
        with self.assertRaises(ValueError):
            module.build_config(old)
