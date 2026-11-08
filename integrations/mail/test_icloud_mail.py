import contextlib
import unittest
from unittest.mock import patch
from pathlib import Path
from icloud_mail import ICloudMailbox

RAW=b'From: owner@icloud.example\r\nTo: owner@example.com\r\nSubject: Test\r\nMessage-ID: <procurator-aaaaaaaaaaaaaaaa@icloud.com>\r\n\r\nBody\r\n'
MID='<procurator-aaaaaaaaaaaaaaaa@icloud.com>'

class Fake:
    def __init__(self):self.raw=None;self.appends=0;self.readonly=[];self.fetches=[]
    def list(self):return 'OK',[b'() "/" "Drafts"',b'(\\Sent) "/" "Sent Messages"']
    def select(self,folder,readonly=False):self.readonly.append(readonly);return 'OK',[b'1']
    def response(self,name):return name,[b'55']
    def append(self,folder,flags,date,raw):
        assert folder==b'"Drafts"';assert flags=='(\\Draft)'
        self.appends+=1;self.raw=raw;return 'OK',[b'Saved']
    def uid(self,command,*args):
        if command=='search':return 'OK',[b'9' if self.raw else b'']
        self.fetches.append(args[-1])
        if args[-1]=='(RFC822.SIZE)':return 'OK',[b'1 (RFC822.SIZE '+str(len(self.raw)).encode()+b')']
        return 'OK',[(b'1 BODY[]',self.raw)]

class Tests(unittest.TestCase):
    def setup_mail(self,fake):
        mailbox=ICloudMailbox(Path('/unused'))
        @contextlib.contextmanager
        def connection():yield fake
        mailbox.connect=connection
        return mailbox
    def test_existing_native_drafts_folder_append_and_exact_readback(self):
        fake=Fake();mail=self.setup_mail(fake)
        result=mail.copy(RAW,MID,append=True)
        self.assertEqual(result['uid'],'9');self.assertTrue(result['content_verified'])
        self.assertEqual(fake.appends,1);self.assertTrue(all(fake.readonly))
        self.assertIn('(BODY.PEEK[])',fake.fetches)
        mail.copy(RAW,MID,append=True);self.assertEqual(fake.appends,1)
    def test_only_observed_single_terminal_crlf_normalization_is_allowed(self):
        fake=Fake();fake.raw=RAW+b'\r\n'
        self.assertTrue(self.setup_mail(fake).copy(RAW,MID,append=False)['content_verified'])
        fake.raw=RAW+b' \r\n'
        with self.assertRaisesRegex(RuntimeError,'changed'):self.setup_mail(fake).copy(RAW,MID,append=False)
        self.assertEqual(fake.appends,0)
    def test_readonly_reconciliation_never_appends_missing_copy(self):
        fake=Fake();self.assertIsNone(self.setup_mail(fake).copy(RAW,MID,append=False));self.assertEqual(fake.appends,0)
    def test_modified_copy_is_held_without_overwrite(self):
        fake=Fake();fake.raw=RAW.replace(b'Body',b'Changed')
        with self.assertRaisesRegex(RuntimeError,'changed'):self.setup_mail(fake).copy(RAW,MID,append=True)
        self.assertEqual(fake.appends,0)
    def test_large_changed_copy_is_rejected_before_body_fetch(self):
        fake=Fake();fake.raw=RAW+b'x'*70000
        with self.assertRaisesRegex(RuntimeError,'size changed'):self.setup_mail(fake).copy(RAW,MID,append=True)
        self.assertNotIn('(BODY.PEEK[])',fake.fetches)
    def test_duplicate_special_folders_are_rejected(self):
        fake=Fake();fake.list=lambda:('OK',[b'(\\Drafts) "/" "One"',b'(\\Drafts) "/" "Two"'])
        with self.assertRaisesRegex(RuntimeError,'one existing'):self.setup_mail(fake).copy(RAW,MID,append=True)
        self.assertEqual(fake.appends,0)
