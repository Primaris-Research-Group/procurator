"""0.19.2 (Nov 6 compatibility run, finding 2): research = read-only task. The review says exactly what it allows, and the
broker refuses carts, bookings, sign-in, uploads and submit/final buttons without offering a review."""
import json
import tempfile
import unittest
from pathlib import Path
from broker import Broker
from test_broker import Provider
from task_policy import normalize_task, preview

CONTEXT={'agentId':'main','sessionKey':'agent:main:telegram:direct:100000001','sessionId':'test-session','accountId':'default'}
def ctl(ref,tag,typ,label):
    return {'ref':ref,'tag':tag,'type':typ,'name':label.lower(),'id':'','label':label,'href':'','form_action':'about:blank','disabled':False,'checked':False,'value':'','options':[]}

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Provider();self.b=Broker(Path(self.tmp.name)/'t.sqlite3',self.p)
        self.p.snap={'url':'about:blank','title':'Shop','text':'We use cookies.\nUSB-C cables','frame_count':0,
            'controls':[ctl('e0','input','search','Search'),ctl('e1','button','submit','Search'),ctl('e2','button','button','Add to cart'),
                        ctl('e3','button','submit','Submit order'),ctl('e4','button','button','Accept all cookies'),ctl('e5','button','button','Sign in'),
                        ctl('e6','button','button','Next page'),ctl('e7','input','text','Name')]}
        self.s=self.b.call('open',{'request_id':'practice-research','url':'practice'})
        t=self.b.call('task_prepare',{'request_id':'research-1','session_id':self.s['id'],'title':'Cable price','goal':'Read the price of a USB-C cable.',
            'mode':'research','facts':['USB-C cable 6 ft']})
        self.b.call('task_review',{'id':t['id']});r=self.b.tasks.row(t['id'])
        self.b.call('task_activate',{'id':t['id'],'hash':r['hash'][:16],'context':CONTEXT},True);self.id=t['id']
    def tearDown(self):self.tmp.cleanup()
    def step(self,rid,steps):return self.b.call('task_step',{'id':self.id,'runtime_context':CONTEXT,'request_id':'research-step-'+rid,'steps':steps})

    def test_review_text_matches_what_the_grant_allows(self):
        spec=json.loads(self.b.tasks.row(self.id)['spec'])
        self.assertEqual(spec['mode'],'research');self.assertFalse(spec['fill_form'])
        text=preview(spec)
        self.assertIn('READ-ONLY TASK',text);self.assertIn('Refused by the server',text);self.assertIn('"USB-C cable 6 ft"',text)
        self.assertNotIn('add to cart, press',text.lower())
        msg=next(m for m in self.p.messages if 'BROWSER TASK REVIEW' in m);self.assertIn('READ-ONLY TASK',msg)

    def test_research_cannot_carry_buying_or_booking_settings(self):
        sess={'id':'a'*16,'origin':'https://www.example.com','practice':0,'timeout_seconds':900}
        url=lambda v:(v,'https://www.example.com');txt=lambda v,n=600:str(v)[:n]
        for extra in ({'spend_limit_usd':20},{'free_booking_auto_confirm':True,'limits':{'dates':['2026-11-13']}},{'fill_form':True},{'upload_files':['a.pdf']}):
            with self.assertRaisesRegex(ValueError,'read-only'):
                normalize_task({'request_id':'x'*8,'session_id':'a'*16,'title':'t','goal':'g','mode':'research',**extra},sess,url,txt)

    def test_search_and_routine_browsing_run_without_review(self):
        acts=self.p.acts
        self.assertEqual(self.step('r1',[{'kind':'fill','ref':'e0','value':'USB-C cable 6 ft'}])['status'],'completed')
        self.assertEqual(self.step('r2',[{'kind':'click','ref':'e1'}])['status'],'completed')
        self.assertEqual(self.step('r3',[{'kind':'click','ref':'e6'}])['status'],'completed')
        self.assertEqual(self.step('r4',[{'kind':'click','ref':'e4'}])['status'],'completed')   # cookie banner
        # 0.20.7: a search box takes the model's own wording (the task listed "USB-C cable 6 ft", it may search "usb c cable").
        self.assertEqual(self.step('r5',[{'kind':'fill','ref':'e0','value':'anker usb c cable'}])['status'],'completed')
        self.assertEqual(self.p.acts,acts+5)

    def test_carts_submits_sign_in_and_unlisted_values_are_refused_without_a_review(self):
        acts,msgs=self.p.acts,len(self.p.messages)
        for rid,steps in (('n1',[{'kind':'click','ref':'e2'}]),('n2',[{'kind':'click','ref':'e3'}]),('n3',[{'kind':'click','ref':'e5'}]),
                          ('n4',[{'kind':'fill','ref':'e7','value':'my home address'}]),('n5',[{'kind':'fill','ref':'e0','value':'send it to me@example.com'}])):
            out=self.step(rid,steps)
            self.assertEqual(out['status'],'refused_read_only',rid);self.assertIn('do not send',out['reason'].lower())
            self.assertNotIn('action_id',out)
        self.assertEqual(self.p.acts,acts);self.assertEqual(len(self.p.messages),msgs)
        with self.b.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) c FROM plans WHERE task_id=? AND status='prepared'",(self.id,)).fetchone()['c'],0)

    def test_a_research_task_completes_with_its_answer_and_page(self):
        self.step('r1',[{'kind':'fill','ref':'e0','value':'USB-C cable 6 ft'}])
        out=self.b.call('task_complete',{'id':self.id,'runtime_context':CONTEXT,'answer':'Anker USB-C cable 6 ft, $9.99 (not sponsored).'})
        self.assertEqual(out['status'],'complete')
        res=json.loads(self.b.tasks.row(self.id)['result'])
        self.assertTrue(res['research']);self.assertIn('$9.99',res['answer']);self.assertEqual(res['title'],'Shop');self.assertEqual(res['lane'],'cloud')
        self.assertEqual(self.b.session(self.s['id'])['status'],'closed')

class LivePageTests(unittest.TestCase):
    """0.20.7: a page whose text changes between the read and the act (timers, ads, live results) no longer makes routine
    steps stale; a final step still needs the exact page."""
    class LiveProvider(Provider):
        def run(self,s,operation,**kwargs):
            self.snap['text']=self.snap.get('text','')+' tick'
            return super().run(s,operation,**kwargs)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=self.LiveProvider();self.b=Broker(Path(self.tmp.name)/'t.sqlite3',self.p)
        self.p.snap={'url':'https://www.opentable.com/s?dateTime=2026-11-13T19%3A00','title':'Restaurants','text':'Results','frame_count':0,
            'controls':[ctl('e0','input','search','Please input a Location, Restaurant or Cuisine'),ctl('e1','button','button','Date selector'),ctl('e2','button','submit','Submit order')]}
        self.s=self.b.call('open',{'request_id':'practice-live','url':'practice'})
        t=self.b.call('task_prepare',{'request_id':'research-live','session_id':self.s['id'],'title':'Tables','goal':'Find tables.','mode':'research','facts':['Midtown Atlanta']})
        self.b.call('task_review',{'id':t['id']});r=self.b.tasks.row(t['id'])
        self.b.call('task_activate',{'id':t['id'],'hash':r['hash'][:16],'context':CONTEXT},True);self.id=t['id']
    def tearDown(self):self.tmp.cleanup()
    def step(self,rid,steps):return self.b.call('task_step',{'id':self.id,'runtime_context':CONTEXT,'request_id':'live-step-'+rid,'steps':steps})
    def test_routine_steps_run_on_a_changing_page(self):
        self.assertEqual(self.step('a',[{'kind':'fill','ref':'e0','value':'Midtown Atlanta'}])['status'],'completed')
        self.assertEqual(self.step('b',[{'kind':'click','ref':'e1'}])['status'],'completed')
        self.assertEqual(self.p.acts,2)
    def test_a_routine_action_left_unconfirmed_in_a_closed_browser_never_blocks_the_next_one(self):
        original=self.p.run
        def crash(s,op,**kw):
            if op=='act':self.p.acts+=1;raise RuntimeError('driver died')
            return original(s,op,**kw)
        self.p.run=crash
        r=self.step('x',[{'kind':'click','ref':'e1'}])
        self.assertEqual(r['status'],'verification_pending');self.p.run=original
        self.b.call('close',{'id':self.s['id']})
        self.b.hold()
        self.assertEqual(self.b.plan(r['id'])['status'],'acknowledged_uncertain')
        self.assertEqual(self.b.call('open',{'request_id':'practice-next','url':'practice'})['status'],'open')
    def test_a_final_step_still_needs_the_exact_page(self):
        from broker import staleness_mode
        self.assertEqual(staleness_mode({'steps':[{'kind':'click','target_class':{'class':'final'}}]}),'full')
        self.assertEqual(staleness_mode({'steps':[{'kind':'click','target_class':{'class':'neutral'}}]},approved=True),'full')
        self.assertEqual(staleness_mode({'steps':[{'kind':'navigate','url':'x'}]}),'none')
        self.assertEqual(staleness_mode({'steps':[{'kind':'fill','control':{}}]}),'page')
        p=self.b.call('prepare',{'id':self.s['id'],'request_id':'final-live','steps':[{'kind':'click','ref':'e2'}]})
        self.b.call('review',{'id':p['id']});r=self.b.plan(p['id'])
        self.assertEqual(self.b.call('execute',{'id':p['id'],'hash':r['hash'][:16]},True)['status'],'stale')

if __name__=='__main__':
    unittest.main()


class ReadOnlyLabels(unittest.TestCase):
    def test_social_and_messaging_buttons_are_refused_in_research(self):
        from task_policy import READ_ONLY_REFUSE
        for label in ('Post', 'Reply', 'Add comment', 'Vote', 'Follow', 'Unsubscribe', 'Delete', 'Send message', 'Contact seller', 'Write a review', 'Add to cart', 'Book now'):
            self.assertTrue(READ_ONLY_REFUSE.search(label), label)
        for label in ('Search', 'Next page', 'Apply filters', 'Show more', 'Close', 'No thanks', 'Sort by price', 'View details'):
            self.assertFalse(READ_ONLY_REFUSE.search(label), label)


class TaskButtons(unittest.TestCase):
    """Nov 6: the task review carries one-tap copy buttons for the exact /webtask line (the owner still sends it themselves)."""
    def test_review_paused_and_started_messages_carry_copy_buttons(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=Provider();sent=[]
        p.telegram_buttons=lambda message,rows:(sent.append((message,rows)),p.messages.append(message))[-1]
        b=Broker(Path(tmp.name)/'t.sqlite3',p)
        p.snap={'url':'about:blank','title':'Form','text':'Form','frame_count':0,'controls':[ctl('e0','input','text','Name')]}
        s=b.call('open',{'request_id':'practice-buttons','url':'practice'})
        t=b.call('task_prepare',{'request_id':'buttons-1','session_id':s['id'],'title':'Practice','goal':'Fill the practice form.','mode':'autonomous','facts':['Name Deploy Test']})
        b.call('task_review',{'id':t['id']});r=b.tasks.row(t['id'])
        msg,rows=next(x for x in sent if x[0].startswith('BROWSER TASK REVIEW'))
        flat=[x for row in rows for x in row]
        approve=next(x for x in flat if x['text']=='✅ Copy approval line')
        self.assertEqual(approve['copy_text']['text'],'/webtask '+t['id']+' '+r['hash'][:16])
        self.assertIn(approve['copy_text']['text'],msg)                     # the same line is still in the text
        self.assertIn('/webstop '+t['id'],[x['copy_text']['text'] for x in flat if 'copy_text' in x])
        self.assertTrue(all(set(x)<={'text','copy_text','url'} for x in flat))  # copy or link only: no button can approve
        paused=b.tasks.buttons(t['id'],json.loads(r['spec']),paused=True)
        self.assertEqual([x['copy_text']['text'] for x in paused[0]],['/webcontinue '+t['id'],'/webtakeover '+s['id'],'/webstop '+t['id']])
