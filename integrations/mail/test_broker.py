import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('broker',Path(__file__).with_name('broker.py'))
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Fake:
    def __init__(self):
        self.sent=[]
        self.reviews=[]
        self.fail=False
        self.found=None
        self.icloud_ready=False
        self.copies=[]
        self.copy_fail=False
        self.icloud_sends=[]
    def gmail_ready(self): return True
    def icloud_send_ready(self): return self.icloud_ready
    def icloud_copy(self,raw,mid,kind,append):
        self.copies.append((kind,append))
        if self.copy_fail:raise TimeoutError()
        return {'folder':kind,'uid':'123','uidvalidity':'55','content_verified':True}
    def send_icloud(self,identifier,raw):
        self.icloud_sends.append(raw)
        if self.fail:raise TimeoutError()
        return {'status':'sent','receipt':{'id':identifier}}
    def icloud_send_status(self,identifier):
        return {'status':'sent','receipt':{'id':identifier}} if self.found else {'status':'send_uncertain'}
    def telegram(self,text): self.reviews.append(text)
    def send(self,raw):
        self.sent.append(raw)
        if self.fail: raise TimeoutError()
        return 'gmail-receipt'
    def reconcile(self,mid): return self.found

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider=Fake()
        self.b=m.Broker(Path(self.temp.name)/'test.db',self.provider)
        self.args={'request_id':'test-action-001','account':'gmail','to':['owner@example.com'],'cc':['copy@example.com'], 'bcc':['blind@example.com'],'subject':'Test subject','body':'Exact body\nline two'}
    def create(self):return self.b.call('create',self.args)
    def review(self):
        draft=self.create()
        self.b.call('review',{'id':draft['id']})
        return {'id':draft['id'],'hash':draft['hash']}
    def test_exact_immutable_draft_idempotency(self):
        a=self.create()
        self.assertEqual(a,self.create())
        self.args['body']='changed'
        with self.assertRaisesRegex(ValueError,'different content'):self.create()
    def test_no_model_approval_even_after_review(self):
        data=self.review()
        with self.assertRaisesRegex(ValueError,'authenticated Telegram'):self.b.call('send',data)
        self.assertEqual(self.provider.sent,[])
    def test_native_send_requires_review_and_matching_hash(self):
        draft=self.create()
        with self.assertRaisesRegex(ValueError,'fresh exact'):self.b.call('send',{'id':draft['id'],'hash':draft['hash']},True)
        data=self.review()
        with self.assertRaisesRegex(ValueError,'does not match'):self.b.call('send',{**data,'hash':'0'*16},True)
    def test_exact_mime_once_and_all_recipients_previewed(self):
        data=self.review()
        for field in ('to','cc','bcc'):
            self.assertIn(self.args[field][0],self.provider.reviews[0])
        self.assertIn(self.args['body'],self.provider.reviews[0])
        first=self.b.call('send',data,True)
        self.assertEqual(first['status'],'sent')
        again=self.b.call('send',data,True)
        self.assertEqual(len(self.provider.sent),1)
        self.assertTrue(again['already']); self.assertNotIn('already',first)   # 0.24.3 (B17): the replay is labelled
        msg=m.BytesParser(policy=m.email.policy.default).parsebytes(self.provider.sent[0])
        self.assertEqual(str(msg['subject']),self.args['subject'])
        self.assertEqual(msg.get_content().replace('\r\n','\n').rstrip('\n'),self.args['body'])
        self.assertEqual(str(msg['bcc']),'blind@example.com')
    def test_ambiguous_send_never_retried_and_can_reconcile(self):
        data=self.review()
        self.provider.fail=True
        self.assertEqual(self.b.call('send',data,True)['status'],'send_uncertain')
        self.b.call('send',data,True)
        self.assertEqual(len(self.provider.sent),1)
        self.provider.found='reconciled-id'
        self.assertEqual(self.b.call('get',{'id':data['id']})['status'],'sent')
    def test_expired_and_cancelled_reviews_cannot_send(self):
        data=self.review()
        with patch.object(m.time,'time',return_value=time.time()+1900):
            with self.assertRaisesRegex(ValueError,'fresh exact'):self.b.call('send',data,True)
        self.b.call('cancel',{'id':data['id']})
        with self.assertRaisesRegex(ValueError,'fresh exact'):self.b.call('send',data,True)
    def test_crash_sending_becomes_uncertain(self):
        data=self.review()
        with self.b.db() as db:db.execute("UPDATE drafts SET status='sending' WHERE id=?",(data['id'],))
        restarted=m.Broker(self.b.path,self.provider)
        self.assertEqual(restarted.call('send',data,True)['status'],'send_uncertain')
        self.assertFalse(self.provider.sent)
    def test_icloud_send_blocked(self):
        self.args['account']='icloud'
        draft=self.create()
        with self.assertRaisesRegex(ValueError,'iCloud sending'):self.b.call('review',{'id':draft['id']})
        with self.assertRaises(ValueError):self.b.call('send',{'id':draft['id'],'hash':draft['hash']},True)
    def test_native_icloud_copy_is_immutable_and_replayed_without_append(self):
        self.args['account']='icloud'
        d=self.create()
        a=self.b.call('save_icloud_draft',{'id':d['id']})
        b=self.b.call('save_icloud_draft',{'id':d['id']})
        self.assertEqual(a['status'],'saved')
        self.assertTrue(b['replayed'])
        self.assertEqual(self.provider.copies,[('Drafts',True)])
        self.assertFalse(self.provider.sent)
    def test_native_icloud_copy_uncertainty_never_reappends(self):
        self.args['account']='icloud'
        d=self.create();self.provider.copy_fail=True
        self.assertEqual(self.b.call('save_icloud_draft',{'id':d['id']})['status'],'copy_uncertain')
        self.assertEqual(self.b.call('save_icloud_draft',{'id':d['id']})['status'],'copy_uncertain')
        self.assertEqual(self.provider.copies,[('Drafts',True),('Drafts',False)])
    def test_gmail_draft_cannot_be_saved_to_icloud(self):
        d=self.create()
        with self.assertRaises(ValueError):self.b.call('save_icloud_draft',{'id':d['id']})
        self.assertFalse(self.provider.copies)
    def test_icloud_send_requires_native_approval_and_verified_relay(self):
        self.args['account']='icloud';self.provider.icloud_ready=True
        data=self.review()
        with self.assertRaises(ValueError):self.b.call('send',data)
        self.assertEqual(self.b.call('send',data,True)['status'],'sent')
        self.b.call('send',data,True)
        self.assertEqual(len(self.provider.icloud_sends),1)
        self.assertEqual(self.provider.copies,[('Sent',True)])
    def test_icloud_ambiguous_send_reconciles_relay_record_without_retry(self):
        self.args['account']='icloud';self.provider.icloud_ready=True
        data=self.review();self.provider.fail=True
        self.assertEqual(self.b.call('send',data,True)['status'],'send_uncertain')
        self.b.call('send',data,True);self.assertEqual(len(self.provider.icloud_sends),1)
        self.provider.found=True
        self.assertEqual(self.b.call('get',{'id':data['id']})['status'],'sent')
        self.assertEqual(len(self.provider.icloud_sends),1)
    def test_icloud_sent_copy_failure_does_not_erase_smtp_receipt(self):
        self.args['account']='icloud';self.provider.icloud_ready=True
        data=self.review();self.provider.copy_fail=True
        result=self.b.call('send',data,True)
        self.assertEqual(result['status'],'sent')
        self.assertEqual(result['sent_copy']['status'],'copy_uncertain')
        self.assertTrue(result['receipt'])

    def test_telegram_review_limit_counts_surrogate_pairs(self):
        with self.assertRaisesRegex(ValueError,'too long'):
            m.normalize({**self.args,'body':'😀'*2000})

    def test_reject_header_injection_unknown_fields_and_deceptive_text(self):
        for key,value in [('to',['a@example.com\nBcc: bad@example.com']),('subject','Hello\nBcc: bad'),('body','test\u202e')]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):m.normalize({**self.args,key:value})
        with self.assertRaises(ValueError):m.normalize({**self.args,'approved':True})

if __name__=='__main__':unittest.main()

class ManageFake(Fake):
    def __init__(self):
        super().__init__();self.modify=True;self.gm=[];self.ic=[]
    def gmail_modify_ready(self):return self.modify
    def gmail_manage(self,ids,op):self.gm.append((tuple(ids),op));return ids
    def gmail_headers(self,i):return {'id':i,'from':'News <n@example.com>','subject':'Weekly','date':'Mon'}
    def icloud_manage(self,f,v,u,op):self.ic.append((f,v,tuple(u),op));return {'changed':u,'missing':[],'folder_name':'INBOX','action':op}
    def icloud_headers(self,f,v,u):return {'folder_name':'Junk','messages':[{'uid':x,'from':'Spam <s@x.com>','subject':'Win','date':'Tue'} for x in u if x!='9'],'missing':['9'] if '9' in u else []}

class ManageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.p=ManageFake();self.b=m.Broker(Path(self.temp.name)/'t.db',self.p)
        self.ic={'account':'icloud','folder_id':'f_0123456789abcdef','uidvalidity':'77','uids':['12','10','9']}
    def test_flags_and_junk_moves_apply_immediately_and_validate_selection(self):
        r=self.b.call('manage',{**self.ic,'action':'mark_read'});self.assertEqual(r['action'],'mark_read');self.assertEqual(self.p.ic[-1][2],('9','10','12'))
        r=self.b.call('manage',{'account':'gmail','action':'junk','message_ids':['18f3a2b4c5d6e7f8']});self.assertEqual(self.p.gm[-1],(('18f3a2b4c5d6e7f8',),'junk'))
        for bad in [{**self.ic,'action':'trash'},{**self.ic,'action':'delete'},{**self.ic,'action':'mark_read','uids':['0']},{**self.ic,'action':'mark_read','folder_id':'INBOX; DELETE'},
                    {'account':'gmail','action':'mark_read','message_ids':['../x']},{'account':'gmail','action':'mark_read','message_ids':[str(i)*12 for i in range(51)]}]:
            with self.assertRaises(ValueError):self.b.call('manage',bad)
        self.p.modify=False
        with self.assertRaisesRegex(ValueError,'modify permission'):self.b.call('manage',{'account':'gmail','action':'mark_read','message_ids':['18f3a2b4c5d6e7f8']})
    def test_trash_requires_owner_review_exact_hash_and_is_not_replayed(self):
        r=self.b.call('trash_request',self.ic);self.assertEqual(r['count'],2);self.assertFalse(r['changed'])
        review=self.p.reviews[-1];self.assertIn('MOVE TO TRASH REVIEW',review);self.assertIn('Spam <s@x.com> — Win',review);self.assertIn('Nothing is permanently deleted',review)
        line=[l for l in review.splitlines() if l.startswith('/mailtrash ')][0];_,ident,code=line.split()
        self.assertEqual(self.p.ic,[])
        with self.assertRaisesRegex(ValueError,'authenticated Telegram'):self.b.call('trash',{'id':ident,'hash':code})
        with self.assertRaisesRegex(ValueError,'does not match'):self.b.call('trash',{'id':ident,'hash':'0'*16},privileged=True)
        done=self.b.call('trash',{'id':ident,'hash':code},privileged=True);self.assertEqual(done['status'],'done')
        self.assertEqual(self.p.ic,[('f_0123456789abcdef','77',('10','12'),'trash')])
        again=self.b.call('trash',{'id':ident,'hash':code},privileged=True);self.assertTrue(again['replayed']);self.assertEqual(len(self.p.ic),1)
    def test_trash_review_expires(self):
        self.b.call('trash_request',{'account':'gmail','message_ids':['18f3a2b4c5d6e7f8']})
        _,ident,code=[l for l in self.p.reviews[-1].splitlines() if l.startswith('/mailtrash ')][0].split()
        with patch.object(m.time,'time',return_value=time.time()+3600):
            with self.assertRaisesRegex(ValueError,'expired'):self.b.call('trash',{'id':ident,'hash':code},privileged=True)
        self.assertEqual(self.p.gm,[])
