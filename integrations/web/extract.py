"""Text from original PDF/DOCX bytes captured from a project viewer.

Input is untrusted: external tools run as an unprivileged user with CPU/memory limits,
in a private temp directory that is deleted afterwards. Only extracted text is cached
(never the original bytes), keyed by the file's SHA-256, for one hour.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from xml.etree import ElementTree as ET

CACHE_TTL = 3600
MAX_BYTES = 20 * 1024 * 1024
MAX_OCR_PAGES = 60
TEXT_CHARS_PER_PAGE = 25  # non-space chars per page; scans have ~0, so below this OCR the PDF
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def tools():
    return {name: shutil.which(name) for name in ('pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract')}


def sandboxed(cmd, timeout, cwd=None):
    prefix = []
    if os.geteuid() == 0 and shutil.which('setpriv'):
        prefix = ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups', '--']
    if shutil.which('prlimit'):
        prefix += ['prlimit', '--as=1500000000', f'--cpu={timeout + 5}', '--']
    return subprocess.run(prefix + cmd, capture_output=True, timeout=timeout, cwd=cwd,
                          env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'OMP_THREAD_LIMIT': '1'})


def docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo('word/document.xml')
        if info.file_size > 50 * 1024 * 1024:
            raise ValueError('Document body is too large.')
        xml = z.read(info)
    if b'<!DOCTYPE' in xml[:4096] or b'<!ENTITY' in xml:
        raise ValueError('Unsupported document markup.')
    root = ET.fromstring(xml)
    paragraphs = []
    for p in root.iter(W + 'p'):
        parts = []
        for n in p.iter():
            if n.tag == W + 't':
                parts.append(n.text or '')
            elif n.tag == W + 'tab':
                parts.append('\t')
            elif n.tag in (W + 'br', W + 'cr'):
                parts.append('\n')
        paragraphs.append(''.join(parts))
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(paragraphs)).strip()


def page(text, offset=0, limit=12000, expected=None):
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if type(offset) is not int or not 0 <= offset <= 2000000 or type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError('Use a bounded preview offset and 1–20000 characters.')
    if offset and expected != digest:
        return None, digest
    content = text[offset:offset + limit]
    while len(json.dumps(content).encode()) > 64000:
        content = content[:int(len(content) * 0.8)]
    end = offset + len(content)
    return {'content': content, 'offset': offset, 'next_offset': end if end < len(text) else None,
            'visible_text_chars': len(text), 'sha256': digest}, digest


class Extractor:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.jobs = {}

    def _path(self, name):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.root / name

    def _read(self, name, default=None):
        try:
            return json.loads(self._path(name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name, value):
        path = self._path(name)
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
        os.replace(tmp, path)

    def lookup(self, key):
        """Recent extraction for this exact service/project/file, or None."""
        with self.lock:
            entry = self._read('index.json', {}).get(key)
            if not entry or time.time() - entry['t'] > CACHE_TTL:
                return None
            record = self._read(entry['sha'] + '.json')
            if not record:
                return None
            if record['status'] == 'running' and entry['sha'] not in self.jobs:
                return None  # interrupted by a restart; re-capture and resume
            return record

    def store_text(self, key, text, method):
        """Cache text that needs no extraction (a Google Doc exported as text, a CSV, a .txt file) under the same index."""
        data = text.encode('utf-8')
        if len(data) > MAX_BYTES:
            raise ValueError('The document text is larger than 20 MB.')
        sha = hashlib.sha256(data).hexdigest()
        record = {'status': 'done', 'method': method, 'text': text}
        with self.lock:
            index = self._read('index.json', {})
            index = {k: v for k, v in index.items() if time.time() - v['t'] <= CACHE_TTL}
            index[key] = {'sha': sha, 't': time.time()}
            self._write('index.json', index)
            self._write(sha + '.json', record)
        return record

    def start(self, key, kind, data):
        if len(data) > MAX_BYTES:
            raise ValueError('The original file is larger than 20 MB.')
        sha = hashlib.sha256(data).hexdigest()
        with self.lock:
            index = self._read('index.json', {})
            index = {k: v for k, v in index.items() if time.time() - v['t'] <= CACHE_TTL}
            index[key] = {'sha': sha, 't': time.time()}
            self._write('index.json', index)
            record = self._read(sha + '.json')
            if record and (record['status'] != 'running' or sha in self.jobs):
                return record
        if kind == 'docx':
            record = {'status': 'done', 'method': 'original_docx', 'text': docx_text(data)}
            self._write(sha + '.json', record)
            return record
        missing = [n for n, p in tools().items() if not p]
        if missing:
            record = {'status': 'unavailable', 'method': 'none',
                      'note': 'PDF extraction tools are not installed on the server: ' + ', '.join(missing)}
            return record
        work = tempfile.mkdtemp(prefix='procurator-extract-')
        try:
            os.chmod(work, 0o755)
            pdf = os.path.join(work, 'in.pdf')
            with open(pdf, 'wb') as f:
                f.write(data)
            os.chmod(pdf, 0o644)
            info = sandboxed(['pdfinfo', pdf], 20).stdout.decode('utf-8', 'replace')
            match = re.search(r'^Pages:\s+(\d+)', info, re.M)
            pages = int(match[1]) if match else 1
            text = sandboxed(['pdftotext', '-layout', '-enc', 'UTF-8', pdf, '-'], 30).stdout.decode('utf-8', 'replace')
            text = re.sub(r'[ \t]+\n', '\n', text.replace('\f', '\n\n')).strip()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if len(re.sub(r'\s', '', text)) >= TEXT_CHARS_PER_PAGE * max(1, pages):
            record = {'status': 'done', 'method': 'original_pdf_text', 'pages_total': pages, 'text': text}
            self._write(sha + '.json', record)
            return record
        record = {'status': 'running', 'method': 'ocr', 'pages_total': pages,
                  'pages_planned': min(pages, MAX_OCR_PAGES), 'pages_done': 0, 'text': ''}
        self._write(sha + '.json', record)
        thread = threading.Thread(target=self._ocr, args=(sha, data, record), daemon=True)
        with self.lock:
            self.jobs[sha] = thread
        thread.start()
        return record

    def _ocr(self, sha, data, record):
        work = tempfile.mkdtemp(prefix='procurator-ocr-')
        try:
            os.chmod(work, 0o777)
            pdf = os.path.join(work, 'in.pdf')
            with open(pdf, 'wb') as f:
                f.write(data)
            os.chmod(pdf, 0o644)
            chunks = []
            for n in range(1, record['pages_planned'] + 1):
                base = os.path.join(work, f'p{n}')
                sandboxed(['pdftoppm', '-r', '200', '-gray', '-png', '-singlefile', '-f', str(n), '-l', str(n), pdf, base], 60)
                result = sandboxed(['tesseract', base + '.png', '-', '-l', 'eng'], 90)
                chunks.append(f'[Page {n}]\n' + result.stdout.decode('utf-8', 'replace').strip())
                try:
                    os.remove(base + '.png')
                except OSError:
                    pass
                record = {**record, 'pages_done': n, 'text': '\n\n'.join(chunks)}
                self._write(sha + '.json', record)
            status = 'done' if record['pages_planned'] == record['pages_total'] else 'done_partial'
            self._write(sha + '.json', {**record, 'status': status})
        except Exception as error:
            self._write(sha + '.json', {**record, 'status': 'failed', 'note': type(error).__name__})
        finally:
            shutil.rmtree(work, ignore_errors=True)
            with self.lock:
                self.jobs.pop(sha, None)
