import json
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from broker import Broker
from test_broker import Provider,control

CONTEXT={'agentId':'main','sessionKey':'agent:main:telegram:direct:100000001','sessionId':'test-session','accountId':'default'}

class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.provider=Provider();self.b=Broker(Path(self.tmp.name)/'test.sqlite3',self.provider)
        self.s=self.b.call('open',{'request_id':'practice-start','url':'practice'})
    def tearDown(self):self.tmp.cleanup()
    def task(self,activate=True,max_steps=4):
        t=self.b.call('task_prepare',{'request_id':'practice-task-1','session_id':self.s['id'],'title':'Practice task',
          'goal':'Fill the practice visitor name then submit after separate approval.',
          'fields':[{'label':'Name','kind':'fill','value':'Practice Visitor'}], 'max_steps':max_steps,'completion_text':'PRACTICE CONFIRMED'})
        self.b.call('task_review',{'id':t['id']});r=self.b.tasks.row(t['id'])
        args={'id':t['id'],'hash':r['hash'][:16],'context':CONTEXT}
        if activate:self.b.call('task_activate',args,True)
        return t['id'],args
    def test_webstop_works_on_a_reviewed_task_that_was_never_activated(self):
        # Nov 6 QA: /webstop on a reviewed, inactive task said it "belongs to an earlier conversation".
        identifier,_=self.task(activate=False)
        self.assertEqual(self.b.call('task_stop',{'id':identifier,'context':{**CONTEXT,'sessionId':'another-session'}},True)['status'],'cancelled')
        with self.assertRaises(ValueError):self.b.call('task_stop',{'id':identifier},False)
    def step(self,identifier,rid='routine-step-1',steps=None):
        return self.b.call('task_step',{'id':identifier,'runtime_context':CONTEXT,'request_id':rid,'steps':steps or [{'kind':'fill','ref':'e0','value':'Practice Visitor'}]})
    def final(self,identifier):
        p=self.step(identifier,'final-step-1',[{'kind':'click','ref':'e1'}]);self.assertEqual(p['status'],'needs_separate_review')
        self.b.call('review',{'id':p['action_id']});r=self.b.plan(p['action_id'])
        return {'id':r['id'],'hash':r['hash'][:16],'context':CONTEXT}
    def test_grant_activation_native_only_and_exact(self):
        t,args=self.task(False)
        with self.assertRaises(ValueError):self.step(t)
        with self.assertRaises(ValueError):self.b.call('task_activate',args)
        with self.assertRaises(ValueError):self.b.call('task_activate',args|{'hash':'0'*16},True)
        self.assertEqual(self.provider.acts,0)
        self.b.call('task_activate',args,True);self.assertEqual(self.step(t)['status'],'completed')
    def test_task_lifetime_matches_new_browser_lifetime(self):
        t,_=self.task()
        session=self.b.session(self.s['id'])
        self.assertEqual(session['timeout_seconds'],2700)
        self.assertEqual(self.b.tasks.row(t)['expires'],session['created']+2700)
        self.assertEqual(self.b.call('status',{})['sessions'][0]['expires_after_minutes'],45)
    def test_reviewed_task_expires_when_its_browser_ends(self):
        t,args=self.task(False)
        self.b.call('close',{'id':self.s['id']})
        self.assertEqual(self.b.tasks.row(t)['status'],'expired')
        with self.assertRaises(ValueError):self.b.call('task_activate',args,True)
        self.assertEqual(self.provider.acts,0)
    def test_exact_routine_next_runs_under_native_task_grant_without_another_approval(self):
        page='https://example.com/form'
        with self.b.db() as db:db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?',('https://example.com',page,self.s['id']))
        self.provider.snap['url']=page
        self.provider.snap['controls'].append(control('e2',kind='button',label='Next')|{'tag':'button','role':'','form_action':''})
        self.b.tasks.url=lambda value:(value,'https://example.com')
        t=self.b.call('task_prepare',{'request_id':'routine-next-task','session_id':self.s['id'],'title':'Patient details','goal':'Continue after approved entries',
            'clicks':[{'label':'Next','url':page,'tag':'button','type':'button','role':''}]})
        self.b.call('task_review',{'id':t['id']})
        self.assertEqual(self.provider.acts,0)
        record=self.b.tasks.row(t['id'])
        self.b.call('task_activate',{'id':t['id'],'hash':record['hash'][:16],'context':CONTEXT},True)
        result=self.step(t['id'],rid='routine-next-1',steps=[{'kind':'click','ref':'e2'}])
        self.assertEqual(result['status'],'completed');self.assertEqual(self.provider.acts,1)
    def test_owner_reviewed_guest_choice_executes_once_under_the_active_task(self):
        page='https://www.cvs.com/scheduling/patient-lookup?lob=rximz&flow=vaccine'
        with self.b.db() as db:db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?',('https://www.cvs.com',page,self.s['id']))
        self.provider.snap.update({'url':page,'title':'Send verification code - CVS','controls':[
            control('e0',kind='',label='Continue as a guest')|{'tag':'a','role':'','href':'javascript:void(0)','form_action':''}]})
        t=self.b.call('task_prepare',{'request_id':'cvs-guest-granted','session_id':self.s['id'],'title':'Owner-selected guest booking',
            'goal':'Book the approved flu shot. Use Continue as a guest when offered.'})
        self.b.call('task_review',{'id':t['id']});record=self.b.tasks.row(t['id'])
        self.b.call('task_activate',{'id':t['id'],'hash':record['hash'][:16],'context':CONTEXT},True)
        result=self.step(t['id'],rid='guest-once',steps=[{'kind':'click','ref':'e0'}])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.step(t['id'],rid='guest-once',steps=[{'kind':'click','ref':'e0'}])['id'],result['id'])
        self.assertEqual(self.provider.acts,1)
    def test_changed_value_needs_separate_review(self):
        t,_=self.task();p=self.step(t,steps=[{'kind':'fill','ref':'e0','value':'Other visitor'}])
        self.assertEqual(p['status'],'needs_separate_review');self.assertEqual(self.provider.acts,0)
    def test_task_does_not_toggle_an_already_visible_cvs_birth_date_off(self):
        t,_=self.task()
        with self.b.db() as db:db.execute('UPDATE sessions SET origin=? WHERE id=?',('https://www.cvs.com',self.s['id']))
        self.provider.snap['controls'].append(control('e2',kind='checkbox',label='Show date of birth')|{'checked':True})
        with self.assertRaisesRegex(ValueError,'already visible'):
            self.step(t,rid='already-visible-birth',steps=[{'kind':'click','ref':'e2'}])
        self.assertEqual(self.provider.acts,0)
    def test_duplicate_request_does_not_repeat_after_page_changes(self):
        t,_=self.task();p=self.step(t);self.provider.snap['controls'][0]['value']='Practice Visitor'
        self.assertEqual(self.step(t)['id'],p['id']);self.assertEqual(self.provider.acts,1)
        with self.assertRaises(ValueError):self.step(t,steps=[{'kind':'fill','ref':'e0','value':'Changed'}])
    def test_ambiguous_label_requires_manual_review(self):
        t,_=self.task();self.provider.snap['controls'].append(control('e2'))
        self.assertEqual(self.step(t)['status'],'needs_separate_review');self.assertEqual(self.provider.acts,0)
    def test_budget_and_expiry_enforced(self):
        t,_=self.task(max_steps=1);self.step(t)
        with self.assertRaises(ValueError):self.step(t,'new-step-2')
        with self.b.db() as db:db.execute('UPDATE web_tasks SET expires=? WHERE id=?',(time.time()-1,t))
        with self.assertRaises(ValueError):self.step(t,'new-step-3')
        self.assertEqual(self.provider.acts,1)
    def test_final_approval_bound_to_native_original_conversation(self):
        t,_=self.task();args=self.final(t)
        for ctx in [None,CONTEXT|{'sessionId':'reset-session'}]:
            with self.assertRaises(ValueError):self.b.call('execute',args|{'context':ctx},True)
        self.assertEqual(self.provider.acts,0)
        self.assertEqual(self.b.call('execute',args,True)['status'],'completed')
        self.assertEqual(self.b.call('task_get',{'id':t,'runtime_context':CONTEXT})['status'],'active')
    def test_paused_task_rejects_pending_approval(self):
        t,_=self.task();args=self.final(t);self.b.call('task_pause',{'id':t,'context':CONTEXT},True)
        with self.assertRaises(ValueError):self.b.call('execute',args,True)
        self.assertEqual(self.provider.acts,0)
    def test_cancelled_final_action_pauses_task_without_hanging_pending_approval(self):
        t,_=self.task();args=self.final(t);self.b.call('cancel',{'id':args['id']})
        task=self.b.call('task_get',{'id':t,'runtime_context':CONTEXT})
        self.assertEqual(task['status'],'paused');self.assertIsNone(task['pending_action'])
        with self.assertRaises(ValueError):self.b.call('execute',args,True)
    def test_native_continue_withdraws_pending_review_and_requires_fresh_final_approval(self):
        t,_=self.task();args=self.final(t)
        with self.assertRaises(ValueError):self.b.call('task_resume',{'id':t,'context':CONTEXT})
        self.b.call('task_resume',{'id':t,'context':CONTEXT},True)
        self.assertEqual(self.b.plan(args['id'])['status'],'cancelled')
        with self.assertRaises(ValueError):self.b.call('execute',args,True)
        self.assertEqual(self.provider.acts,0)
        self.assertEqual(self.step(t)['status'],'completed')
    def test_uncertainty_and_restart_do_not_repeat(self):
        # Typing that crashed is fields_incomplete (Nov 4): the task stays active, the same request never replays.
        t,_=self.task();self.provider.fail=True;self.assertEqual(self.step(t)['status'],'fields_incomplete')
        self.b=Broker(self.b.path,self.provider)
        self.assertEqual(self.b.call('task_get',{'id':t,'runtime_context':CONTEXT})['status'],'active')
        self.assertEqual(self.step(t)['status'],'fields_incomplete')
        self.assertEqual(self.provider.acts,1)
    def test_routine_dispatch_receipt_recovers_on_snapshot_without_repeating_the_effect(self):
        t,_=self.task();original=self.provider.run
        def dispatch(s,operation,**kwargs):
            if operation!='act':return original(s,operation,**kwargs)
            self.provider.acts+=1;self.provider.snap['controls'][0]['value']='Practice Visitor'
            return {'ok':False,'kind':'verification_pending','action_dispatched':True}
        with patch.object(self.provider,'run',side_effect=dispatch):result=self.step(t)
        self.assertEqual(result['status'],'verification_pending')
        self.assertEqual(self.b.tasks.public(self.b.tasks.row(t))['pending_checks'][0]['id'],result['id'])
        with self.assertRaises(ValueError):self.b.hold()
        self.b.call('snapshot',{'id':self.s['id']})
        self.assertEqual(self.b.plan(result['id'])['status'],'completed')
        self.assertEqual(self.b.tasks.row(t)['status'],'active')
        self.assertEqual(self.step(t)['id'],result['id']);self.assertEqual(self.provider.acts,1)
    def test_final_dispatch_receipt_does_not_clear_without_owner_inspection(self):
        t,_=self.task();args=self.final(t)
        with patch.object(self.provider,'run',return_value={'ok':False,'kind':'verification_pending','action_dispatched':True}):
            result=self.b.call('execute',args,True)
        self.assertEqual(result['status'],'verification_pending')
        self.b.call('snapshot',{'id':self.s['id']})
        self.assertEqual(self.b.plan(result['id'])['status'],'verification_pending')
        with self.assertRaises(ValueError):self.b.hold()
        with self.assertRaises(ValueError):self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT})
    def test_acknowledged_uncertain_request_never_replays_after_native_resume(self):
        t,_=self.task();self.provider.fail=True;result=self.step(t)
        with self.b.db() as db:db.execute("UPDATE plans SET status='uncertain' WHERE id=?",(result['id'],))   # e.g. an older click
        self.b.call('resolve',{'id':result['id']},True)
        self.b.call('task_resume',{'id':t,'context':CONTEXT},True)
        self.assertEqual(self.step(t)['status'],'acknowledged_uncertain')
        self.assertEqual(self.provider.acts,1)
    def test_completion_needs_approved_submit_and_live_confirmation(self):
        t,_=self.task();self.step(t)
        with self.assertRaises(ValueError):self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT})
        args=self.final(t);self.b.call('execute',args,True)
        with self.assertRaises(ValueError):self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT})
        self.provider.snap['text']='PRACTICE CONFIRMED: Practice Visitor'
        done=self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT});self.assertEqual(done['status'],'complete');self.assertTrue(done['result']['browser_closed'])
    def test_continuation_receipts_deduplicate_and_do_not_complete_task(self):
        t,_=self.task();args={'id':t,'event_id':'activate:'+t,'context':CONTEXT}
        with self.assertRaises(ValueError):self.b.call('task_resume_claim',args)
        self.assertTrue(self.b.call('task_resume_claim',args,True)['claimed'])
        self.assertFalse(self.b.call('task_resume_claim',args,True)['claimed'])
        self.b.call('task_resume_receipt',args|{'status':'accepted'},True)
        self.assertFalse(self.b.call('task_resume_claim',args|{'event_id':'another'},True)['claimed'])
        self.b.call('task_resume_receipt',args|{'status':'completed'},True)
        self.assertEqual(self.b.call('task_get',{'id':t,'runtime_context':CONTEXT})['status'],'active')
        self.assertTrue(self.b.call('task_resume_claim',args|{'event_id':'another'},True)['claimed'])
    def test_native_context_is_not_returned_to_model(self):
        t,_=self.task();self.assertNotIn('test-session',json.dumps(self.b.call('task_get',{'id':t,'runtime_context':CONTEXT})))
    def test_routine_actions_reject_a_different_model_session(self):
        t,_=self.task()
        with self.assertRaises(ValueError):self.b.call('task_step',{'id':t,'runtime_context':CONTEXT|{'sessionId':'reset'},'request_id':'reset-step-1','steps':[{'kind':'fill','ref':'e0','value':'Practice Visitor'}]})
        self.assertEqual(self.provider.acts,0)
    def test_legacy_wake_is_observed_only_by_correct_runtime_session(self):
        t,_=self.task();args={'id':t,'context':CONTEXT,'event_id':'legacy:'+t}
        self.b.call('task_resume_claim',args,True);self.b.call('task_resume_receipt',args|{'status':'wake_requested'},True)
        with self.assertRaises(ValueError):self.b.call('task_get',{'id':t,'runtime_context':CONTEXT|{'sessionId':'reset'}})
        current=self.b.call('task_get',{'id':t,'runtime_context':CONTEXT})
        self.assertEqual(current['continuations'][0]['status'],'observed');self.assertEqual(current['status'],'active')

if __name__=='__main__':unittest.main()
