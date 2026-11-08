"""Text messages through the Twilio number (mail broker, 0.25.0). Same shape as email: the assistant writes an immutable draft
(sms_draft), asks for review (sms_review: the exact text goes to the owner in Telegram with a one-line approval), and only the
authenticated owner command /smssend ID CODE sends it, once, within 30 minutes. An ambiguous result is "uncertain" and is never
retried. Inbound texts are not read here.

SMS is OFF until the owner turns it on with bin/connect-sms.py after their number is registered for business texting (US carriers
block unregistered application-to-person texts: A2P 10DLC brand + campaign, or a verified toll-free number). The Twilio account
credentials are the ones the phone module already verified (/var/lib/procurator-voice/twilio.json, root only); they never reach the
model, the draft or a log line.
"""
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TWILIO = Path('/var/lib/procurator-voice/twilio.json')
SETTINGS = Path('/var/lib/procurator-mail/sms.json')
API = 'https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json'
E164_US = re.compile(r'^\+1[2-9]\d{2}[2-9]\d{6}$')
NO_TEXT = re.compile(r'^\+1(?:900|976)\d{7}$')
SECRET = re.compile(r'(?i)\b(?:password|passcode|one[- ]time code|otp|pin)\b\s*[:=]|(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])|(?<![\w-])(?:\d[ -]?){13,19}(?![\w-])')
MAX_BODY = 480
REVIEW_SECONDS = 1800


def code_for(draft):
    return hashlib.sha256(json.dumps(draft, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Twilio:
    def __init__(self, creds=TWILIO, settings=SETTINGS, opener=urllib.request.urlopen):
        self.creds, self.settings, self.open = Path(creds), Path(settings), opener

    def enabled(self):
        try:
            s = json.loads(self.settings.read_text())
            return s if s.get('enabled') is True and E164_US.match(str(s.get('from') or '')) else None
        except (OSError, ValueError):
            return None

    def send(self, to, body):
        s = self.enabled()
        if not s:
            raise RuntimeError('sms off')
        c = json.loads(self.creds.read_text())
        sid, token = c['account_sid'], c['auth_token']
        data = urllib.parse.urlencode({'To': to, 'From': s['from'], 'Body': body}).encode()
        req = urllib.request.Request(API % urllib.parse.quote(sid), data=data, method='POST', headers={
            'Authorization': 'Basic ' + base64.b64encode(('%s:%s' % (sid, token)).encode()).decode(), 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with self.open(req, timeout=25) as r:
                out = json.loads(r.read(65536))
            return {'status': 'accepted', 'provider_id': out.get('sid'), 'provider_status': out.get('status')}
        except urllib.error.HTTPError as e:
            try:
                code = json.loads(e.read(65536)).get('code')
            except Exception:
                code = None
            return {'status': 'refused', 'reason': {21610: 'the recipient replied STOP to this number', 21211: 'the number is not valid',
                                                    21614: 'the number cannot receive texts', 30034: 'the sending number is not registered for business texting (A2P 10DLC)',
                                                    30032: 'the toll-free number is not verified', 21408: 'texting to this region is not enabled'}.get(code, 'Twilio refused it (code %s)' % code)}


class Sms:
    def __init__(self, broker, provider=None):
        self.b = broker
        self.p = provider or Twilio()
        with self.b.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sms(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,hash TEXT,draft TEXT,status TEXT,created REAL,reviewed REAL,'
                       'sent REAL,result TEXT)')

    def row(self, ident):
        with self.b.db() as db:
            r = db.execute('SELECT * FROM sms WHERE id=?', (str(ident or ''),)).fetchone()
        if not r:
            raise ValueError('Unknown text message draft.')
        return r

    @staticmethod
    def public(r):
        d = json.loads(r['draft'])
        return {'id': r['id'], 'to': d['to'], 'body': d['body'], 'status': r['status'], 'result': json.loads(r['result']) if r['result'] else None}

    def call(self, action, a, privileged=False):
        if action == 'sms_draft':
            rid = a.get('request_id')
            if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', rid):
                raise ValueError('Use a stable request_id of 8-100 letters, digits, underscores or hyphens.')
            to = re.sub(r'[\s().-]', '', str(a.get('to') or ''))
            if not E164_US.match(to) or NO_TEXT.match(to):
                raise ValueError('to is one US mobile or business number in +1 format (no 900/976 numbers).')
            body = str(a.get('body') or '').strip()
            if not 1 <= len(body) <= MAX_BODY:
                raise ValueError('body is 1-%d characters (about three text segments).' % MAX_BODY)
            if SECRET.search(body):
                raise ValueError('Texts never carry passwords, codes, card or Social Security numbers. Nothing was saved.')
            draft = {'to': to, 'body': body}
            h = code_for(draft)
            with self.b.db() as db:
                old = db.execute('SELECT * FROM sms WHERE request_id=?', (rid,)).fetchone()
                if old:
                    if old['hash'] != h:
                        raise ValueError('That request_id belongs to a different text. Use a new request_id for an edit.')
                    return self.public(old)
                ident = uuid.uuid4().hex[:16]
                db.execute("INSERT INTO sms VALUES(?,?,?,?,'draft',?,NULL,NULL,NULL)", (ident, rid, h, json.dumps(draft), time.time()))
            return {**self.public(self.row(ident)), 'note': 'Saved as a draft. Nothing was sent. Ask for review with sms_review.'}
        if action == 'sms_get':
            return self.public(self.row(a.get('id')))
        if action == 'sms_cancel':
            r = self.row(a.get('id'))
            with self.b.db() as db:
                db.execute("UPDATE sms SET status='cancelled' WHERE id=? AND status IN ('draft','reviewed')", (r['id'],))
            return self.public(self.row(r['id']))
        if action == 'sms_review':
            r = self.row(a.get('id'))
            if r['status'] not in ('draft', 'reviewed'):
                raise ValueError('This text cannot be reviewed for sending (it is %s).' % r['status'])
            if not self.p.enabled():
                raise ValueError('Texting is off on this server. The owner turns it on with bin/connect-sms.py once their number is registered for business '
                                 'texting (A2P 10DLC or a verified toll-free number). The draft is saved; no approval was issued.')
            d = json.loads(r['draft'])
            self.b.provider.telegram('TEXT MESSAGE REVIEW — %s\nTo: %s\n\n%s\n\nSend it by copying this ENTIRE line within 30 minutes:\n/smssend %s %s\n\n'
                                     'Ignore it to cancel. Any edit needs a new draft and approval.' % (r['id'], d['to'], d['body'], r['id'], r['hash'][:16]))
            with self.b.db() as db:
                db.execute("UPDATE sms SET status='reviewed',reviewed=? WHERE id=?", (time.time(), r['id']))
            return {'id': r['id'], 'status': 'reviewed', 'review_delivered_to_owner': True, 'sent': False}
        if action == 'sms_send':
            if not privileged:
                raise ValueError('Texts are sent only by the authenticated Telegram owner command /smssend.')
            r = self.row(a.get('id'))
            if a.get('hash') != r['hash'][:16]:
                raise ValueError('Approval does not match this exact text.')
            if r['status'] in ('sent', 'sending', 'uncertain', 'refused'):
                return {**self.public(r), 'already': True}
            if r['status'] != 'reviewed' or not r['reviewed'] or not 0 <= time.time() - r['reviewed'] <= REVIEW_SECONDS:
                raise ValueError('A fresh review is required (/smssend works for 30 minutes after the review).')
            with self.b.db() as db:
                db.execute("UPDATE sms SET status='sending' WHERE id=?", (r['id'],))
            d = json.loads(r['draft'])
            try:
                out = self.p.send(d['to'], d['body'])
                status = 'sent' if out['status'] == 'accepted' else 'refused'
            except Exception:
                out, status = {'status': 'uncertain', 'note': 'No clear answer from Twilio. Check the Twilio console before sending again; it is not retried.'}, 'uncertain'
            with self.b.db() as db:
                db.execute('UPDATE sms SET status=?,sent=?,result=? WHERE id=?', (status, time.time(), json.dumps(out), r['id']))
            return self.public(self.row(r['id']))
        raise ValueError('Unknown text message action.')
