#!/usr/bin/env python3
"""SSH forced-command relay. Fixed Apple SMTP destination, sender, durable no-retry journal."""
import base64
import contextlib
import datetime as dt
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import socket
import sqlite3
import ssl
import sys

ACCOUNT = 'owner@icloud.example'
STATE = Path('/var/lib/procurator-icloud-relay')
MAX_BYTES = 65536


def validate(request):
    if not isinstance(request, dict) or set(request) - {'action', 'id', 'raw', 'sha256', 'password'}:
        raise ValueError('Invalid relay request.')
    action = request.get('action')
    if action not in ('check', 'send', 'status'):
        raise ValueError('Unsupported relay action.')
    if action == 'check':
        return None
    if not isinstance(request.get('id'), str) or not re.fullmatch(r'[a-f0-9]{16}', request['id']):
        raise ValueError('Invalid send identity.')
    if action == 'status':
        return None
    try:
        raw = base64.b64decode(request['raw'], validate=True)
    except (KeyError, ValueError, TypeError):
        raise ValueError('Invalid message encoding.') from None
    if not 1 <= len(raw) <= MAX_BYTES or hashlib.sha256(raw).hexdigest() != request.get('sha256'):
        raise ValueError('Message content hash mismatch.')
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    if msg.defects or len(msg.get_all('From', [])) != 1 or str(msg['From']) != ACCOUNT:
        raise ValueError('The relay sends only from its configured iCloud account.')
    if str(msg.get('Message-ID', '')) != '<procurator-' + request['id'] + '@icloud.com>':
        raise ValueError('Message identity does not match the approved snapshot.')
    if msg.is_multipart() or msg.get_content_type() != 'text/plain' or len(msg.get_all('Subject', [])) != 1:
        raise ValueError('Only one plain-text email is supported.')
    if set(k.lower() for k in msg.keys()) - {'from','to','cc','bcc','subject','message-id','date','mime-version','content-type','content-transfer-encoding'}:
        raise ValueError('Unexpected email header.')
    envelope = []
    for header in ('To','Cc','Bcc'):
        if len(msg.get_all(header, [])) > 1:
            raise ValueError('Duplicate address header.')
        for display, addr in getaddresses(msg.get_all(header, [])):
            if display or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", addr):
                raise ValueError('Invalid envelope recipient.')
            envelope.append(addr)
    if not 1 <= len(envelope) <= 8 or len({x.lower() for x in envelope}) != len(envelope):
        raise ValueError('Invalid recipient count.')
    # Bcc participates in the envelope but is never disclosed in recipient headers.
    if 'Bcc' in msg:
        del msg['Bcc']
    return envelope, msg.as_bytes()


def smtp_send(wire, recipients, password):
    server = smtplib.SMTP('smtp.mail.me.com', 587, timeout=20)
    try:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(ACCOUNT, password)
        if wire is None:
            server.noop()
            return {}
        return server.sendmail(ACCOUNT, recipients, wire)
    finally:
        try:
            server.quit()
        except Exception:
            server.close()


class Relay:
    def __init__(self, directory, sender=smtp_send):
        self.directory = directory
        self.sender = sender
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, hash TEXT, status TEXT, result TEXT)')

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.directory / 'outbox.sqlite3')
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
        finally:
            connection.close()

    def execute(self, request):
        prepared = validate(request)
        # One lock spans preflight, durable send claim and SMTP. Other SSH invocations cannot race a retry.
        with open(self.directory / 'outbox.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.db() as db:
                db.execute("UPDATE actions SET status='send_uncertain' WHERE status='sending'")
            if request['action'] == 'check':
                try:
                    self.sender(None, [], request['password'])
                except Exception:
                    return {'status': 'unavailable', 'smtp_authenticated': False}
                return {'status': 'ready', 'smtp_authenticated': True, 'smtp_host': 'smtp.mail.me.com', 'smtp_port': 587}
            with self.db() as db:
                old = db.execute('SELECT * FROM actions WHERE id=?', (request['id'],)).fetchone()
            if old:
                if request['action'] == 'send' and old['hash'] != request['sha256']:
                    raise ValueError('The send ID belongs to different content.')
                return {'status': old['status'], 'receipt': json.loads(old['result']) if old['result'] else None}
            if request['action'] == 'status':
                return {'status': 'not_recorded', 'receipt': None}
            password = request.get('password')
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise ValueError('A private app-specific password is required.')
            recipients, wire = prepared
            with self.db() as db:
                db.execute('INSERT INTO actions VALUES(?,?,?,?)', (request['id'], request['sha256'], 'sending', None))
            try:
                refused = self.sender(wire, recipients, password)
                rejected = [addr for addr in recipients if addr in refused]
                accepted = [addr for addr in recipients if addr not in refused]
                status = 'partially_sent' if rejected else 'sent'
                receipt = {'id': request['id'], 'accepted_recipients': accepted, 'refused_recipients': rejected,
                           'smtp_accepted_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            except Exception:
                status, receipt = 'send_uncertain', None
            with self.db() as db:
                db.execute('UPDATE actions SET status=?, result=? WHERE id=?',
                           (status, json.dumps(receipt) if receipt else None, request['id']))
            return {'status': status, 'receipt': receipt}


def main():
    os.umask(0o077)
    socket.setdefaulttimeout(20)
    raw = sys.stdin.buffer.readline(100001)
    if len(raw) > 100000:
        raise ValueError('Relay request too large.')
    request = json.loads(raw)
    result = Relay(STATE).execute(request)
    print(json.dumps({'ok': True, 'result': result}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never print SMTP diagnostics, email data or credentials to stdout/stderr.
        print(json.dumps({'ok': False, 'error': 'Relay request failed. Check status before any further action.'}))
        sys.exit(1)
