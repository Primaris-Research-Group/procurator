"""File uploads into web forms (web 0.18.0, PROCURATOR browsing plan Phase 6).

A document the owner sent in Telegram (or a file the assistant made for them) is staged for one browser:
the gateway copies it into the inbox, the broker keeps it under /var/lib/procurator-web/uploads/<browser>/ (3 days).
An upload step then names the file by its name. Uploading sends a document to a website, so it always goes to the
owner for review unless the owner approved that exact file with the task (upload_files in the /webtask review).
"""
import hashlib
import mimetypes
import os
import re
import shutil
import time
import uuid
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
KEEP_DAYS = 3
SAFE = re.compile(r'[^A-Za-z0-9._ ()-]+')
ALLOWED = ('.pdf', '.png', '.jpg', '.jpeg', '.heic', '.gif', '.webp', '.doc', '.docx', '.txt', '.rtf', '.csv', '.xls', '.xlsx', '.odt')

def safe_name(name):
    base = SAFE.sub('_', os.path.basename(str(name or 'document'))).strip(' ._')[:100] or 'document'
    return base

class Uploads:
    def __init__(self, broker, state, inbox):
        self.b, self.root, self.inbox = broker, Path(state) / 'uploads', Path(inbox)
        with broker.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS uploads(session_id TEXT,name TEXT,sha256 TEXT,bytes INTEGER,mime TEXT,path TEXT,ts REAL,PRIMARY KEY(session_id,name))')

    def stage(self, session_id, inbox_name, name):
        s = self.b.session(session_id)
        if s['status'] != 'open' or s.get('project_read_only'):
            raise ValueError('Open the task browser first.')
        if not re.fullmatch(r'upload-[a-f0-9]{32}\.bin', inbox_name or ''):
            raise ValueError('Upload staging failed (no file arrived).')
        src = self.inbox / inbox_name
        try:
            if not src.is_file():
                raise ValueError('Upload staging failed (no file arrived).')
            size = src.stat().st_size
            name = safe_name(name)
            if not name.lower().endswith(ALLOWED):
                raise ValueError('Only documents and images can be uploaded (PDF, Word, text, spreadsheets, JPG/PNG/HEIC).')
            if not 0 < size <= MAX_BYTES:
                raise ValueError('Files up to 20 MB can be uploaded.')
            data = src.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            d = self.root / session_id
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = d / (sha[:8] + '-' + name)
            dest.write_bytes(data)
            os.chmod(dest, 0o600)
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            with self.b.db() as db:
                db.execute('INSERT OR REPLACE INTO uploads VALUES(?,?,?,?,?,?,?)', (session_id, name, sha, size, mime, str(dest), time.time()))
            self.rotate()
            return {'session_id': session_id, 'file': name, 'bytes': size, 'sha256': sha, 'mime': mime,
                    'next': 'Use a web_task_step upload step: {"kind":"upload","ref":"<file input or Upload button ref>","file":"%s"}. Uploading goes to the owner for review unless the owner approved this file in the task.' % name}
        finally:
            src.unlink(missing_ok=True)

    def get(self, session_id, name):
        with self.b.db() as db:
            r = db.execute('SELECT * FROM uploads WHERE session_id=? AND name=?', (session_id, safe_name(name))).fetchone()
        if not r or not Path(r['path']).is_file():
            raise ValueError('No staged file named %s for this browser. Stage it first with web_upload_stage.' % name)
        return dict(r)

    def listing(self, session_id):
        with self.b.db() as db:
            return [{'file': r['name'], 'bytes': r['bytes'], 'sha256': r['sha256'][:12]} for r in db.execute('SELECT * FROM uploads WHERE session_id=? ORDER BY ts', (session_id,))]

    def for_act(self, steps, session_id):
        """Copy each upload step's file into the inbox for the driver; returns (steps for the driver, paths to remove)."""
        out, temp = [], []
        for st in steps:
            if st.get('kind') != 'upload':
                out.append(st)
                continue
            r = self.get(session_id, st['file']['name'])
            if hashlib.sha256(Path(r['path']).read_bytes()).hexdigest() != st['file']['sha256']:
                raise ValueError('The staged file changed since the review. Nothing was uploaded; stage it again and ask for a fresh review.')
            p = self.inbox / ('upload-' + uuid.uuid4().hex + '.bin')
            shutil.copyfile(r['path'], p)
            os.chmod(p, 0o640)
            try:
                os.chown(p, 0, 1000)
            except (PermissionError, OSError):
                pass
            temp.append(p)
            out.append({**st, 'file_path': str(p)})
        return out, temp

    def rotate(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        with self.b.db() as db:
            old = db.execute('SELECT path FROM uploads WHERE ts<?', (cutoff,)).fetchall()
            db.execute('DELETE FROM uploads WHERE ts<?', (cutoff,))
        for r in old:
            Path(r['path']).unlink(missing_ok=True)
