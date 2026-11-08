import base64
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
import relay

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.calls=[];self.fail=False;self.refused={}
        def sender(wire,recipients,password):
            self.calls.append((wire,recipients,password))
            if self.fail:raise TimeoutError()
            return self.refused
        self.sender=sender;self.worker=relay.Relay(self.directory,sender)
        self.raw=self.message();self.request=self.payload(self.raw)
    def message(self,account=relay.ACCOUNT,body='Exact approved body'):
        m=EmailMessage(policy=policy.SMTP);m['From']=account;m['To']='owner@example.com';m['Cc']='copy@example.com';m['Bcc']='blind@example.com';m['Subject']='Test';m['Message-ID']='<procurator-aaaaaaaaaaaaaaaa@icloud.com>';m.set_content(body);return m.as_bytes()
    def payload(self,raw):return {'action':'send','id':'a'*16,'raw':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest(),'password':'private-app-password'}
    def test_envelope_includes_bcc_but_recipient_headers_do_not(self):
        result=self.worker.execute(self.request);self.assertEqual(result['status'],'sent')
        wire,recipients,_=self.calls[0];m=BytesParser(policy=policy.default).parsebytes(wire)
        self.assertNotIn('Bcc',m);self.assertEqual(recipients,['owner@example.com','copy@example.com','blind@example.com'])
        self.assertEqual(m.get_content().strip(),'Exact approved body')
    def test_restart_and_repeat_return_same_receipt_without_resend(self):
        first=self.worker.execute(self.request)
        restarted=relay.Relay(self.directory,self.sender)
        self.assertEqual(first,restarted.execute(self.request));self.assertEqual(len(self.calls),1)
    def test_ambiguous_send_and_status_never_retry(self):
        self.fail=True;self.assertEqual(self.worker.execute(self.request)['status'],'send_uncertain')
        self.fail=False;self.assertEqual(self.worker.execute(self.request)['status'],'send_uncertain')
        self.assertEqual(self.worker.execute({'action':'status','id':'a'*16})['status'],'send_uncertain')
        self.assertEqual(len(self.calls),1)
    def test_crash_during_send_remains_uncertain(self):
        with self.worker.db() as db:db.execute('INSERT INTO actions VALUES(?,?,?,?)',('a'*16,self.request['sha256'],'sending',None))
        self.assertEqual(self.worker.execute(self.request)['status'],'send_uncertain');self.assertFalse(self.calls)
    def test_partial_delivery_is_not_reported_as_complete_or_retried(self):
        self.refused={'blind@example.com':(550,b'Refused')}
        result=self.worker.execute(self.request);self.assertEqual(result['status'],'partially_sent')
        self.assertEqual(result['receipt']['refused_recipients'],['blind@example.com'])
        self.worker.execute(self.request);self.assertEqual(len(self.calls),1)
    def test_hash_and_sender_and_id_collisions_rejected(self):
        with self.assertRaises(ValueError):self.worker.execute({**self.request,'sha256':'0'*64})
        with self.assertRaises(ValueError):self.worker.execute(self.payload(self.message(account='other@icloud.com')))
        with self.assertRaises(ValueError):self.worker.execute({**self.request,'smtp_host':'attacker.example'})
        self.worker.execute(self.request)
        with self.assertRaises(ValueError):self.worker.execute(self.payload(self.message(body='Changed')))
        self.assertEqual(len(self.calls),1)
    def test_check_only_authenticates_and_credentials_are_not_persisted(self):
        self.assertEqual(self.worker.execute({'action':'check','password':'private-app-password'})['status'],'ready')
        self.assertIsNone(self.calls[0][0])
        self.worker.execute(self.request)
        for p in self.directory.iterdir():self.assertNotIn(b'private-app-password',p.read_bytes())
        self.assertNotIn(b'Exact approved body',(self.directory/'outbox.sqlite3').read_bytes())
    def test_smtp_requires_verified_starttls_and_ignores_quit_error_after_acceptance(self):
        seen=[]
        class SMTP:
            def __init__(self,host,port,timeout):seen.append((host,port))
            def ehlo(self):seen.append('ehlo')
            def starttls(self,context):
                assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED
                seen.append('verified_tls')
            def login(self,account,password):seen.append(('login',account))
            def sendmail(self,account,recipients,wire):seen.append('send');return {}
            def quit(self):raise OSError()
            def close(self):seen.append('close')
        with patch.object(relay.smtplib,'SMTP',SMTP):self.assertEqual(relay.smtp_send(b'body',['owner@example.com'],'private'),{})
        self.assertLess(seen.index('verified_tls'),seen.index(('login',relay.ACCOUNT)))
        self.assertLess(seen.index(('login',relay.ACCOUNT)),seen.index('send'))

if __name__=='__main__':unittest.main()
