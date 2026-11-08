import copy
import importlib.util
import unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('realtime_candidate',Path(__file__).parents[1]/'bin/stage-realtime-voice.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class CandidateTests(unittest.TestCase):
    def current(self):
        return {'plugins':{'entries':{'voice-call':{'config':{'provider':'twilio','fromNumber':'+15550100100','maxDurationSeconds':110,'reports':{'enabled':True}}}}},'agents':{'entries':{'phone':{'tools':{'allow':['phone_line_info']}}}},'tools':{'deny':['voice_call']}}
    def test_candidate_preserves_limits_and_approval_boundary_without_mutating_live_input(self):
        old=self.current();snapshot=copy.deepcopy(old);new=m.build_config(old)
        self.assertEqual(old,snapshot)
        self.assertEqual(new['tools']['deny'],['voice_call'])
        v=new['plugins']['entries']['voice-call']['config']
        self.assertEqual(v['maxDurationSeconds'],110)
        self.assertTrue(v['reports']['enabled'])
        self.assertEqual(v['realtime']['toolPolicy'],'none')
        self.assertFalse(v['realtime']['agentContext']['enabled'])
        self.assertEqual(v['realtime']['providers']['openai']['apiKey'],'${OPENAI_API_KEY}')
        self.assertEqual(v['realtime']['providers']['openai']['voice'],'marin')
    def test_transport_and_tool_policy_drift_are_rejected(self):
        old=self.current();old['plugins']['entries']['voice-call']['config']['skipSignatureVerification']=True
        with self.assertRaises(ValueError):m.build_config(old)
        old=self.current();old['agents']['entries']['phone']['tools']['allow'].append('mail_accounts')
        with self.assertRaises(ValueError):m.build_config(old)
