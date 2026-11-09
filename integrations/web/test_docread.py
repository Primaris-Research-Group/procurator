"""Whole-document reads (Nov 6 2026): inbox intake, sniffing, paging with sha256, OCR status, refusals."""
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from broker import Broker
import docread
from docread import DocReader, sniff


class Provider:
    def telegram(self, m):
        pass


def docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   + ''.join('<w:p><w:r><w:t>%s</w:t></w:r></w:p>' % line for line in text.split('\n')) + '</w:body></w:document>')
    return buf.getvalue()


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.b = Broker(root / 'browser.sqlite3', Provider())
        self.inbox = root / 'inbox'; self.inbox.mkdir()
        self.b.docs = DocReader(self.b, self.inbox)
        self.n = 0

    def drop(self, data):
        self.n += 1
        name = ('%032x' % self.n) + '.bin'
        (self.inbox / name).write_bytes(data)
        return name

    def call(self, action, **a):
        return self.b.call(action, a)

    def test_long_google_doc_is_read_in_pages_to_the_end(self):
        text = ''.join('Line %05d of the master timeline.\n' % i for i in range(3000))  # ~105 KB, past the old 64 KB cap
        key = 'drive:1AbCdEfGhIjK@2026-11-06T12:00:00Z'
        self.assertEqual(self.call('doc_page', key=key), {'cached': False})
        r = self.call('doc_ingest', key=key, kind='text', file=self.drop(text.encode()), name='Master timeline', mime='application/vnd.google-apps.document')
        self.assertEqual(r['status'], 'file_read'); self.assertEqual(r['extraction'], 'google_export_text'); self.assertFalse(r['complete'])
        self.assertIn('NOT read the whole document', r['how_to_continue']); self.assertEqual(list(self.inbox.iterdir()), [], 'the original is deleted after reading')
        got, offset, sha = r['content'], r['next_offset'], r['sha256']
        while offset is not None:
            p = self.call('doc_page', key=key, offset=offset, expected_sha256=sha)
            self.assertTrue(p['cached']); got += p['content']; offset = p['next_offset']
        self.assertEqual(got, text); self.assertIn('Line 02999', got)
        self.assertEqual(self.call('doc_page', key=key, offset=20000, expected_sha256='0' * 64)['status'], 'file_changed')

    def test_sniffing_docx_text_and_unreadable(self):
        self.assertEqual(sniff(b'%PDF-1.7 ...'), 'pdf'); self.assertEqual(sniff(docx('a')), 'docx'); self.assertEqual(sniff('héllo'.encode()), 'text')
        self.assertIsNone(sniff(b'\x89PNG\r\n\x1a\n\x00\x00')); self.assertIsNone(sniff(b'PK\x03\x04garbage'))
        r = self.call('doc_ingest', key='gmail:18c2a:att0001', kind='auto', file=self.drop(docx('Engagement letter\nFee: $20,000')), name='letter.docx')
        self.assertEqual(r['extraction'], 'original_docx'); self.assertIn('Fee: $20,000', r['content']); self.assertTrue(r['complete'])
        r = self.call('doc_ingest', key='gmail:18c2a:att0002', kind='auto', file=self.drop(b'plain notes'))
        self.assertEqual(r['extraction'], 'original_text')
        r = self.call('doc_ingest', key='gmail:18c2a:att0003', kind='auto', file=self.drop(b'\x89PNG\r\n\x1a\n\x00\x00'), name='photo.png')
        self.assertEqual(r['status'], 'file_not_extractable')

    def test_saved_email_is_decoded_not_read_as_raw_mime(self):
        """Nov 9: an .eml in Drive came back with its body still base64. Saved emails are parsed: headers, the decoded body,
        a forwarded message in full, a Word attachment inline, other attachments listed."""
        from email.message import EmailMessage
        inner = EmailMessage()
        inner['From'] = 'Program Office <office@example.org>'; inner['To'] = 'Resident <resident@example.org>'
        inner['Date'] = 'Mon, 02 Nov 2026 09:15:00 -0500'; inner['Subject'] = 'Committee recommendations'
        inner.set_content('The committee recommends extending the review period by one month.\nPlease confirm receipt.')
        outer = EmailMessage()
        outer['From'] = 'Resident <resident@example.org>'; outer['To'] = 'Counsel <counsel@example.com>'
        outer['Date'] = 'Mon, 02 Nov 2026 10:00:00 -0500'; outer['Subject'] = 'Fwd: Committee recommendations'
        outer.set_content('See below. Café notes: ñ — “quoted”.', charset='utf-8', cte='base64')
        outer.add_alternative('<p>See <b>below</b>.</p>', subtype='html')
        outer.add_attachment(inner)
        outer.add_attachment(docx('Rebuttal draft\nPoint one'), maintype='application', subtype='vnd.openxmlformats-officedocument.wordprocessingml.document', filename='rebuttal.docx')
        outer.add_attachment(b'%PDF-1.4 fake', maintype='application', subtype='pdf', filename='notice.pdf')
        raw = outer.as_bytes()
        self.assertIn(b'Content-Transfer-Encoding: base64', raw)
        self.assertTrue(docread.looks_like_email(raw)); self.assertFalse(docread.looks_like_email(b'From the desk of the editor:\nhello'))
        r = self.call('doc_ingest', key='drive:1EmlFromDrive01@v1', kind='auto', file=self.drop(raw), name='Re CCC Recommendations.eml', mime='message/rfc822')
        self.assertEqual(r['extraction'], 'original_email'); c = r['content']
        self.assertIn('Subject: Fwd: Committee recommendations', c); self.assertIn('Café notes: ñ — “quoted”.', c)
        self.assertNotIn('Q2Fm', c, 'no base64 left in the text')
        self.assertIn('----- Attached message', c); self.assertIn('From: Program Office <office@example.org>', c)
        self.assertIn('extending the review period by one month', c)
        self.assertIn('----- Attachment rebuttal.docx', c); self.assertIn('Point one', c)
        self.assertIn('Other attachments', c); self.assertIn('notice.pdf (application/pdf', c)
        html_only = EmailMessage(); html_only['From'] = 'a@example.org'; html_only['To'] = 'b@example.org'; html_only['Date'] = 'Mon, 02 Nov 2026 10:00:00 -0500'
        html_only['Subject'] = 'HTML only'; html_only.set_content('<html><style>p{}</style><p>Line&nbsp;one</p><p>Line two &amp; more</p></html>', subtype='html')
        r = self.call('doc_ingest', key='drive:1EmlHtmlOnly01@v1', kind='auto', file=self.drop(html_only.as_bytes()), name='x.eml')
        self.assertIn('Line one\nLine two & more', r['content']); self.assertNotIn('<p>', r['content'])
        r = self.call('doc_ingest', key='drive:1OutlookMsg001@v1', kind='auto', file=self.drop(docread.OLE + b'\x00' * 100), name='GME.msg')
        self.assertEqual(r['status'], 'file_not_extractable'); self.assertIn('Outlook .msg', r['note'])

    def test_pdf_paths(self):
        orig = docread.Extractor.start
        calls = []
        def fake_start(self_, key, kind, data):
            calls.append(kind)
            return {'status': 'running', 'method': 'ocr', 'pages_total': 3, 'pages_planned': 3, 'pages_done': 1, 'text': ''}
        docread.Extractor.start = fake_start
        self.addCleanup(setattr, docread.Extractor, 'start', orig)
        r = self.call('doc_ingest', key='drive:1PdfScanOnly@v1', kind='auto', file=self.drop(b'%PDF-1.4 scanned'), name='NJ_Fee_arbitration.pdf', mime='application/pdf')
        self.assertEqual(calls, ['pdf']); self.assertEqual(r['status'], 'ocr_in_progress'); self.assertEqual(r['retry_after_seconds'], 20)

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, 'Bad document key'):
            self.call('doc_page', key='file:/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'Unknown inbox file'):
            self.call('doc_ingest', key='drive:1AbCdEfGhIjK@v', file='../browser.sqlite3')
        with self.assertRaisesRegex(ValueError, 'not in the inbox'):
            self.call('doc_ingest', key='drive:1AbCdEfGhIjK@v', file='f' * 32 + '.bin')
        target = Path(self.tmp.name) / 'secret.txt'; target.write_text('secret')
        link = self.inbox / ('a' * 32 + '.bin'); os.symlink(target, link)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.call('doc_ingest', key='drive:1AbCdEfGhIjK@v', file=link.name)
        self.assertTrue(target.exists()); self.assertFalse(link.exists())
        with self.assertRaisesRegex(ValueError, 'kind must be'):
            self.call('doc_ingest', key='drive:1AbCdEfGhIjK@v', file=self.drop(b'x'), kind='exe')


if __name__ == '__main__':
    unittest.main()
