"""Accounts: connect an Apple (iCloud) or Google account with one email + app password, verified live (web 0.29.0).

Why this exists. Until now each account needed a root console script (bin/connect-icloud.py, bin/connect-google.py) run by the
operator on the box. A lay owner installing PROCURATOR cannot do that. This module is the backend of the dashboard's **Accounts**
tab: the owner types an email address and an app password into a form on the dashboard (same-origin, TLS, session + CSRF), the
broker verifies each service against the real provider in a few seconds, stores the secret under secrets/accounts/ (0600) and
tells the owner exactly which services are now live.

Design, decided after reviewing LifeOS and the market (claude/PROCURATOR_ECOSYSTEM_AND_INSTALLER_BUILD_PLAN.md):

  Apple    one Apple Account email + app-specific password (account.apple.com > Sign-In and Security > App-Specific Passwords)
           lights up: Mail (IMAP imap.mail.me.com), Send (SMTP smtp.mail.me.com:587, when the host can reach it), Calendar
           (CalDAV caldav.icloud.com), Contacts (CardDAV contacts.icloud.com). No OAuth, no developer account, no scopes.
           Reminders, Notes and iMessage are NOT reachable this way (Apple dropped CalDAV for Reminders in iOS 13) and need the
           Mac companion; the page says so instead of pretending.
  Google   one Google account email + app password (myaccount.google.com/apppasswords; needs 2-Step Verification; not offered on
           Workspace accounts since May 2025) lights up Mail (IMAP imap.gmail.com) and Send (SMTP smtp.gmail.com:587). Calendar,
           Contacts, Tasks and Drive need OAuth: the page offers "Sign in with Google" through oauth_store (provider 'google',
           sensitive scopes only — no restricted Gmail scopes, so no CASA audit). Reading mail over IMAP instead of gmail.readonly
           is what keeps the shared client out of the restricted tier.

Hosting note: DigitalOcean blocks outbound SMTP for new accounts. The SMTP probe therefore reports 'blocked' (not 'failed') when
port 587 is unreachable, and the page explains that sending will go through the Mac companion or the private relay. The IMAP,
CalDAV and CardDAV probes are plain HTTPS/IMAPS and work from any host.

Compatibility: for iCloud, the stored secret is also written in the legacy mail-broker format ({email, username, password}) to
secrets/accounts/icloud-mail.json; the mail broker reads that file when /var/lib/procurator-mail/icloud.json is absent, so Mail
works the moment the account is connected, with no change to the mail broker's reader.

The listing never contains a password. credentials(id) is for brokers only and is never exposed as a model tool.
"""
import imaplib
import json
import os
import re
import smtplib
import socket
import sqlite3
import ssl
import time
from contextlib import contextmanager
from pathlib import Path

import icloud_dav

SECRETS = Path(os.environ.get('PROCURATOR_SECRETS', '/var/lib/procurator-web/secrets'))
EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$")

PROVIDERS = {
    'icloud': {
        'title': 'Apple (iCloud)',
        'password_label': 'App-specific password',
        'password_help': 'account.apple.com › Sign-In and Security › App-Specific Passwords › + . Looks like xxxx-xxxx-xxxx-xxxx. Never your Apple Account password.',
        'password_url': 'https://account.apple.com/account/manage',
        'imap': ('imap.mail.me.com', 993), 'smtp': ('smtp.mail.me.com', 587),
        'caldav': icloud_dav.ICLOUD_CALDAV, 'carddav': icloud_dav.ICLOUD_CARDDAV,
        'services': ['mail', 'send', 'calendar', 'contacts'],
        'not_here': 'Reminders, Notes and iMessage live only on a Mac: install the PROCURATOR Companion there to add them.',
        'domains': ('icloud.com', 'me.com', 'mac.com'),
        # 0.30.0: the walk-through the Accounts page shows (Tier 1 of the lay-person install plan).
        'steps': ['Open account.apple.com and sign in with your Apple Account (the same one as on your iPhone).',
                  'Choose Sign-In and Security, then App-Specific Passwords. (No such option? Two-factor authentication must be on — it is on for almost every account made since 2019.)',
                  'Press +, name it "PROCURATOR", and confirm with your Apple Account password.',
                  'Apple shows a password in four groups of four letters (xxxx-xxxx-xxxx-xxxx) once. Copy it and paste it below, with the email you use for iCloud.'],
        'mistakes': 'Use the app-specific password, not your Apple Account password. If you later change your Apple Account password, Apple cancels every app-specific password: come back here and reconnect.',
    },
    'gmail': {
        'title': 'Google (Gmail)',
        'password_label': 'App password',
        'password_help': 'myaccount.google.com/apppasswords (2-Step Verification must be on). 16 letters, shown once. Not your Google password. Work/school (Workspace) accounts cannot make app passwords — use "Sign in with Google" instead.',
        'password_url': 'https://myaccount.google.com/apppasswords',
        'imap': ('imap.gmail.com', 993), 'smtp': ('smtp.gmail.com', 587),
        'caldav': None, 'carddav': None,
        'services': ['mail', 'send'],
        'not_here': 'Calendar, Contacts, Tasks and Drive use "Sign in with Google" below (no password to copy).',
        'domains': ('gmail.com', 'googlemail.com'),
        'steps': ['Open myaccount.google.com/security and turn on 2-Step Verification if it is off (Google requires it for app passwords).',
                  'Open myaccount.google.com/apppasswords, type "PROCURATOR" as the app name and press Create.',
                  'Google shows 16 letters in four groups once. Copy them and paste them below, with your Gmail address.'],
        'mistakes': 'Work or school Google accounts (Google Workspace) cannot make app passwords any more; connect a personal Gmail here. If the apppasswords page says the setting is not available, 2-Step Verification is still off.',
    },
}
# Google OAuth provider for oauth_store.PROVIDERS: sensitive scopes only (verification without a CASA audit); gmail.readonly and
# gmail.modify are RESTRICTED and deliberately absent — mail reading goes over IMAP with an app password instead.
GOOGLE_OAUTH = {
    'title': 'Google (Calendar, Contacts, Tasks, Drive files, send mail)', 'status': 'needs_client',
    'auth_url': 'https://accounts.google.com/o/oauth2/v2/auth', 'token_url': 'https://oauth2.googleapis.com/token',
    'revoke_url': 'https://oauth2.googleapis.com/revoke', 'pkce': True, 'kinds': ['person'],
    'scopes': ['openid', 'email', 'profile', 'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/contacts.readonly',
               'https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/drive.file'],
    'extra_auth': {'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'true'},
    'note': 'Mail reading stays on IMAP (app password), so no restricted Gmail scope is requested. Until Google verifies the PROCURATOR client, '
            'the consent screen shows an "unverified app" warning (Advanced › continue) and the client is capped at 100 users.',
}


class AccountError(ValueError):
    pass


def google_identities(access_token, fetch=None):
    """oauth_store identities adapter: who signed in. Uses the OpenID userinfo endpoint; no Gmail scope needed."""
    import urllib.request
    fetch = fetch or (lambda url, h: json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15).read().decode()))
    me = fetch('https://openidconnect.googleapis.com/v1/userinfo', {'Authorization': 'Bearer ' + access_token})
    return [{'external_id': str(me.get('sub') or me.get('email') or 'google'), 'display': str(me.get('email') or me.get('name') or 'Google account')[:120], 'kind': 'person'}]


# ---------- live probes (each returns a small dict, never raises on a provider "no") ----------
def probe_imap(host, port, user, password, timeout=12):
    try:
        with imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout) as c:
            c.login(user, password)
            typ, data = c.select('INBOX', readonly=True)
            if typ != 'OK':
                return {'ok': False, 'error': 'Signed in, but the inbox could not be opened.'}
            try:
                count = int(data[0])
            except (TypeError, ValueError, IndexError):
                count = None
            return {'ok': True, 'messages': count}
    except imaplib.IMAP4.error:
        return {'ok': False, 'error': 'The mail server did not accept this email and app password.'}
    except (OSError, socket.timeout, ssl.SSLError) as e:
        return {'ok': False, 'error': 'Could not reach %s (%s).' % (host, e.__class__.__name__)}


def probe_smtp(host, port, user, password, timeout=8):
    """Login only; nothing is sent. 'blocked' means the host cannot reach port 587 (DigitalOcean's default for new accounts)."""
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
            s.login(user, password)
            return {'ok': True}
    except smtplib.SMTPAuthenticationError:
        return {'ok': False, 'error': 'The outgoing mail server did not accept this email and app password.'}
    except (socket.timeout, ConnectionRefusedError, OSError):
        return {'ok': False, 'blocked': True, 'error': 'This host cannot reach the outgoing mail server (port 587 is blocked here). Sending will go through the Mac companion or the private relay.'}
    except smtplib.SMTPException as e:
        return {'ok': False, 'error': 'Outgoing mail check failed (%s).' % e.__class__.__name__}


def probe_caldav(base, user, password):
    try:
        cals = icloud_dav.DAVClient(base, user, password).calendars()
        return {'ok': True, 'calendars': [{'name': c['name'], 'url': c['url']} for c in cals][:50]}
    except icloud_dav.DAVError as e:
        return {'ok': False, 'error': str(e)}


def probe_carddav(base, user, password):
    try:
        books = icloud_dav.DAVClient(base, user, password).addressbooks()
        return {'ok': True, 'addressbooks': [{'name': b['name'], 'url': b['url']} for b in books][:20]}
    except icloud_dav.DAVError as e:
        return {'ok': False, 'error': str(e)}


PROBES = {'imap': probe_imap, 'smtp': probe_smtp, 'caldav': probe_caldav, 'carddav': probe_carddav}


class Accounts:
    def __init__(self, path, secrets_dir=SECRETS, now=time.time, probes=None, legacy_mail_path=None):
        self.path, self.secrets, self.now = str(path), Path(secrets_dir), now
        self.probes = {**PROBES, **(probes or {})}
        # Where the mail broker looks when /var/lib/procurator-mail/icloud.json is absent (see mail/broker.py icloud_path()).
        self.legacy_mail_path = Path(legacy_mail_path) if legacy_mail_path else self.secrets / 'accounts' / 'icloud-mail.json'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, provider TEXT, email TEXT, services TEXT, status TEXT,
                checked REAL, error TEXT, created REAL, UNIQUE(provider, email));''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    # ---------- secrets ----------
    def _secret_path(self, aid):
        return self.secrets / 'accounts' / (aid + '.json')

    def _write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)

    def credentials(self, aid):
        """Brokers only. Never returned by a tool or the dashboard API."""
        p = self._secret_path(aid)
        if not p.is_file():
            raise AccountError('That account is not connected.')
        return json.loads(p.read_text())

    # ---------- the flow ----------
    @staticmethod
    def provider(pid):
        p = PROVIDERS.get(pid) if isinstance(pid, str) else None
        if not p:
            raise AccountError('Choose Apple (iCloud) or Google (Gmail).')
        return p

    @staticmethod
    def clean_email(email):
        e = str(email or '').strip().lower()
        if not EMAIL.match(e) or len(e) > 200:
            raise AccountError('That does not look like an email address.')
        return e

    @staticmethod
    def clean_password(password, pid):
        pw = str(password or '').strip()
        if pid == 'icloud':
            pw = pw.lower()
            letters = re.sub(r'[\s-]', '', pw)
            if re.fullmatch(r'[a-z]{16}', letters):          # pasted without dashes, or with spaces: put the dashes back
                pw = '-'.join(letters[i:i + 4] for i in (0, 4, 8, 12))
            if not re.fullmatch(r'[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}', pw):
                raise AccountError('An Apple app-specific password looks like xxxx-xxxx-xxxx-xxxx (16 letters in four groups). Nothing was saved.')
        elif pid == 'gmail':
            pw = re.sub(r'[\s-]', '', pw).lower()
            if not re.fullmatch(r'[a-z]{16}', pw):
                raise AccountError('A Google app password is 16 letters (Google shows them in four groups). Nothing was saved.')
        if not pw:
            raise AccountError('Enter the app password. Nothing was saved.')
        return pw

    def verify(self, pid, email, password):
        """Runs every probe for the provider. Returns {services:{...}, ok:bool}; a provider refusal is a result, not an exception."""
        p = self.provider(pid)
        services = {}
        imap = self.probes['imap'](p['imap'][0], p['imap'][1], email, password)
        services['mail'] = imap
        if not imap['ok']:
            # A wrong password fails everything the same way; stop here so the owner sees one message, not four.
            err = imap['error']
            if 'did not accept' in err:
                err += (' Check the email is your Apple Account email and the password is a NEW app-specific password (not your Apple password).'
                        if pid == 'icloud' else
                        ' Check it is your Gmail address and a NEW app password; work/school (Workspace) accounts cannot use app passwords.')
            return {'ok': False, 'services': services, 'error': err}
        services['send'] = self.probes['smtp'](p['smtp'][0], p['smtp'][1], email, password)
        if p.get('caldav'):
            services['calendar'] = self.probes['caldav'](p['caldav'], email, password)
        if p.get('carddav'):
            services['contacts'] = self.probes['carddav'](p['carddav'], email, password)
        return {'ok': True, 'services': services}

    def connect(self, pid, email, password):
        """Verify live, then store. The password is kept only in the secret file; the row holds the service summary."""
        p = self.provider(pid)
        email = self.clean_email(email)
        password = self.clean_password(password, pid)
        result = self.verify(pid, email, password)
        if not result['ok']:
            raise AccountError(result['error'] + ' Nothing was saved.')
        aid = pid + '-' + re.sub(r'[^a-z0-9]+', '-', email.split('@')[0])[:24].strip('-')
        with self.db() as db:
            existing = db.execute('SELECT id FROM accounts WHERE provider=? AND email=?', (pid, email)).fetchone()
            if existing:
                aid = existing['id']
            elif db.execute('SELECT 1 FROM accounts WHERE id=?', (aid,)).fetchone():
                aid = aid + '-' + str(int(self.now()) % 10000)
        secret = {'provider': pid, 'email': email, 'username': email, 'password': password,
                  'imap': list(p['imap']), 'smtp': list(p['smtp']), 'caldav': p.get('caldav'), 'carddav': p.get('carddav')}
        self._write_json(self._secret_path(aid), secret)
        if pid == 'icloud':
            self._write_json(self.legacy_mail_path, {'email': email, 'username': email, 'password': password})
        summary = self.summary(result['services'])
        t = self.now()
        with self.db() as db:
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET services=excluded.services, status=excluded.status, checked=excluded.checked, error=NULL',
                       (aid, pid, email, json.dumps(summary), 'connected', t, None, t))
        return {'id': aid, 'provider': pid, 'email': email, 'services': summary, 'status': 'connected'}

    def recheck(self, aid):
        creds = self.credentials(aid)
        result = self.verify(creds['provider'], creds['email'], creds['password'])
        t = self.now()
        with self.db() as db:
            if result['ok']:
                db.execute("UPDATE accounts SET services=?, status='connected', checked=?, error=NULL WHERE id=?", (json.dumps(self.summary(result['services'])), t, aid))
            else:
                db.execute("UPDATE accounts SET status='reconnect', checked=?, error=? WHERE id=?", (t, result['error'][:200], aid))
        return self.get(aid)

    def disconnect(self, aid):
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (str(aid or ''),)).fetchone()
            if not row:
                raise AccountError('That account is not connected.')
            db.execute('DELETE FROM accounts WHERE id=?', (row['id'],))
        for path in (self._secret_path(row['id']),) + ((self.legacy_mail_path,) if row['provider'] == 'icloud' else ()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return {'removed': row['id'], 'email': row['email']}

    @staticmethod
    def summary(services):
        """The service table the page shows: ok | blocked | failed, plus counts/names. No credentials, no URLs with tokens."""
        out = {}
        for name, r in services.items():
            s = {'state': 'ok' if r.get('ok') else ('blocked' if r.get('blocked') else 'failed')}
            if not r.get('ok'):
                s['error'] = r.get('error', '')[:200]
            if name == 'mail' and r.get('messages') is not None:
                s['messages'] = r['messages']
            if name == 'calendar' and r.get('ok'):
                s['calendars'] = [c['name'] for c in r.get('calendars', [])][:20]
            if name == 'contacts' and r.get('ok'):
                s['addressbooks'] = len(r.get('addressbooks', []))
            out[name] = s
        return out

    def get(self, aid):
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
        return self._row(row) if row else None

    def list(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM accounts ORDER BY created').fetchall()
        return {'providers': [{'id': k, 'title': v['title'], 'password_label': v['password_label'], 'password_help': v['password_help'],
                               'password_url': v['password_url'], 'services': v['services'], 'not_here': v['not_here'],
                               'steps': v.get('steps', []), 'mistakes': v.get('mistakes', '')} for k, v in PROVIDERS.items()],
                'accounts': [self._row(r) for r in rows]}

    @staticmethod
    def _row(r):
        return {'id': r['id'], 'provider': r['provider'], 'title': PROVIDERS.get(r['provider'], {}).get('title', r['provider']), 'email': r['email'],
                'services': json.loads(r['services'] or '{}'), 'status': r['status'], 'checked': r['checked'], 'error': r['error'], 'created': r['created']}

    # ---------- for brokers: which account serves what ----------
    def for_service(self, service, provider=None):
        """First connected account whose service is 'ok' (or 'blocked' for send, which the companion/relay handles)."""
        for a in self.list()['accounts']:
            if provider and a['provider'] != provider:
                continue
            st = (a['services'].get(service) or {}).get('state')
            if a['status'] == 'connected' and st in ('ok', 'blocked'):
                return a
        return None

    def dav(self, aid, kind):
        """A DAVClient for 'calendar' or 'contacts' on a connected account (brokers only)."""
        c = self.credentials(aid)
        base = c.get('caldav') if kind == 'calendar' else c.get('carddav')
        if not base:
            raise AccountError('That account has no %s service.' % kind)
        return icloud_dav.DAVClient(base, c['username'], c['password'])
