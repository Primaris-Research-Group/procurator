import base64,io,json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from extract import Extractor,docx_text,page,tools
try:
    import reportlab,PIL  # noqa: F401  (fixture generators; may be absent on the server)
    FIXTURES=True
except ImportError:
    FIXTURES=False
OCR=all(tools().values())

def make_docx(paragraphs):
    W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    body=''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('word/document.xml',f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return b.getvalue()

def text_pdf(lines):
    from reportlab.pdfgen import canvas
    b=io.BytesIO();c=canvas.Canvas(b)
    for i,l in enumerate(lines):c.drawString(72,760-18*i,l)
    c.showPage();c.save();return b.getvalue()

def scanned_pdf(lines):
    from PIL import Image,ImageDraw,ImageFont
    img=Image.new('L',(1700,800),255);d=ImageDraw.Draw(img)
    try:font=ImageFont.truetype('DejaVuSans.ttf',48)
    except OSError:font=ImageFont.load_default(size=48)
    for i,l in enumerate(lines):d.text((80,80+90*i),l,fill=0,font=font)
    b=io.BytesIO();img.save(b,'PDF',resolution=200);return b.getvalue()

def wait(ex,key,seconds=90):
    end=time.time()+seconds
    while time.time()<end:
        r=ex.lookup(key)
        if r and r['status']!='running':return r
        time.sleep(0.5)
    raise AssertionError('OCR did not finish')

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.ex=Extractor(Path(self.tmp.name)/'cache')
    def test_docx_paragraphs_and_entity_rejection(self):
        self.assertEqual(docx_text(make_docx(['Dear Counsel,','Revised response.'])),'Dear Counsel,\nRevised response.')
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('word/document.xml','<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
        with self.assertRaises(ValueError):docx_text(b.getvalue())
    @unittest.skipUnless(FIXTURES and OCR,'PDF fixture libraries or PDF tools unavailable')
    def test_text_pdf_uses_exact_text_layer(self):
        r=self.ex.start('k','pdf',text_pdf(['Notice of noncompliance with the night float schedule.','Issued by the program office on August 25.']))
        self.assertEqual(r['status'],'done');self.assertEqual(r['method'],'original_pdf_text');self.assertIn('night float schedule',r['text'])
    @unittest.skipUnless(FIXTURES and OCR,'PDF fixture libraries or OCR tools unavailable')
    def test_scanned_pdf_is_ocrd_in_background_and_cached_without_bytes(self):
        r=self.ex.start('scan','pdf',scanned_pdf(['NOTICE OF NONCOMPLIANCE','Night Float Shift Schedule']))
        self.assertEqual(r['method'],'ocr');self.assertEqual(r['status'],'running')
        done=wait(self.ex,'scan');self.assertEqual(done['status'],'done');self.assertIn('NONCOMPLIANCE',done['text'].upper());self.assertIn('[Page 1]',done['text'])
        stored=''.join(p.read_text() for p in (Path(self.tmp.name)/'cache').glob('*.json'))
        self.assertNotIn('%PDF',stored)
    def test_pagination_requires_matching_fingerprint(self):
        chunk,d=page('abcdefghij',0,4);self.assertEqual(chunk['content'],'abcd');self.assertEqual(chunk['next_offset'],4)
        self.assertIsNone(page('abcdefghij',4,4,'0'*64)[0]);self.assertEqual(page('abcdefghij',8,4,d)[0]['next_offset'],None)
    def test_cache_expires_and_missing_tools_are_reported(self):
        self.ex.start('d','docx',make_docx(['x']*3))
        self.assertIsNotNone(self.ex.lookup('d'))
        with patch('extract.time.time',return_value=time.time()+7200):self.assertIsNone(self.ex.lookup('d'))
        with patch('extract.tools',return_value={'pdftotext':None,'pdfinfo':None,'pdftoppm':None,'tesseract':None}):
            r=self.ex.start('p','pdf',b'%PDF-1.4 fake')
        self.assertEqual(r['status'],'unavailable')

if __name__=='__main__':unittest.main()
