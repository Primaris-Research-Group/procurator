"""Read-only, folder-bound iCloud browsing with UID pagination and safe search."""
import base64
import datetime
from email import policy
from email.parser import BytesParser
import hashlib
import json
from html.parser import HTMLParser
import re
from icloud_mail import ICloudMailbox

MAX_PAGE = 50
BODY_BYTES = 131072


def uid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', value) or int(value) > 4294967295:
        raise ValueError('Use a message UID or mailbox identity returned by the same folder listing.')
    return value


def search_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Search text must be 1–200 visible characters without line breaks.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def criteria(filters, before_uid=None):
    allowed = {'from', 'to', 'subject', 'text', 'since', 'before', 'unread'}
    if not isinstance(filters, dict) or set(filters)-allowed:
        raise ValueError('Unsupported iCloud search filter.')
    terms = []
    for key in ('from', 'to', 'subject', 'text'):
        if key in filters:
            terms += [key.upper(), search_text(filters[key])]
    for key in ('since', 'before'):
        if key in filters:
            value = filters[key]
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Use YYYY-MM-DD for received-date filters.')
            day = datetime.date.fromisoformat(value)
            months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
            terms += [key.upper(), f'{day.day:02d}-{months[day.month-1]}-{day.year:04d}']
    if 'unread' in filters:
        if type(filters['unread']) is not bool:
            raise ValueError('Unread must be true or false.')
        terms.append('UNSEEN' if filters['unread'] else 'SEEN')
    if before_uid is not None:
        uid(before_uid)
        if int(before_uid) == 1:
            return None
        terms += ['UID', f'1:{int(before_uid)-1}']
    return terms or ['ALL']


def display_name(wire):
    value = wire.decode('ascii', errors='replace')
    if value.startswith('"') and value.endswith('"'):
        value = re.sub(r'\\(.)', r'\1', value[1:-1])
    def decode(match):
        if not match[1]:
            return '&'
        try:
            raw = match[1].replace(',', '/')
            return base64.b64decode(raw+'='*((-len(raw)) % 4)).decode('utf-16-be')
        except (ValueError, UnicodeError):
            return match[0]
    return re.sub(r'&([^-]*)-', decode, value)[:200]


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li') and not self.hidden:
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden-1)
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def fetched_messages(chunks, requested):
    """UID can precede or follow a literal. Never confuse sequence numbers with UIDs."""
    groups = []
    metadata, bodies = [], []
    def finish():
        if bodies:
            groups.append((b' '.join(metadata), b''.join(bodies)))
    for item in chunks:
        if isinstance(item, tuple) and isinstance(item[0], bytes) and isinstance(item[1], bytes):
            if bodies and re.match(rb'^\d+\s+\(', item[0]):
                finish()
                metadata, bodies = [], []
            metadata.append(item[0])
            bodies.append(item[1])
        elif isinstance(item, bytes):
            metadata.append(item)
    finish()
    output = {}
    for meta, raw in groups:
        values = re.findall(rb'\bUID (\d+)\b', meta)
        if len(values) != 1:
            raise RuntimeError('The mail server did not return one unambiguous UID for a message.')
        identifier = values[0].decode('ascii')
        if identifier not in requested or identifier in output:
            raise RuntimeError('The mail server returned an unexpected message identity.')
        output[identifier] = (raw, b'\\Seen' not in meta)
    return output


class ICloudReader(ICloudMailbox):
    @staticmethod
    def inventory(client):
        ok, rows = client.list()
        if ok != 'OK':
            raise RuntimeError('iCloud folders could not be listed.')
        result = {}
        for row in rows:
            if not isinstance(row, bytes):
                continue
            match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:\\.|[^"\\])*"|NIL)\s+(.+)', row)
            if not match:
                continue
            flags, wire = match.groups()
            if b'\\noselect' in flags.lower().split() or len(wire) > 1024 or b'\r' in wire or b'\n' in wire:
                continue
            label = display_name(wire)
            identifier = 'inbox' if label.upper() == 'INBOX' else 'f_'+hashlib.sha256(wire).hexdigest()[:16]
            if identifier in result:
                raise RuntimeError('Ambiguous iCloud folder identity.')
            result[identifier] = {'wire': wire, 'name': label}
        return result

    def folders(self, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError('Folder pages use limit 1–100 and a nonnegative offset up to 10000.')
        with self.connect() as client:
            folders = self.inventory(client)
        ordered = sorted(folders.items(), key=lambda x: (x[0] != 'inbox', x[1]['name'].casefold()))
        return {'folders': [{'folder_id': k, 'name': v['name']} for k, v in ordered[offset:offset+limit]],
                'next_offset': offset+limit if len(ordered) > offset+limit else None,
                'total_folders': len(ordered), 'read_only': True}

    def browse(self, folder_id='inbox', limit=25, before_uid=None, uidvalidity=None, filters=None, message_uid=None):
        if not isinstance(folder_id, str) or not re.fullmatch(r'inbox|f_[a-f0-9]{16}', folder_id):
            raise ValueError('Use a folder_id returned by mail_icloud_folders.')
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError('Use 1–50 messages per page; follow next_before_uid for older mail.')
        filters = {} if filters is None else filters
        terms = criteria(filters, before_uid)
        if before_uid is not None or message_uid is not None:
            uid(uidvalidity)
        if message_uid is not None:
            uid(message_uid)
        with self.connect() as client:
            folders = self.inventory(client)
            if folder_id not in folders:
                raise ValueError('That folder is no longer available; refresh the folder list.')
            folder = folders[folder_id]
            validity = self.select(client, folder['wire'])
            if uidvalidity is not None and uidvalidity != validity:
                raise ValueError('Mailbox UIDVALIDITY changed. Start a fresh page in this folder.')
            if message_uid is None:
                charset = ['CHARSET', 'UTF-8'] if any(not s.isascii() for s in (terms or [])) else []
                if terms is None:
                    matches = []
                else:
                    ok, found = client.uid('search', *charset, *(s.encode('utf-8') for s in terms))
                    if ok != 'OK':
                        raise RuntimeError('iCloud search did not complete.')
                    if not found or not isinstance(found[0], bytes) or len(found[0]) > 8000000:
                        raise RuntimeError('iCloud returned an invalid or oversized UID search.')
                    matches = sorted({uid(x.decode('ascii')) for x in found[0].split()}, key=int, reverse=True)
                    if before_uid is not None:
                        matches = [x for x in matches if int(x) < int(before_uid)]
                identifiers = matches[:limit]
                more = len(matches) > limit
            else:
                identifiers, more = [message_uid], False
            output, consumed, output_bytes = [], [], 0
            if identifiers:
                body = f'BODY.PEEK[]<0.{BODY_BYTES}>' if message_uid else 'BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]<0.4096>'
                ok, chunks = client.uid('fetch', ','.join(identifiers), f'(UID FLAGS RFC822.SIZE {body})')
                if ok != 'OK':
                    raise RuntimeError('iCloud message read did not complete.')
                fetched = fetched_messages(chunks, set(identifiers))
                for identifier in identifiers:
                    if identifier not in fetched:
                        consumed.append(identifier)
                        continue  # The message may have been removed concurrently; never substitute another UID.
                    raw, unread = fetched[identifier]
                    if len(raw) > (BODY_BYTES if message_uid else 4096)+4096:
                        raise RuntimeError('iCloud returned more content than the requested bound.')
                    message = BytesParser(policy=policy.default).parsebytes(raw)
                    record = {'uid': identifier, 'unread': unread,
                              **{k: str(message.get(k, ''))[:(300 if k == 'subject' else 250)] for k in ('from', 'to', 'subject', 'date', 'message-id')}}
                    if message_uid:
                        part = message.get_body(preferencelist=('plain', 'html'))
                        content = '(No readable body in this bounded excerpt.)'
                        if part:
                            try:
                                content = str(part.get_content())
                            except (LookupError, UnicodeError):
                                content = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
                            if part.get_content_type() == 'text/html':
                                parser = PlainHTML()
                                parser.feed(content)
                                content = ''.join(parser.parts)
                        record.update(body=content[:20000], excerpt_only=True, attachments_downloaded=False)
                        while len(json.dumps(record).encode()) > 60000:
                            record['body'] = record['body'][:int(len(record['body'])*0.8)]
                    size = len(json.dumps(record).encode())
                    if output and output_bytes+size > 65000:
                        more = True
                        break
                    output.append(record)
                    consumed.append(identifier)
                    output_bytes += size
            return {'folder_id': folder_id, 'folder_name': folder['name'], 'uidvalidity': validity,
                    'untrusted_email_data': output, 'next_before_uid': consumed[-1] if more and consumed else None,
                    'has_more': more, 'filters': filters, 'read_only': True,
                    'pagination_note': 'Keep folder_id, uidvalidity and filters unchanged when following next_before_uid. Newer arrivals will not shift older UID pages.'}
