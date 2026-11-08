"""Autonomous task mode: one owner approval to start, one before money is spent."""
import json
import tempfile
import unittest
from pathlib import Path
from broker import Broker,driver_result
from task_policy import autonomous_decision,normalize_task,order_totals
from test_broker import Provider,control
from test_tasks import CONTEXT

ORIGIN='https://www.dominos.com'
MENU=ORIGIN+'/en/pages/order/menu'
CHECKOUT=ORIGIN+'/en/pages/order/checkout'

def button(ref,label,kind='button',**extra):
    return control(ref,kind,label)|{'tag':'button','role':'','form_action':'','name':'','id':''}|extra

def spec(**extra):
    base={'mode':'autonomous','origin':ORIGIN,'fields':[],'options':['Delivery','Large'],
          'facts':['123 Main St','Apt 4','31901'],'spend_limit_usd':40.0,'read_urls':[],'clicks':[]}
    return base|extra

def plan(step,url=MENU,**extra):
    return {'url':url,'steps':[step],'page_errors':[],'cookie_notice':False,'detected_total':None}|extra

def click(label,kind='button',role=''):
    return {'kind':'click','control':button('e1',label,kind,role=role)}

class PolicyTests(unittest.TestCase):
    def decide(self,step,**extra):return autonomous_decision(spec(),plan(step,**extra))
    def test_ordinary_browsing_is_routine(self):
        for label in ['Add to Order','Specialty Pizzas','Continue','Next','Checkout','Proceed to Checkout','Large (14")','Pepperoni']:
            self.assertEqual(self.decide(click(label)),(True,'routine'),label)
        self.assertEqual(self.decide({'kind':'click','control':button('e1','Medium',role='option')}),(True,'routine'))
    def test_cookie_banner_only_when_a_cookie_notice_is_showing(self):
        self.assertEqual(self.decide(click('Accept All'),cookie_notice=True),(True,'routine'))
        self.assertEqual(self.decide(click('Accept Cookies'),cookie_notice=True),(True,'routine'))
        self.assertEqual(self.decide(click('Accept All'))[1],'consent')
        self.assertEqual(self.decide(click('Accept and Place Order'),cookie_notice=True)[1],'final')
    def test_final_order_and_booking_always_stop(self):
        for label in ['Place Order','Place Your Order','Submit','Confirm Reservation','Complete Booking','Buy Now','Pay Now']:
            self.assertEqual(self.decide(click(label))[1],'final',label)
    def test_start_buttons_are_final_only_on_checkout_like_pages(self):
        self.assertEqual(self.decide(click('Order Now'),url=ORIGIN+'/en/'),(True,'routine'))
        self.assertEqual(self.decide(click('Book'),url='https://hotel.example/search'),(True,'routine'))
        self.assertEqual(self.decide(click('Order Now'),url=CHECKOUT)[1],'final')
        self.assertEqual(self.decide(click('Reserve'),detected_total=31.5)[1],'final')
    def test_payment_login_consent_and_removal_stop(self):
        self.assertEqual(self.decide(click('Cash','radio'))[1],'payment')
        self.assertEqual(self.decide(click('Add a tip'))[1],'payment')
        self.assertEqual(self.decide(click('Sign In'))[1],'login')
        self.assertEqual(self.decide(click('I agree to the Terms','checkbox'))[1],'checkbox')
        self.assertEqual(self.decide(click('Text me deals'))[1],'consent')
        self.assertEqual(self.decide(click('Remove'))[1],'destructive')
        self.assertEqual(self.decide(click(''))[1],'unknown')
    def test_owner_choices_unlock_listed_payment_or_radio(self):
        s=spec(options=['Delivery','Cash'])
        self.assertEqual(autonomous_decision(s,plan(click('Cash','radio'))),(True,'routine'))
        self.assertEqual(autonomous_decision(s,plan(click('Card','radio')))[1],'payment')
    def test_typing_only_owner_facts(self):
        field=control('e0','text','Street Address')
        self.assertEqual(self.decide({'kind':'fill','control':field,'value':'123 Main St'}),(True,'routine'))
        self.assertEqual(self.decide({'kind':'fill','control':field,'value':'999 Other Rd'})[1],'value_not_approved')
        self.assertEqual(self.decide({'kind':'fill','control':control('e0','text','Card Number'),'value':'123 Main St'})[1],'login')
        self.assertEqual(self.decide({'kind':'fill','control':control('e0','text','Note')|{'value':'[manual entry only]'},'value':'31901'})[1],'sensitive')
    def test_spending_limit_stops_clicks_but_not_typing(self):
        self.assertEqual(self.decide(click('Add to Order'),detected_total=45.10)[1],'over_limit')
        self.assertEqual(self.decide(click('Checkout'),detected_total=45.10)[1],'over_limit')
        self.assertEqual(self.decide(click('Add to Order'),detected_total=39.99),(True,'routine'))
        self.assertEqual(self.decide({'kind':'fill','control':control('e0','text','ZIP'),'value':'31901'},detected_total=99.0),(True,'routine'))
    def test_page_error_stops_clicks(self):
        self.assertEqual(self.decide(click('Continue'),page_errors=['Address not found'])[1],'page_error')
    def test_order_totals(self):
        self.assertEqual(order_totals('Subtotal $18.99\nDelivery Fee $5.99\nTotal\n$27.43')[0],27.43)
        self.assertEqual(order_totals('Subtotal: $1,018.50')[0],1018.5)
        self.assertEqual(order_totals('Large pizza $15.99')[0],None)

class NormalizeTests(unittest.TestCase):
    session={'id':'s1','origin':ORIGIN,'practice':0,'timeout_seconds':2700}
    def norm(self,**args):
        base={'request_id':'r'*8,'session_id':'s1','title':'Pizza','goal':'Order a large pepperoni and a 2-liter for delivery.'}
        return normalize_task(base|args,self.session,lambda u:(u,ORIGIN),lambda v,n=500:v if isinstance(v,str) else '')
    def test_autonomous_fields(self):
        s=self.norm(mode='autonomous',facts=['123 Main St'],spend_limit_usd=40,options=['Delivery'])
        self.assertEqual((s['mode'],s['facts'],s['spend_limit_usd'],s['max_steps']),('autonomous',['123 Main St'],40.0,40))
        self.assertEqual(self.norm(mode='autonomous',max_steps=60)['max_steps'],60)
    def test_rejects_secrets_cards_and_bad_limits(self):
        for bad in [{'facts':['4111 1111 1111 1111']},{'facts':['password hunter2']},{'spend_limit_usd':0},{'spend_limit_usd':'40'},{'max_steps':61}]:
            with self.assertRaises(ValueError,msg=bad):self.norm(mode='autonomous',**bad)
    def test_exact_mode_unchanged(self):
        s=self.norm();self.assertNotIn('mode',s)
        with self.assertRaises(ValueError):self.norm(facts=['x'])
        with self.assertRaises(ValueError):self.norm(max_steps=21)

class FlowTests(unittest.TestCase):
    """A simulated Domino's order: approve once, browse freely, approve the real total."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.provider=Provider();self.b=Broker(Path(self.tmp.name)/'t.sqlite3',self.provider)
        self.s=self.b.call('open',{'request_id':'pizza-open-1','url':'practice'})
        with self.b.db() as db:db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?',(ORIGIN,MENU,self.s['id']))
        self.page(MENU,'We use cookies.\nSpecialty Pizzas',[button('e0','Accept All'),button('e1','Add to Order'),button('e2','Add to Order'),
            control('e3','text','Street Address')|{'form_action':''},button('e4','Checkout')])
    def tearDown(self):self.tmp.cleanup()
    def page(self,url,text,controls):self.provider.snap.update({'url':url,'title':'Dominos','text':text,'controls':controls})
    def task(self,limit=40):
        t=self.b.call('task_prepare',{'request_id':'pizza-task-1','session_id':self.s['id'],'title':'Pizza','goal':'Large pepperoni and a 2-liter, delivery.',
            'mode':'autonomous','facts':['123 Main St'],'options':['Delivery'],'spend_limit_usd':limit})
        self.b.call('task_review',{'id':t['id']})
        self.assertIn('AUTONOMOUS TASK',self.provider.messages[-1]);self.assertIn('$%.2f'%limit,self.provider.messages[-1])
        r=self.b.tasks.row(t['id']);self.b.call('task_activate',{'id':t['id'],'hash':r['hash'][:16],'context':CONTEXT},True)
        return t['id']
    def step(self,t,rid,steps):return self.b.call('task_step',{'id':t,'runtime_context':CONTEXT,'request_id':'pizza-'+rid,'steps':steps})
    def test_whole_order_needs_two_approvals(self):
        t=self.task()
        for rid,steps in [('cookie-1',[{'kind':'click','ref':'e0'}]),('add-1',[{'kind':'click','ref':'e2'}]),
                          ('addr-1',[{'kind':'fill','ref':'e3','value':'123 Main St'}]),('checkout-1',[{'kind':'click','ref':'e4'}])]:
            self.assertEqual(self.step(t,rid,steps)['status'],'completed',rid)
        self.assertEqual(self.provider.acts,4)
        self.page(CHECKOUT,'Subtotal $24.98\nDelivery $5.99\nTotal $33.42',[button('e0','Place Order')])
        stop=self.step(t,'place-1',[{'kind':'click','ref':'e0'}])
        self.assertEqual((stop['status'],stop['stop_reason']),('needs_separate_review','final'))
        self.b.call('review',{'id':stop['action_id']})
        self.assertIn('Total on page: $33.42',self.provider.messages[-1]);self.assertIn('task: $40.00',self.provider.messages[-1])
        r=self.b.plan(stop['action_id'])
        self.assertEqual(self.b.call('execute',{'id':r['id'],'hash':r['hash'][:16],'context':CONTEXT},True)['status'],'completed')
        self.provider.snap['text']='Thank you! Your order number is 4471.'
        done=self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT})
        self.assertEqual(done['status'],'complete');self.assertIn('order number',done['result']['confirmation'])
    def test_over_limit_stops_and_final_approval_refused(self):
        t=self.task(limit=30)
        self.page(MENU,'Total $33.42',[button('e1','Add to Order')])
        stop=self.step(t,'add-over',[{'kind':'click','ref':'e1'}])
        self.assertEqual((stop['status'],stop['detected_total']),('over_limit',33.42));self.assertEqual(self.provider.acts,0)
        self.page(CHECKOUT,'Total $33.42',[button('e0','Place Order')])
        stop=self.step(t,'place-over',[{'kind':'click','ref':'e0'}])
        self.assertEqual(stop['stop_reason'],'final')
        self.b.call('review',{'id':stop['action_id']});r=self.b.plan(stop['action_id'])
        with self.assertRaisesRegex(ValueError,'above your'):self.b.call('execute',{'id':r['id'],'hash':r['hash'][:16],'context':CONTEXT},True)
        self.assertEqual(self.provider.acts,0)
    def test_unapproved_value_and_login_stop_without_acting(self):
        t=self.task()
        self.assertEqual(self.step(t,'addr-x',[{'kind':'fill','ref':'e3','value':'9 Elsewhere'}])['stop_reason'],'value_not_approved')
        self.page(MENU,'Specialty',[button('e0','Sign In')])
        stop=self.step(t,'login-x',[{'kind':'click','ref':'e0'}])
        self.assertEqual(stop['stop_reason'],'login');self.assertIn('/webtakeover '+self.s['id'],stop['reason'])
        self.assertEqual(self.provider.acts,0)
    def test_completion_requires_a_visible_confirmation(self):
        t=self.task()
        self.page(CHECKOUT,'Total $20.00',[button('e0','Place Order')])
        stop=self.step(t,'place-2',[{'kind':'click','ref':'e0'}]);self.b.call('review',{'id':stop['action_id']})
        r=self.b.plan(stop['action_id']);self.b.call('execute',{'id':r['id'],'hash':r['hash'][:16],'context':CONTEXT},True)
        self.provider.snap['text']='Something went wrong. Please try again.'
        with self.assertRaisesRegex(ValueError,'No confirmation'):self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT})
    def test_cvs_style_confirmation_completes_the_task(self):
        """Nov 4: CVS showed "Good news! The owner is all set." and the task could not be completed."""
        t=self.task()
        self.page(CHECKOUT,'Total $0.00',[button('e0','Place Order')])
        stop=self.step(t,'place-cvs',[{'kind':'click','ref':'e0'}]);self.b.call('review',{'id':stop['action_id']})
        r=self.b.plan(stop['action_id']);self.b.call('execute',{'id':r['id'],'hash':r['hash'][:16],'context':CONTEXT},True)
        self.provider.snap['text']='Good news! The owner is all set.\nWe sent the visit details to your email.'
        done=self.b.call('task_complete',{'id':t,'runtime_context':CONTEXT})
        self.assertEqual(done['status'],'complete');self.assertIn('all set',done['result']['confirmation'])
    def test_click_that_never_happened_does_not_block(self):
        t=self.task();original=self.provider.run
        def covered(s,op,**kw):
            if op=='act':self.provider.acts+=1;return {'ok':False,'kind':'not_dispatched','error_stage':'action','error_type':'timeout'}
            return original(s,op,**kw)
        self.provider.run=covered
        self.assertEqual(self.step(t,'add-covered',[{'kind':'click','ref':'e1'}])['status'],'not_dispatched')
        self.provider.run=original
        self.b.hold()  # no /webresolve needed
        self.assertEqual(self.b.tasks.row(t)['status'],'active')
        self.assertEqual(self.step(t,'add-covered',[{'kind':'click','ref':'e1'}])['status'],'not_dispatched')  # same ID never replays
        self.assertEqual(self.step(t,'add-retry',[{'kind':'click','ref':'e1'}])['status'],'completed')

    def test_half_filled_form_after_a_page_reload_does_not_freeze_the_task(self):
        """Nov 4 MyLeaveco: the absence form reloaded itself after a dropdown; the batch went 'uncertain' and the task froze."""
        t=self.task();original=self.provider.run
        def reloaded(s,op,**kw):
            if op=='act':self.provider.acts+=1;return {'ok':False,'kind':'fields_incomplete','error_stage':'action','error_type':'driver_error','reason':'page_changed','steps_done':1,'fills':[{'ref':'e3','ok':True}]}
            return original(s,op,**kw)
        self.provider.run=reloaded
        r=self.step(t,'addr-reload',[{'kind':'fill','ref':'e3','value':'123 Main St'}])
        self.assertEqual(r['status'],'fields_incomplete');self.assertIn('redo only the missing',r['recover'])
        self.provider.run=original
        self.b.hold()  # no /webresolve needed
        self.assertEqual(self.b.tasks.row(t)['status'],'active')
        self.assertEqual(self.step(t,'addr-reload',[{'kind':'fill','ref':'e3','value':'123 Main St'}])['status'],'fields_incomplete')  # same ID never replays
        self.assertEqual(self.step(t,'addr-redo',[{'kind':'fill','ref':'e3','value':'123 Main St'}])['status'],'completed')

    def test_typing_that_crashed_never_blocks_other_browsers_but_a_click_does(self):
        """Nov 4: an 'uncertain' Leaveco form fill blocked opening CVS until /webresolve, which arrived with backticks."""
        t=self.task();original=self.provider.run
        def crash(s,op,**kw):
            if op=='act':self.provider.acts+=1;raise RuntimeError('driver died')
            return original(s,op,**kw)
        self.provider.run=crash
        self.assertEqual(self.step(t,'addr-crash',[{'kind':'fill','ref':'e3','value':'123 Main St'}])['status'],'fields_incomplete')
        self.b.hold()
        # 0.20.7: a crashed ROUTINE click ("Add to Order", neutral) submitted nothing: it waits for the next page read of this
        # browser (no /webresolve, no hold on other browsers once that browser is closed). A crashed FINAL press still holds.
        r=self.step(t,'add-crash',[{'kind':'click','ref':'e1'}])
        self.assertEqual(r['status'],'verification_pending')
        with self.assertRaisesRegex(ValueError,'pending'):self.b.hold()
        self.provider.run=original
        self.b.call('snapshot',{'id':self.s['id']});self.b.hold()   # the read settles it
        self.assertEqual(self.b.plan(r['id'])['status'],'completed')
        self.provider.run=crash
        self.page(MENU,'Total $33.42',[button('e5','Place order',kind='submit')])
        p=self.b.call('prepare',{'id':self.s['id'],'request_id':'final-crash-1','steps':[{'kind':'click','ref':'e5'}]})
        self.b.call('review',{'id':p['id']});h=self.b.plan(p['id'])['hash'][:16]
        r=self.b.call('execute',{'id':p['id'],'hash':h},True)
        self.assertEqual(r['status'],'uncertain');self.provider.run=original
        self.assertIn('\n/webresolve '+r['id'],self.provider.messages[-1]);self.assertNotIn('`',self.provider.messages[-1])
        with self.assertRaisesRegex(ValueError,'unconfirmed'):self.b.hold()
        # Closing that browser does not release a final press; /webresolve does.
        self.b.call('close',{'id':self.s['id']})
        with self.assertRaisesRegex(ValueError,'unconfirmed'):self.b.hold()
        self.b.call('resolve',{'id':r['id']},True);self.b.hold()
    def test_old_uncertain_typing_is_released_on_restart(self):
        t=self.task();original=self.provider.run
        self.provider.run=lambda s,op,**kw:(_ for _ in ()).throw(RuntimeError('x')) if op=='act' else original(s,op,**kw)
        r=self.step(t,'addr-old',[{'kind':'fill','ref':'e3','value':'123 Main St'}]);self.provider.run=original
        with self.b.db() as db:db.execute("UPDATE plans SET status='uncertain' WHERE id=?",(r['id'],))
        with self.assertRaises(ValueError):self.b.hold()
        b2=Broker(self.b.path,self.provider);b2.hold()
        self.assertEqual(b2.plan(r['id'])['status'],'fields_incomplete')

class DriverProtocolV2Tests(unittest.TestCase):
    def frames(self,*items):return '\n'.join(json.dumps(x) for x in items)
    def test_no_effect_announced_means_not_dispatched(self):
        raw=self.frames({'event':'protocol','version':2},{'event':'phase','stage':'action'})
        self.assertEqual(driver_result(raw,'act',1,timed_out=True)['kind'],'not_dispatched')
    def test_announced_effect_stays_uncertain(self):
        raw=self.frames({'event':'protocol','version':2},{'event':'phase','stage':'action'},{'event':'effect_started'})
        self.assertEqual(driver_result(raw,'act',1,timed_out=True)['kind'],'unconfirmed')
    def test_old_driver_without_protocol_stays_uncertain(self):
        self.assertEqual(driver_result(self.frames({'event':'phase','stage':'action'}),'act',1,timed_out=True)['kind'],'unconfirmed')
    def test_no_stage_reached_stays_uncertain(self):
        self.assertEqual(driver_result(self.frames({'event':'protocol','version':2}),'act',1,timed_out=True)['kind'],'unconfirmed')

if __name__=='__main__':unittest.main()
