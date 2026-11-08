import unittest
from unittest.mock import patch
from icloud_manage import ICloudManager

class FakeIMAP:
    """Minimal IMAP double: one INBOX and Deleted Messages/Junk folders, tracking flags and copies."""
    def __init__(self,deleted_extra=False,copy_ok=True):
        self.ops=[];self.selected=None;self.copy_ok=copy_ok
        self.msgs={'10':set(),'12':set(),'20':({'\\Deleted'} if deleted_extra else set())}
        self.folders=[b'(\\HasNoChildren) "/" "INBOX"',b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',b'(\\HasNoChildren \\Junk) "/" "Junk"']
    def __enter__(self):return self
    def __exit__(self,*a):return False
    def list(self):return 'OK',self.folders
    def select(self,wire,readonly=True):self.selected=wire;self.readonly=readonly;return 'OK',[b'3']
    def response(self,name):return name,[b'77']
    def expunge(self):
        self.ops.append(('expunge',));self.msgs={k:v for k,v in self.msgs.items() if '\\Deleted' not in v};return 'OK',[]
    def uid(self,cmd,*args):
        cmd=cmd.lower();self.ops.append((cmd,)+args)
        if cmd=='search':
            if args[0]=='UID':return 'OK',[' '.join(u for u in args[1].split(',') if u in self.msgs).encode()]
            if args[0]=='DELETED':return 'OK',[' '.join(u for u,f in self.msgs.items() if '\\Deleted' in f).encode()]
        if cmd=='store':
            assert not self.readonly
            for u in args[0].split(','):
                (self.msgs[u].add if args[1]=='+FLAGS' else self.msgs[u].discard)(args[2].strip('()'))
            return 'OK',[]
        if cmd=='copy':return ('OK' if self.copy_ok else 'NO'),[]
        raise AssertionError(cmd)

def manager(fake):
    m=ICloudManager(None);m.connect=lambda:fake;return m

class Tests(unittest.TestCase):
    def test_mark_read_and_unread_change_only_existing_selected_messages(self):
        f=FakeIMAP();r=manager(f).apply('inbox','77',['12','10','99'],'mark_read')
        self.assertEqual(r['changed'],['10','12']);self.assertEqual(r['missing'],['99']);self.assertIn('\\Seen',f.msgs['10']);self.assertNotIn('\\Seen',f.msgs['20'])
        manager(f).apply('inbox','77',['10'],'mark_unread');self.assertNotIn('\\Seen',f.msgs['10'])
    def test_trash_copies_then_expunges_only_when_it_owns_every_deleted_flag(self):
        f=FakeIMAP();r=manager(f).apply('inbox','77',['10'],'trash')
        self.assertEqual(r['destination'],'Deleted Messages');self.assertTrue(r['original_removed'])
        self.assertIn(('copy','10',b'"Deleted Messages"'),f.ops);self.assertEqual(sorted(f.msgs),['12','20'])
    def test_unrelated_deleted_messages_block_expunge(self):
        f=FakeIMAP(deleted_extra=True);r=manager(f).apply('inbox','77',['10'],'trash')
        self.assertFalse(r['original_removed']);self.assertNotIn(('expunge',),f.ops);self.assertIn('20',f.msgs)
    def test_failed_copy_removes_nothing_and_stale_or_wrong_moves_are_refused(self):
        f=FakeIMAP(copy_ok=False)
        with self.assertRaisesRegex(RuntimeError,'Nothing was removed'):manager(f).apply('inbox','77',['10'],'junk')
        self.assertFalse(any('\\Deleted' in v for k,v in f.msgs.items()))
        with self.assertRaisesRegex(ValueError,'UIDVALIDITY'):manager(FakeIMAP()).apply('inbox','78',['10'],'mark_read')
        with self.assertRaisesRegex(ValueError,'already in INBOX'):manager(FakeIMAP()).apply('inbox','77',['10'],'not_junk')
        with self.assertRaises(ValueError):manager(FakeIMAP()).apply('inbox','77',['10'],'expunge_all')

if __name__=='__main__':unittest.main()
