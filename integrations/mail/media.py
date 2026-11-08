"""Media files bound into approvals (web 0.27.0; mail/media.py is a byte-identical copy, test_media.py checks).

Posts need images or video and emails need attachments. In both cases the owner approves a preview, and what the owner approved
must be exactly what leaves. So every file goes through this store first:

  * content-addressed: the file is kept under its own sha256 (MEDIA_ROOT/<sha256>) with a small sidecar
    (<sha256>.json: name, mime, bytes, source, created); a media ref is "m_" + the first 24 hex digits of the hash;
  * the type is sniffed from the bytes (JPEG, PNG, GIF, WebP, MP4/MOV, PDF), never taken from the name;
  * bind(refs) returns [{ref, sha256, name, mime, bytes}] — this list goes INTO the approval hash, so the preview, the hash
    and the files cannot drift apart;
  * read_verified(item) re-hashes the file at the moment it is used and refuses if a single byte changed (an image swapped
    after approval is refused, nothing is sent).

The web broker writes (it owns MEDIA_ROOT); the mail broker only reads (root, ProtectSystem=strict leaves /var/lib readable).
Media never goes to the model: tools see refs, names, types and sizes only.
"""
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

MEDIA_ROOT = Path(os.environ.get('PROCURATOR_MEDIA_ROOT', '/var/lib/procurator-web/media'))
REF = re.compile(r'^m_[a-f0-9]{24}$')
LIMITS = {'image': 20 * 1024 * 1024, 'video': 200 * 1024 * 1024, 'document': 20 * 1024 * 1024}
KEEP_DAYS = 30


def sniff(head):
    """(mime, kind) from the first bytes, or (None, None)."""
    if head[:3] == b'\xff\xd8\xff':
        return 'image/jpeg', 'image'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png', 'image'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif', 'image'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp', 'image'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        return ('video/quicktime' if brand == b'qt  ' else 'video/mp4'), 'video'
    if head[:5] == b'%PDF-':
        return 'application/pdf', 'document'
    return None, None


def clean_name(name, mime):
    base = re.sub(r'[^\w .()-]', '_', os.path.basename(str(name or '')))[:120].strip(' .') or 'file'
    ext = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp', 'video/mp4': '.mp4',
           'video/quicktime': '.mov', 'application/pdf': '.pdf'}.get(mime, '')
    if ext and not base.lower().endswith(ext) and not (ext == '.jpg' and base.lower().endswith('.jpeg')):
        base += ext
    return base


def size_text(n):
    return '%.1f MB' % (n / 1048576) if n >= 1048576 else '%d KB' % max(1, round(n / 1024))


class MediaError(ValueError):
    pass


class Media:
    def __init__(self, root=MEDIA_ROOT, now=time.time):
        self.root, self.now = Path(root), now

    # ---------- write (web broker) ----------
    def put(self, data, name='', source='upload'):
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise MediaError('The file is empty.')
        mime, kind = sniff(bytes(data[:16]))
        if not mime:
            raise MediaError('Only JPEG, PNG, GIF and WebP images, MP4/MOV video and PDF files can be attached or posted.')
        if len(data) > LIMITS[kind]:
            raise MediaError('The %s is larger than %s.' % (kind, size_text(LIMITS[kind])))
        sha = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / sha
        if not path.exists():
            tmp = self.root / ('.%s.%d.tmp' % (sha, os.getpid()))
            with open(tmp, 'wb') as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        meta = {'sha256': sha, 'name': clean_name(name, mime), 'mime': mime, 'kind': kind, 'bytes': len(data), 'source': str(source)[:40],
                'created': self.now()}
        (self.root / (sha + '.json')).write_text(json.dumps(meta))
        return self.public(meta)

    def put_file(self, path, name=None, source='inbox', limit=None, delete=True):
        """Take one regular file (no links), e.g. a file the gateway dropped in the inbox; the inbox copy is deleted."""
        p = Path(path)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            raise MediaError('The file is not there (it may have been taken already).')
        try:
            if not stat.S_ISREG(st.st_mode):
                raise MediaError('Not a regular file.')
            if st.st_size > (limit or max(LIMITS.values())):
                raise MediaError('The file is too large.')
            fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as f:
                data = f.read()
        finally:
            if delete:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return self.put(data, name or p.name, source)

    def prune(self, keep_refs=()):
        """Drop files older than KEEP_DAYS unless a pending draft still names them."""
        keep = {r[2:] for r in keep_refs}
        cutoff, gone = self.now() - KEEP_DAYS * 86400, 0
        for side in self.root.glob('*.json') if self.root.is_dir() else []:
            try:
                meta = json.loads(side.read_text())
            except (OSError, ValueError):
                continue
            if meta.get('created', 0) < cutoff and meta['sha256'][:24] not in keep:
                for f in (self.root / meta['sha256'], side):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                gone += 1
        return gone

    # ---------- read (both brokers) ----------
    @staticmethod
    def public(meta):
        return {'ref': 'm_' + meta['sha256'][:24], 'sha256': meta['sha256'], 'name': meta['name'], 'mime': meta['mime'],
                'kind': meta.get('kind') or sniff(b'')[1], 'bytes': meta['bytes'], 'size': size_text(meta['bytes'])}

    def meta(self, ref):
        if not isinstance(ref, str) or not REF.match(ref):
            raise MediaError('Media refs look like m_ followed by 24 hex digits (from media_list or media_add).')
        hits = [p for p in self.root.glob(ref[2:] + '*.json')] if self.root.is_dir() else []
        if len(hits) != 1:
            raise MediaError('Unknown media %s (it may have expired after %d days).' % (ref, KEEP_DAYS))
        return json.loads(hits[0].read_text())

    def bind(self, refs, kinds=None, max_files=10):
        """The list that goes into an approval hash. Refuses duplicates, unknown refs and kinds the destination cannot take."""
        if refs in (None, []):
            return []
        if not isinstance(refs, list) or len(refs) > max_files:
            raise MediaError('Attach at most %d files, as a list of media refs.' % max_files)
        if len(set(refs)) != len(refs):
            raise MediaError('The same file is listed twice.')
        out = []
        for ref in refs:
            m = self.meta(ref)
            if kinds and m.get('kind') not in kinds:
                raise MediaError('%s is a %s; this destination takes %s.' % (m['name'], m.get('kind'), ' or '.join(kinds)))
            self.read_verified(self.public(m))   # exists and is intact now
            out.append({k: v for k, v in self.public(m).items() if k in ('ref', 'sha256', 'name', 'mime', 'bytes')})
        return out

    def read_verified(self, item):
        """The file's bytes, after checking they still hash to what was approved."""
        sha = item.get('sha256') if isinstance(item, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha):
            raise MediaError('Bad media record.')
        p = self.root / sha
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise MediaError('Media file is not a regular file.')
            fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as f:
                data = f.read()
        except FileNotFoundError:
            raise MediaError('%s is no longer stored. Nothing was sent; add the file again and request a new approval.' % item.get('name', 'A file'))
        if hashlib.sha256(data).hexdigest() != sha:
            raise MediaError('%s changed after it was approved (its sha256 no longer matches). Nothing was sent; request a new approval.' % item.get('name', 'A file'))
        return data

    @staticmethod
    def preview_lines(items):
        return ['%d. %s — %s, %s, sha256 %s…' % (i, m['name'], m['mime'], size_text(m['bytes']), m['sha256'][:12]) for i, m in enumerate(items, 1)]

    def list(self, limit=20):
        rows = []
        for side in self.root.glob('*.json') if self.root.is_dir() else []:
            try:
                rows.append(json.loads(side.read_text()))
            except (OSError, ValueError):
                pass
        rows.sort(key=lambda m: -m.get('created', 0))
        return [self.public(m) | {'source': m.get('source')} for m in rows[:limit]]
