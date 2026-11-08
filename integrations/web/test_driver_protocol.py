import json
import unittest
from broker import driver_result

class DriverProtocolTests(unittest.TestCase):
    def frames(self,*items):return '\n'.join(json.dumps(x) for x in items)
    def test_dispatched_interaction_is_distinct_from_lost_followup_snapshot(self):
        raw=self.frames({'event':'phase','stage':'action'},{'event':'action_dispatched','steps':1},
            {'event':'phase','stage':'snapshot_after'})
        result=driver_result(raw,'act',1,timed_out=True)
        self.assertEqual(result['kind'],'verification_pending')
        self.assertTrue(result['action_dispatched'])
        self.assertEqual(result['error_stage'],'snapshot_after')
    def test_partial_or_missing_dispatch_receipt_stays_uncertain(self):
        for raw in [self.frames({'event':'phase','stage':'action'}),self.frames({'event':'action_dispatched','steps':1})]:
            self.assertEqual(driver_result(raw,'act',2,timed_out=True)['kind'],'unconfirmed')
    def test_regular_and_legacy_results_are_preserved(self):
        final={'ok':True,'snapshot':{'title':'Practice'}}
        self.assertEqual(driver_result(json.dumps(final),'snapshot',0),final)
        self.assertEqual(driver_result(self.frames({'event':'phase','stage':'snapshot_after'},final),'snapshot',0),final)
    def test_stale_before_dispatch_never_becomes_dispatched(self):
        self.assertEqual(driver_result(self.frames({'ok':False,'kind':'stale'}),'act',1)['kind'],'stale')
    def test_unknown_diagnostic_text_is_not_returned(self):
        raw=self.frames({'event':'phase','stage':'PRIVATE_PROVIDER_DETAILS'})+'\nnot json'
        self.assertNotIn('PRIVATE_PROVIDER_DETAILS',str(driver_result(raw,'act',1,timed_out=True)))
