import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from broker import page_key,Broker,digest,url,Provider as BrowserbaseProvider,CreationRejected,SESSION_LIFETIME,CREATION_GRACE
import urllib.error
import time

def control(ref='e0',kind='text',label='Name'):
    return {'ref':ref,'tag':'input','type':kind,'name':'name','id':'name','label':label,
            'href':'','form_action':'about:blank','disabled':False,'checked':False,'value':'','options':[]}

class Provider:
    def __init__(self):
        self.created=0;self.acts=0;self.messages=[];self.fail=False;self.stale=False
        self.remote_status='RUNNING';self.found=None;self.lookup_fails=False
        self.snap={'url':'about:blank','title':'Practice','text':'Practice form','controls':[control(),control('e1','submit','Submit practice')],'frame_count':0,'form_errors':[]}
    def create(self,rid,practice=False,timeout_seconds=SESSION_LIFETIME):self.created+=1;return {'id':'00000000-0000-4000-8000-000000000001'}
    def inspect(self,identifier):
        if self.lookup_fails:raise TimeoutError()
        return {'id':identifier,'status':self.remote_status}
    def find(self,request_id):
        if self.lookup_fails:raise TimeoutError()
        return self.found
    def close(self,_):return {}
    def run(self,s,operation,**kwargs):
        if operation=='act':
            self.acts+=1
            if self.fail:raise TimeoutError()
            if self.stale:return {'ok':False,'kind':'stale'}
            # 0.20.7: like the driver — 'full' needs the exact page, 'page' the same website path, 'none' nothing.
            mode=kwargs.get('expected_mode','full')
            if mode=='full' and kwargs['expected']!=digest(self.snap):return {'ok':False,'kind':'stale'}
            if mode=='page' and kwargs.get('expected_page')!=page_key(self.snap.get('url')):return {'ok':False,'kind':'stale'}
        return {'ok':True,'snapshot':copy.deepcopy(self.snap)}
    def telegram(self,m):self.messages.append(m)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.provider=Provider();self.b=Broker(Path(self.tmp.name)/'test.sqlite3',self.provider)
        self.s=self.b.call('open',{'request_id':'practice-start','url':'practice'})
    def tearDown(self):self.tmp.cleanup()
    def prepare(self,rid='entry-0001',steps=None):
        return self.b.call('prepare',{'id':self.s['id'],'request_id':rid,'steps':steps or [{'kind':'fill','ref':'e0','value':'Practice Visitor'}]})
    def reviewed(self,steps=None):
        p=self.prepare(steps=steps);self.b.call('review',{'id':p['id']});r=self.b.plan(p['id']);return p,{'id':p['id'],'hash':r['hash'][:16]}
    def test_public_socket_cannot_execute_even_with_hash(self):
        p,args=self.reviewed()
        with self.assertRaises(ValueError):self.b.call('execute',args)
        self.assertEqual(self.provider.acts,0)
    def test_approved_exact_action_executes_once(self):
        p,args=self.reviewed();self.assertEqual(self.b.call('execute',args,True)['status'],'completed')
        self.assertEqual(self.b.call('execute',args,True)['status'],'completed');self.assertEqual(self.provider.acts,1)
    def test_wrong_hash_rejected(self):
        p,args=self.reviewed();args['hash']='0'*16
        with self.assertRaises(ValueError):self.b.call('execute',args,True)
        self.assertEqual(self.provider.acts,0)
    def test_review_is_required_and_expires(self):
        p=self.prepare();args={'id':p['id'],'hash':self.b.plan(p['id'])['hash'][:16]}
        with self.assertRaises(ValueError):self.b.call('execute',args,True)
        self.b.call('review',{'id':p['id']})
        with self.b.db() as db:db.execute('UPDATE plans SET reviewed=1 WHERE id=?',(p['id'],))
        with self.assertRaises(ValueError):self.b.call('execute',args,True)
    def test_stale_page_does_not_repeat(self):
        p,args=self.reviewed();self.provider.stale=True
        self.assertEqual(self.b.call('execute',args,True)['status'],'stale');self.b.call('execute',args,True)
        self.assertEqual(self.provider.acts,1)
    def test_uncertain_action_holds_other_actions_and_survives_restart(self):
        p,args=self.reviewed(steps=[{'kind':'click','ref':'e1'}]);self.provider.fail=True;self.assertEqual(self.b.call('execute',args,True)['status'],'uncertain')
        b=Broker(self.b.path,self.provider)
        with self.assertRaises(ValueError):b.call('prepare',{'id':self.s['id'],'request_id':'other-0001','steps':[{'kind':'click','ref':'e1'}]})
        b.call('execute',args,True);self.assertEqual(self.provider.acts,1)
        with self.assertRaises(ValueError):b.call('resolve',{'id':p['id']})
        b.call('resolve',{'id':p['id']},True);self.assertEqual(b.plan(p['id'])['status'],'acknowledged_uncertain')
    def test_crash_claim_becomes_uncertain(self):
        p=self.prepare(steps=[{'kind':'click','ref':'e1'}]);f=self.prepare(rid='entry-typing')
        with self.b.db() as db:db.execute("UPDATE plans SET status='executing' WHERE id IN (?,?)",(p['id'],f['id']))
        b=Broker(self.b.path,self.provider);self.assertEqual(b.plan(p['id'])['status'],'uncertain')
        self.assertEqual(b.plan(f['id'])['status'],'fields_incomplete')   # typing only: released on restart, never blocks
    def test_no_mixed_entries_and_submission(self):
        with self.assertRaises(ValueError):self.prepare(steps=[{'kind':'fill','ref':'e0','value':'Test'},{'kind':'click','ref':'e1'}])
        self.assertEqual(self.provider.acts,0)
    def test_passwords_and_generic_javascript_blocked(self):
        self.provider.snap['controls'][0]['type']='password'
        with self.assertRaises(ValueError):self.prepare()
        with self.assertRaises(ValueError):self.prepare(steps=[{'kind':'evaluate','value':'fetch("https://example.com")'}])
    def test_readiness_wait_is_read_only_bounded_and_does_not_grant_interactions(self):
        for state in ['verification_in_progress','verification_required','loading']:
            self.provider.snap['readiness']={'state':state}
            with self.assertRaisesRegex(ValueError,'still loading or verifying'):self.prepare()
            self.assertEqual(self.b.call('snapshot',{'id':self.s['id'],'wait_seconds':0})['snapshot']['readiness']['state'],state)
        for wait in [-1,31,True,'30']:
            with self.assertRaises(ValueError):self.b.call('snapshot',{'id':self.s['id'],'wait_seconds':wait})
        self.assertEqual(self.provider.acts,0)
    def test_provider_uses_supported_verification_only_for_public_sessions(self):
        p=BrowserbaseProvider()
        with patch.object(p,'api',return_value={'id':'provider-fixture'}) as api:
            p.create('public-fixture');self.assertFalse(api.call_args.args[2]['browserSettings']['solveCaptchas'])   # 0.19.2: owner solves CAPTCHAs themselves (Nov 4 decision)
            p.create('private-fixture',practice=True);self.assertFalse(api.call_args.args[2]['browserSettings']['solveCaptchas'])
    def test_complete_review_contains_exact_fields_and_no_execution(self):
        p=self.prepare();self.b.call('review',{'id':p['id']});m=self.provider.messages[0]
        self.assertIn('Practice Visitor',m);self.assertIn('/webapprove '+p['id'],m);self.assertEqual(self.provider.acts,0)
    def test_session_request_replay_does_not_create_duplicate(self):
        again=self.b.call('open',{'request_id':'practice-start','url':'practice'})
        self.assertEqual(again['id'],self.s['id']);self.assertEqual(self.provider.created,1)
    def test_explicit_longer_session_is_stored_and_bound_to_request(self):
        self.b.call('close',{'id':self.s['id']})
        opened=self.b.call('open',{'request_id':'long-owner-task','url':'practice','session_minutes':120})
        self.assertEqual(opened['expires_after_minutes'],120)
        self.assertEqual(self.b.session(opened['id'])['timeout_seconds'],7200)
        with self.assertRaises(ValueError):self.b.call('open',{'request_id':'long-owner-task','url':'practice','session_minutes':45})
        for value in [True,0,60,121]:
            with self.assertRaises(ValueError):self.b.call('open',{'request_id':'bad-browser-duration','url':'practice','session_minutes':value})
    def test_changed_request_id_content_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):self.prepare(steps=[{'kind':'fill','ref':'e0','value':'Different'}])
    def uncertain_creation(self,age=0,status='creation_uncertain'):
        with self.b.db() as db:
            db.execute('UPDATE sessions SET provider_id=NULL,status=?,created=? WHERE id=?',
                (status,time.time()-age,self.s['id']))
    def test_old_unknown_creation_is_reconciled_without_retry_or_record_deletion(self):
        self.uncertain_creation(SESSION_LIFETIME+CREATION_GRACE+10)
        status=self.b.call('status',{})
        self.assertEqual(status['sessions'],[])
        self.assertEqual(status['reconciled_sessions'][0]['status'],'closed')
        self.assertEqual(self.b.session(self.s['id'])['status'],'closed')
        self.assertEqual(self.provider.created,1)
        self.b.call('open',{'request_id':'recovered-browser','url':'practice'})
        self.assertEqual(self.provider.created,2)
    def test_open_recovers_expired_creation_without_a_separate_status_call(self):
        self.uncertain_creation(SESSION_LIFETIME+CREATION_GRACE+10,status='creating')
        self.assertEqual(self.b.call('open',{'request_id':'new-after-crash','url':'practice'})['status'],'open')
        self.assertEqual(self.provider.created,2)
    def test_recent_unknown_creation_is_not_retried_or_cleared(self):
        self.uncertain_creation(10)
        self.assertEqual(self.b.call('status',{})['sessions'][0]['status'],'creation_uncertain')
        with self.assertRaises(ValueError):self.b.call('open',{'request_id':'no-blind-retry','url':'practice'})
        with self.assertRaises(ValueError):self.b.call('close',{'id':self.s['id']})
        self.assertEqual(self.provider.created,1)
    def test_provider_outage_is_not_proof_of_session_absence(self):
        self.uncertain_creation(SESSION_LIFETIME+CREATION_GRACE+10)
        self.provider.lookup_fails=True
        self.assertEqual(self.b.call('status',{})['sessions'][0]['status'],'creation_uncertain')
        with self.assertRaises(ValueError):self.b.call('open',{'request_id':'blocked-outage','url':'practice'})
        self.assertEqual(self.provider.created,1)
    def test_matching_active_provider_is_adopted_without_reinitializing_its_page(self):
        self.uncertain_creation(5)
        self.provider.found={'id':'provider-recovered','status':'RUNNING'}
        with patch.object(self.provider,'run',side_effect=AssertionError('Must not navigate again')):
            status=self.b.call('status',{})
        self.assertEqual(status['sessions'][0]['status'],'open')
        self.assertEqual(self.b.session(self.s['id'])['provider_id'],'provider-recovered')
        self.assertEqual(self.provider.created,1)
        self.assertEqual(self.b.call('close',{'id':self.s['id']})['status'],'closed')
    def test_matching_ended_provider_preserves_identifier_and_releases_slot(self):
        self.uncertain_creation(5)
        self.provider.found={'id':'provider-ended','status':'TIMED_OUT'}
        self.assertEqual(self.b.call('status',{})['sessions'],[])
        self.assertEqual(self.b.session(self.s['id'])['provider_id'],'provider-ended')
    def test_known_ended_browser_does_not_block_new_browser(self):
        self.provider.remote_status='COMPLETED'
        self.assertEqual(self.b.call('open',{'request_id':'after-provider-ended','url':'practice'})['status'],'open')
        self.assertEqual(self.b.session(self.s['id'])['status'],'closed')
    def test_session_recovery_never_clears_uncertain_submission(self):
        p,args=self.reviewed(steps=[{'kind':'click','ref':'e1'}]);self.provider.fail=True
        self.assertEqual(self.b.call('execute',args,True)['status'],'uncertain')
        self.provider.remote_status='COMPLETED'
        self.assertEqual(self.b.call('status',{})['sessions'],[])
        with self.assertRaises(ValueError):self.b.call('open',{'request_id':'after-uncertain-submit','url':'practice'})
        self.assertEqual(self.b.plan(p['id'])['status'],'uncertain')
        self.assertEqual(self.provider.acts,1)
    def test_definitive_creation_rejection_does_not_block_or_retry_same_request(self):
        self.b.call('close',{'id':self.s['id']})
        with patch.object(self.provider,'create',side_effect=CreationRejected('Rejected')) as create:
            with self.assertRaises(CreationRejected):self.b.call('open',{'request_id':'rejected-browser','url':'practice'})
            replay=self.b.call('open',{'request_id':'rejected-browser','url':'practice'})
            self.assertEqual(replay['status'],'failed');self.assertEqual(create.call_count,1)
        self.assertEqual(self.b.call('status',{})['sessions'],[])
        self.assertEqual(self.b.call('open',{'request_id':'after-rejection','url':'practice'})['status'],'open')
    def test_provider_rejections_are_credential_free_but_ambiguous_http_stays_uncertain(self):
        for code in (400,401,402,403,404,422,429,408,409,500):
            p=BrowserbaseProvider()
            error=urllib.error.HTTPError('https://api.browserbase.com',code,'PRIVATE_PROVIDER_DETAILS',None,None)
            expected=CreationRejected if code in (400,401,402,403,404,422,429) else urllib.error.HTTPError
            with patch.object(p,'api',side_effect=error):
                with self.assertRaises(expected) as caught:p.create('provider-rejection')
            if expected is CreationRejected:self.assertNotIn('PRIVATE_PROVIDER_DETAILS',str(caught.exception))
    def test_takeover_not_exposed_to_model(self):
        with self.assertRaises(ValueError):self.b.call('takeover',{'id':self.s['id']})
    def test_truncated_form_cannot_be_approved(self):
        # 0.20.7: on a page with more controls than the snapshot lists, routine steps still run (the control is listed);
        # a final/commit/protected step, whose review must be complete, is refused.
        self.provider.snap['controls_truncated']=True
        self.assertEqual(self.prepare()['status'],'prepared')
        with self.assertRaises(ValueError):self.prepare('final-0001',[{'kind':'click','ref':'e1'}])
        self.provider.snap['controls_truncated']=False;self.provider.snap['controls'][0]['value_truncated']=True
        with self.assertRaises(ValueError):self.prepare('final-0002',[{'kind':'click','ref':'e1'}])
    def test_snapshot_find_is_passed_to_the_driver(self):
        seen=[]
        orig=self.provider.run
        def run(s,op,**kw):seen.append((op,kw.get('find')));return orig(s,op,**kw)
        self.provider.run=run
        self.b.call('snapshot',{'id':self.s['id'],'find':'sort'})
        self.assertIn(('snapshot','sort'),seen)
    def test_manual_secret_fields_require_owner_completion(self):
        self.provider.snap['controls'][0]['value']='[manual entry only]'
        with self.assertRaises(ValueError):self.prepare(steps=[{'kind':'click','ref':'e1'}])
    def test_cvs_birth_date_display_toggle_needs_review_and_does_not_unlock_other_clicks(self):
        self.provider.snap['controls']=[control(kind='password',label='Date of birth')|{'value':'[manual entry only]'},
            control('e1',kind='checkbox',label='Show date of birth'),control('e2',kind='button',label='Next')]
        with self.b.db() as db:db.execute('UPDATE sessions SET origin=? WHERE id=?',('https://www.cvs.com',self.s['id']))
        plan=self.prepare(steps=[{'kind':'click','ref':'e1'}])
        self.assertEqual(plan['status'],'prepared');self.assertEqual(self.provider.acts,0)
        with self.assertRaises(ValueError):self.prepare(rid='next-not-unlocked',steps=[{'kind':'click','ref':'e2'}])
        with self.b.db() as db:db.execute('UPDATE sessions SET origin=? WHERE id=?',('https://other.example',self.s['id']))
        with self.assertRaises(ValueError):self.prepare(rid='foreign-toggle',steps=[{'kind':'click','ref':'e1'}])
    def test_model_cannot_supply_its_own_selector(self):
        with self.assertRaises(ValueError):self.prepare(steps=[{'kind':'fill','ref':'e0','value':'Test','selector':'input'}])
        self.assertEqual(self.provider.acts,0)
    def test_inert_link_can_be_prepared_without_accepting_executable_javascript(self):
        c=control('e2',kind='',label='Continue as a guest')|{'tag':'a','href':'javascript:void(0)','form_action':''}
        self.provider.snap['controls'].append(c)
        p=self.prepare(rid='inert-guest-link',steps=[{'kind':'click','ref':'e2'}])
        self.assertEqual(p['status'],'prepared');self.assertEqual(self.provider.acts,0)
        for href in ['javascript:sendCode()','javascript:void(fetch("https://other.example"))','data:text/html,test']:
            c['href']=href
            with self.assertRaises(ValueError):self.prepare(rid='reject-script-link',steps=[{'kind':'click','ref':'e2'}])
    def test_private_selectors_are_saved_but_not_returned(self):
        self.provider.snap['controls'][0]['selector']='input:nth-of-type(1)'
        p=self.prepare();self.assertNotIn('selector',p['plan']['steps'][0]['control'])
        stored=json.loads(self.b.plan(p['id'])['plan']);self.assertEqual(stored['steps'][0]['control']['selector'],'input:nth-of-type(1)')
        self.assertNotIn('selector',self.b.call('snapshot',{'id':self.s['id']})['snapshot']['controls'][0])
    def test_private_destinations_and_email_sites_rejected(self):
        for target in ['file:///etc/passwd','http://example.com','https://user:pass@example.com','https://mail.google.com','https://www.icloud.com','https://localhost']:
            with self.subTest(target=target),self.assertRaises(ValueError):url(target,resolve=False)
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('169.254.169.254',443))]):
            with self.assertRaises(ValueError):url('https://example.com')

if __name__=='__main__':unittest.main()
