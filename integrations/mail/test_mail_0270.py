"""0.27.0 mail: attachments bound into the approval by sha256, and the owner copy per audience (synthetic only)."""
import email.policy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from email.parser import BytesParser
from pathlib import Path

from test_broker import Fake

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40 + b'synthetic'


class Mail0270(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.environ.get('PROCURATOR_MEDIA_ROOT')
        os.environ['PROCURATOR_MEDIA_ROOT'] = str(Path(self.tmp.name) / 'media')
        spec = importlib.util.spec_from_file_location('mailbroker0270', Path(__file__).with_name('broker.py'))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        import media
        importlib.reload(media)
        self.media = media.Media(Path(self.tmp.name) / 'media')
        self.p = Fake()
        self.b = self.m.Broker(Path(self.tmp.name) / 'mail.db', self.p)
        self.args = {'request_id': 'attach-test-001', 'account': 'icloud', 'to': ['friend@example.com'], 'subject': 'Photo', 'body': 'Here it is.'}

    def tearDown(self):
        if self.old is None:
            os.environ.pop('PROCURATOR_MEDIA_ROOT', None)
        else:
            os.environ['PROCURATOR_MEDIA_ROOT'] = self.old

    def test_owner_copy_rule(self):
        self.p.icloud_ready = True
        d = self.b.call('create', self.args)['draft']
        self.assertEqual(d['bcc'], [self.m.ACCOUNTS['gmail']])
        self.assertEqual(d['auto_cc'], {'audience': 'person', 'mode': 'bcc', 'address': self.m.ACCOUNTS['gmail']})
        # From the copy address itself: it is already in Sent, no copy.
        d = self.b.call('create', {**self.args, 'request_id': 'attach-test-002', 'account': 'gmail'})['draft']
        self.assertEqual(d['bcc'], [])
        # Only to their own addresses (self audience): no copy.
        d = self.b.call('create', {**self.args, 'request_id': 'attach-test-003', 'to': [self.m.ACCOUNTS['icloud']]})['draft']
        self.assertNotIn('auto_cc', d)
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('auto_cc_set', {'person': 'off'})
        self.assertEqual(self.b.call('auto_cc_set', {'person': 'cc'}, True)['person'], 'cc')
        d = self.b.call('create', {**self.args, 'request_id': 'attach-test-004'})['draft']
        self.assertEqual(d['cc'], [self.m.ACCOUNTS['gmail']])
        self.b.call('review', {'id': self.b.call('create', {**self.args, 'request_id': 'attach-test-005'})['id']})
        self.assertIn('Owner copy: %s added as CC' % self.m.ACCOUNTS['gmail'], self.p.reviews[-1])
        self.b.call('auto_cc_set', {'person': 'off'}, True)
        self.assertNotIn('auto_cc', self.b.call('create', {**self.args, 'request_id': 'attach-test-006'})['draft'])

    def test_attachment_bound_and_verified(self):
        self.p.icloud_ready = True
        ref = self.media.put(PNG, 'site photo.png')['ref']
        d = self.b.call('create', {**self.args, 'attachments': [ref]})
        att = d['draft']['attachments'][0]
        self.assertEqual(att['sha256'], hashlib.sha256(PNG).hexdigest())
        self.b.call('review', {'id': d['id']})
        self.assertIn('site photo.png — image/png', self.p.reviews[-1])
        self.assertIn('sha256 ' + att['sha256'][:12], self.p.reviews[-1])
        # Swapping the stored file after the draft does not change what leaves: the message was frozen with the approved bytes.
        (Path(self.tmp.name) / 'media' / att['sha256']).write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x01' * 40)
        r = self.b.call('send', {'id': d['id'], 'hash': d['hash']}, True)
        self.assertEqual(r['status'], 'sent')
        msg = BytesParser(policy=email.policy.default).parsebytes(self.p.icloud_sends[-1])
        parts = list(msg.iter_attachments())
        self.assertEqual(hashlib.sha256(parts[0].get_payload(decode=True)).hexdigest(), att['sha256'])
        self.assertEqual(parts[0].get_filename(), 'site photo.png')

    def test_tampered_stored_message_is_refused(self):
        self.p.icloud_ready = True
        ref = self.media.put(PNG, 'p.png')['ref']
        d = self.b.call('create', {**self.args, 'request_id': 'attach-test-010', 'attachments': [ref]})
        self.b.call('review', {'id': d['id']})
        msg = BytesParser(policy=email.policy.default).parsebytes(self.b.row(d['id'])['raw'])
        for part in msg.iter_attachments():
            part.set_content(b'\x89PNG\r\n\x1a\nswapped', maintype='image', subtype='png', filename='p.png')
        with self.b.db() as db:
            db.execute('UPDATE drafts SET raw=? WHERE id=?', (msg.as_bytes(), d['id']))
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            self.b.call('send', {'id': d['id'], 'hash': d['hash']}, True)
        self.assertEqual(self.p.icloud_sends, [])

    def test_attachment_limits(self):
        with self.assertRaisesRegex(ValueError, 'Media refs look like'):
            self.b.call('create', {**self.args, 'attachments': ['../../etc/passwd']})
        with self.assertRaisesRegex(ValueError, 'Unknown media'):
            self.b.call('create', {**self.args, 'attachments': ['m_' + '1' * 24]})


if __name__ == '__main__':
    unittest.main()


class StreamK17(unittest.TestCase):
    def test_stream_lines_carry_no_mail_content(self):
        """K17: after a mail send the activity stream has the draft id and status only: no body, subject or address."""
        import importlib.util as iu
        spec = iu.spec_from_file_location('mailbroker_k17', Path(__file__).with_name('broker.py'))
        m = iu.module_from_spec(spec)
        spec.loader.exec_module(m)
        lines = []

        class P(Fake):
            def stream(self, kind, summary, task_id=None):
                lines.append((kind, summary))
        with tempfile.TemporaryDirectory() as d:
            p = P()
            b = m.Broker(Path(d) / 'k17.db', p)
            draft = b.call('create', {'request_id': 'k17-mail-0001', 'account': 'gmail', 'to': ['friend@example.com'], 'subject': 'Secret subject words',
                                      'body': 'Very private body text about the appointment'})
            b.call('review', {'id': draft['id']})
            b.call('send', {'id': draft['id'], 'hash': draft['hash']}, True)
        self.assertEqual([k for k, _ in lines], ['waiting_approval', 'done'])
        for _, s in lines:
            for leak in ('private', 'Secret', 'friend@', 'appointment'):
                self.assertNotIn(leak, s)
