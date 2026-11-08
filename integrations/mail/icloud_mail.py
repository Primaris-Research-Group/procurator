"""Fixed iCloud IMAP operations; never sends SMTP or deletes mailbox messages."""
import contextlib
from email import policy
from email.parser import BytesParser
import hashlib
import imaplib
import json
import re
import ssl


class ICloudMailbox:
    def __init__(self, credential_file):
        self.credential_file = credential_file

    @contextlib.contextmanager
    def connect(self):
        config = json.loads(self.credential_file.read_text())
        with imaplib.IMAP4_SSL('imap.mail.me.com', 993, ssl_context=ssl.create_default_context(), timeout=20) as client:
            client.login(config['username'], config['password'])
            yield client

    @staticmethod
    def folder(client, kind):
        ok, rows = client.list()
        if ok != 'OK':
            raise RuntimeError('iCloud folder discovery failed.')
        special, fallback = [], []
        for raw in rows:
            if not isinstance(raw, bytes):
                continue
            match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:\\.|[^"\\])*"|NIL)\s+(.+)', raw)
            if not match:
                continue
            flags, wire_name = match.groups()
            if b'\\noselect' in flags.lower().split():
                continue
            # Retain the server's quoted ASCII/modified-UTF7 name, never model input.
            if b'\r' in wire_name or b'\n' in wire_name or len(wire_name) > 1024:
                continue
            if ('\\' + kind).encode().lower() in flags.lower().split():
                special.append(wire_name)
            name = wire_name.strip(b'"').lower()
            names = (b'drafts',) if kind == 'Drafts' else (b'sent messages', b'sent')
            if name in names:
                fallback.append(wire_name)
        candidates = special or fallback
        if len(candidates) != 1:
            raise RuntimeError('Could not identify one existing iCloud ' + kind + ' folder. No folder was created or changed.')
        return candidates[0]

    @staticmethod
    def select(client, folder):
        if client.select(folder, readonly=True)[0] != 'OK':
            raise RuntimeError('Could not open the iCloud folder for verification.')
        values = client.response('UIDVALIDITY')[1]
        if not values or not values[0]:
            raise RuntimeError('iCloud did not provide a mailbox identity.')
        return values[0].decode('ascii')

    @staticmethod
    def matching_copy(client, message_id, raw):
        if not re.fullmatch(r'<procurator-[a-f0-9]{16}@[a-zA-Z0-9.-]+>', message_id):
            raise ValueError('Invalid assistant message identity.')
        ok, rows = client.uid('search', None, 'HEADER', 'Message-ID', '"' + message_id + '"')
        if ok != 'OK':
            raise RuntimeError('iCloud draft verification search failed.')
        ids = rows[0].split() if rows and rows[0] else []
        if not ids:
            return None
        if len(ids) != 1:
            raise RuntimeError('More than one matching mailbox copy needs operator review.')
        ok, size_rows = client.uid('fetch', ids[0], '(RFC822.SIZE)')
        sizes = [int(x) for row in size_rows if isinstance(row, bytes) for x in re.findall(rb'RFC822.SIZE (\d+)', row)]
        if ok != 'OK' or len(sizes) != 1 or sizes[0] > min(65536, len(raw) + 4096):
            raise RuntimeError('The mailbox copy size changed; operator review is required.')
        ok, chunks = client.uid('fetch', ids[0], '(BODY.PEEK[])')
        if ok != 'OK':
            raise RuntimeError('iCloud draft read-back failed.')
        fetched = b''.join(x[1] for x in chunks if isinstance(x, tuple))
        # Apple's APPEND storage adds one terminal CRLF. Permit only that exact
        # observed transport normalization; headers and meaningful body bytes must match.
        if hashlib.sha256(fetched).digest() != hashlib.sha256(raw).digest() and fetched != raw + b'\r\n':
            raise RuntimeError('The mailbox copy changed. Do not overwrite or create another copy.')
        return ids[0].decode('ascii')

    def copy(self, raw, message_id, kind='Drafts', append=False):
        with self.connect() as client:
            folder = self.folder(client, kind)
            validity = self.select(client, folder)
            uid = self.matching_copy(client, message_id, raw)
            if uid is None and append:
                flags = '(\\Draft)' if kind == 'Drafts' else '(\\Seen)'
                # APPEND is valid while a mailbox is selected read-only; existing messages are never modified.
                if client.append(folder, flags, None, raw)[0] != 'OK':
                    raise RuntimeError('iCloud did not confirm saving the mailbox copy.')
                validity = self.select(client, folder)
                uid = self.matching_copy(client, message_id, raw)
            if uid is None:
                return None
            return {'folder': folder.decode('ascii').strip('"'), 'uidvalidity': validity, 'uid': uid,
                    'content_verified': True, 'message_id': message_id,
                    'verification': 'Exact message bytes, allowing one optional trailing CRLF added by iCloud.'}
