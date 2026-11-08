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
