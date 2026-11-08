"""One OAuth2 connection store for every provider (web 0.27.0).

Meta, LinkedIn, TikTok, Slack and Microsoft all use OAuth2 authorization-code grants. Instead of one connect-*.py script per
service, every provider is a row in PROVIDERS and every connection lives in two tables:

  oauth_grants    one authorization by the owner: provider, the provider's user id, display name, scopes, status,
                  access-token expiry, token_ref (a path under secrets/), last refresh, last error. NO token values.
  oauth_accounts  the identities that grant can act as (their own profile, a company page, a workspace), each with a short
                  alias the owner chooses ("me", "acme-engineering", "primaris", "zenithiot"). Tools name the account by alias; the
                  approval hash binds provider, alias AND the provider's account id, so an approval cannot be reused for a
                  different page even if an alias is later reassigned.

Tokens (access and refresh) are written only to SECRETS/oauth/<provider>/<grant>.json, mode 0600, by this module. Nothing
here returns a token to a tool result; access_token() is for broker adapters only.

The Setup page pattern (dashboard, Phase 4): Settings > Connections lists PROVIDERS; Connect calls begin() (state + PKCE S256,
10 minutes), the browser goes to the provider, the provider returns to /assistant/dashboard/oauth/callback, complete()
exchanges the code (single use), stores the tokens and discovers the accounts. Disconnect deletes the token file.

Providers marked 'planned' have their endpoints recorded but cannot be connected until their connector ships and the endpoints
are re-checked against the provider's current documentation; 'mock-social' is the synthetic test provider (mock_social.py).
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

SECRETS = Path(os.environ.get('PROCURATOR_SECRETS', '/var/lib/procurator-web/secrets'))
MOCK_BASE = os.environ.get('PROCURATOR_MOCK_SOCIAL', 'http://127.0.0.1:3355')
ALIAS = re.compile(r'^[a-z0-9][a-z0-9-]{0,30}$')
PROVIDER_ID = re.compile(r'^[a-z][a-z0-9-]{1,30}$')

PROVIDERS = {
    'mock-social': {'title': 'Mock social (synthetic test site)', 'status': 'test', 'auth_url': MOCK_BASE + '/oauth/authorize',
                    'token_url': MOCK_BASE + '/oauth/token', 'revoke_url': MOCK_BASE + '/oauth/revoke', 'scopes': ['profile', 'pages', 'publish', 'comments'],
                    'client_id': 'procurator-test', 'pkce': True, 'kinds': ['person', 'page']},
    'linkedin': {'title': 'LinkedIn', 'status': 'planned', 'auth_url': 'https://www.linkedin.com/oauth/v2/authorization',
                 'token_url': 'https://www.linkedin.com/oauth/v2/accessToken', 'scopes': ['openid', 'profile', 'w_member_social'],
                 'kinds': ['person', 'page'], 'note': 'Company pages need the Community Management API (separate review).'},
    'meta': {'title': 'Meta (Facebook Pages, Instagram professional)', 'status': 'planned',
             'auth_url': 'https://www.facebook.com/dialog/oauth', 'token_url': 'https://graph.facebook.com/oauth/access_token',
             'scopes': ['pages_show_list', 'pages_manage_posts', 'instagram_basic', 'instagram_content_publish'], 'kinds': ['page'],
             'note': 'Needs a Business or Creator account and app review; Graph API version is pinned when the connector is built.'},
    'tiktok': {'title': 'TikTok', 'status': 'planned', 'auth_url': 'https://www.tiktok.com/v2/auth/authorize/',
               'token_url': 'https://open.tiktokapis.com/v2/oauth/token/', 'scopes': ['user.info.basic', 'video.publish'], 'kinds': ['person'],
               'note': 'Direct posting needs TikTok\'s content posting audit; until then posts are private drafts.'},
    'slack': {'title': 'Slack', 'status': 'planned', 'auth_url': 'https://slack.com/oauth/v2/authorize',
              'token_url': 'https://slack.com/api/oauth.v2.access', 'scopes': ['chat:write', 'channels:history', 'im:history'], 'kinds': ['workspace'],
              'note': 'Workspace audience: messages in channels are previewed like public posts.'},
    'microsoft': {'title': 'Microsoft 365 (Outlook, Teams)', 'status': 'planned',
                  'auth_url': 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
                  'token_url': 'https://login.microsoftonline.com/common/oauth2/v2.0/token', 'pkce': True,
                  'scopes': ['offline_access', 'User.Read', 'Mail.ReadWrite', 'Calendars.ReadWrite'], 'kinds': ['person', 'workspace'],
                  'note': 'A hospital tenant may require administrator consent.'},
}

# 0.29.0: Google with SENSITIVE scopes only (Calendar, Contacts read, Tasks, gmail.send, drive.file). No restricted Gmail scope is requested:
# mail reading goes over IMAP with an app password (accounts.py), which keeps the shared client out of Google's CASA audit tier.
# The client id/secret come from secrets/oauth/google-client.json (status 'needs_client' until that file exists).
try:
    from accounts import GOOGLE_OAUTH as _GOOGLE
    PROVIDERS['google'] = dict(_GOOGLE)
except Exception:  # accounts.py absent in a stripped deployment: Google simply is not listed
    pass


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def slug(text):
    s = re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')[:30].strip('-')
    return s or 'account'


def http_form(url, data, headers=None, timeout=20):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or '{}')


class OAuthError(ValueError):
    pass


class OAuthStore:
    def __init__(self, path, secrets_dir=SECRETS, now=time.time, post=http_form, identities=None, client_secret=None, providers=None, test_profile=False):
        """identities: {provider: fn(access_token) -> [{external_id, display, kind}]} (adapters supply them)."""
        self.path, self.secrets, self.now, self.post = str(path), Path(secrets_dir), now, post
        self.identities = identities or {}
        self.client_secret = client_secret or self._secret_from_file
        self.providers = providers or PROVIDERS
        self.test_profile = test_profile
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS oauth_flows(state TEXT PRIMARY KEY, provider TEXT, verifier TEXT, redirect_uri TEXT, created REAL, used REAL);
            CREATE TABLE IF NOT EXISTS oauth_grants(id TEXT PRIMARY KEY, provider TEXT, subject TEXT, display TEXT, scopes TEXT, status TEXT,
                expires_at REAL, token_ref TEXT, created REAL, updated REAL, last_refresh REAL, last_error TEXT);
            CREATE TABLE IF NOT EXISTS oauth_accounts(provider TEXT, alias TEXT, external_id TEXT, display TEXT, kind TEXT, grant_id TEXT, status TEXT,
                created REAL, PRIMARY KEY(provider, alias));''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    # ---------- client credentials (0.29.0) ----------
    def client_file(self, provider):
        """secrets/oauth/<provider>-client.json: {"client_id": ..., "client_secret": ...}. Written by the operator (bin/set-oauth-client.sh) or by Setup."""
        try:
            data = json.loads((self.secrets / 'oauth' / (provider + '-client.json')).read_text())
            return data if isinstance(data, dict) and data.get('client_id') else None
        except (OSError, ValueError):
            return None

    def client_id(self, provider):
        p = self.providers.get(provider) or {}
        return (p.get('client_id') or os.environ.get('PROCURATOR_OAUTH_%s_CLIENT_ID' % provider.upper().replace('-', '_'))
                or (self.client_file(provider) or {}).get('client_id') or '')

    def _secret_from_file(self, provider):
        env = os.environ.get('PROCURATOR_OAUTH_%s_CLIENT_SECRET' % provider.upper().replace('-', '_'))
        return env or (self.client_file(provider) or {}).get('client_secret')

    def status(self, pid):
        p = self.providers[pid]
        if p['status'] == 'needs_client':
            return 'ready' if self.client_id(pid) else 'needs_client'
        return p['status']

    def provider(self, pid, connectable=True):
        p = self.providers.get(pid) if isinstance(pid, str) else None
        if not p:
            raise OAuthError('Unknown provider. Known: ' + ', '.join(sorted(self.providers)))
        if connectable and p['status'] == 'needs_client' and not self.client_id(pid):
            raise OAuthError('%s needs its OAuth client first: save it with bin/set-oauth-client.sh %s (client id and secret from the Google Cloud Console, or the PROCURATOR shared client when distributed). Nothing was connected.' % (p['title'], pid))
        if connectable and p['status'] == 'planned':
            raise OAuthError('%s is planned but not built yet: its connector ships after the public release. Nothing was connected.' % p['title'])
        if connectable and p['status'] == 'test' and not self.test_profile:
            raise OAuthError('%s is a test fixture; it is available only with the test profile on (PROCURATOR_TEST_PROFILE=1).' % p['title'])
        return p

    # ---------- secrets ----------
    def _token_path(self, provider, grant_id):
        return self.secrets / 'oauth' / provider / (grant_id + '.json')

    def _write_tokens(self, provider, grant_id, tok):
        p = self._token_path(provider, grant_id)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(tok, f)
        os.replace(tmp, p)
        return str(p.relative_to(self.secrets))

    def _read_tokens(self, ref):
        p = self.secrets / ref
        if not str(p.resolve()).startswith(str(self.secrets.resolve())):
            raise OAuthError('Bad token reference.')
        return json.loads(p.read_text())

    # ---------- the flow ----------
    def begin(self, provider, redirect_uri):
        p = self.provider(provider)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        q = {'response_type': 'code', 'client_id': self.client_id(provider),
             'redirect_uri': redirect_uri, 'scope': ' '.join(p['scopes']), 'state': state}
        if not q['client_id']:
            raise OAuthError('%s has no client id configured (Setup > Connections, advanced).' % p['title'])
        q.update(p.get('extra_auth') or {})  # e.g. Google: access_type=offline + prompt=consent so a refresh token is issued
        if p.get('pkce'):
            q['code_challenge'] = b64url(hashlib.sha256(verifier.encode()).digest())
            q['code_challenge_method'] = 'S256'
        with self.db() as db:
            db.execute('DELETE FROM oauth_flows WHERE created<?', (self.now() - 3600,))
            db.execute('INSERT INTO oauth_flows VALUES(?,?,?,?,?,NULL)', (state, provider, verifier, redirect_uri, self.now()))
        return {'url': p['auth_url'] + ('&' if '?' in p['auth_url'] else '?') + urllib.parse.urlencode(q), 'state': state, 'expires_in': 600}

    def complete(self, state, code, error=None):
        with self.db() as db:
            flow = db.execute('SELECT * FROM oauth_flows WHERE state=?', (str(state or ''),)).fetchone()
            if not flow:
                raise OAuthError('This sign-in link is unknown. Start again from Settings > Connections.')
            if flow['used']:
                raise OAuthError('This sign-in was already completed (a code is used once).')
            db.execute('UPDATE oauth_flows SET used=? WHERE state=?', (self.now(), flow['state']))
        if self.now() - flow['created'] > 600:
            raise OAuthError('The sign-in took longer than 10 minutes. Start again.')
        if error:
            raise OAuthError('The provider did not grant access (%s). Nothing was connected.' % re.sub(r'[^\w .-]', '', str(error))[:60])
        if not isinstance(code, str) or not 8 <= len(code) <= 2000:
            raise OAuthError('No authorization code came back. Nothing was connected.')
        prov = flow['provider']
        p = self.provider(prov)
        data = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': flow['redirect_uri'], 'client_id': self.client_id(prov)}
        if p.get('pkce'):
            data['code_verifier'] = flow['verifier']
        secret = self.client_secret(prov)
        if secret:
            data['client_secret'] = secret
        tok = self.post(p['token_url'], data)
        if not tok.get('access_token'):
            raise OAuthError('The provider did not return a token (%s). Nothing was connected.' % re.sub(r'[^\w .-]', '', str(tok.get('error', 'no token')))[:60])
        fetch = self.identities.get(prov)
        found = fetch(tok['access_token']) if fetch else []
        me = next((a for a in found if a.get('kind') == 'person'), found[0] if found else {'external_id': 'unknown', 'display': p['title']})
        grant_id = 'g_' + secrets.token_hex(8)
        t = self.now()
        record = {'access_token': tok['access_token'], 'refresh_token': tok.get('refresh_token'), 'token_type': tok.get('token_type', 'Bearer'),
                  'expires_at': t + int(tok['expires_in']) if str(tok.get('expires_in', '')).isdigit() else None}
        ref = self._write_tokens(prov, grant_id, record)
        scopes = tok.get('scope', ' '.join(p['scopes']))
        with self.db() as db:
            db.execute('INSERT INTO oauth_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (grant_id, prov, str(me.get('external_id'))[:120], str(me.get('display'))[:120],
                       json.dumps(scopes.split() if isinstance(scopes, str) else scopes), 'connected', record['expires_at'], ref, t, t, None, None))
            added = []
            for a in found:
                taken = {r['alias'] for r in db.execute('SELECT alias FROM oauth_accounts WHERE provider=?', (prov,))}
                existing = db.execute('SELECT alias FROM oauth_accounts WHERE provider=? AND external_id=?', (prov, str(a['external_id']))).fetchone()
                if existing:
                    db.execute("UPDATE oauth_accounts SET grant_id=?, status='active', display=? WHERE provider=? AND alias=?",
                               (grant_id, str(a.get('display'))[:120], prov, existing['alias']))
                    added.append(existing['alias'])
                    continue
                alias = 'me' if a.get('kind') == 'person' and 'me' not in taken else slug(a.get('display'))
                n, base = 2, alias
                while alias in taken:
                    alias, n = '%s-%d' % (base[:27], n), n + 1
                db.execute('INSERT INTO oauth_accounts VALUES(?,?,?,?,?,?,?,?)', (prov, alias, str(a['external_id'])[:120], str(a.get('display'))[:120],
                           a.get('kind', 'person'), grant_id, 'active', t))
                added.append(alias)
        return {'status': 'connected', 'provider': prov, 'grant': grant_id, 'display': me.get('display'), 'accounts': added}

    def access_token(self, provider, alias):
        """For broker adapters only. Refreshes when the token expires within two minutes; marks reauth_required on failure."""
        acct = self.account(provider, alias)
        with self.db() as db:
            g = db.execute('SELECT * FROM oauth_grants WHERE id=?', (acct['grant_id'],)).fetchone()
        if not g or g['status'] != 'connected':
            raise OAuthError('%s %s needs to be reconnected (Settings > Connections).' % (provider, alias))
        tok = self._read_tokens(g['token_ref'])
        if tok.get('expires_at') and tok['expires_at'] - self.now() < 120:
            p = self.provider(provider)
            if not tok.get('refresh_token'):
                self._mark(g['id'], 'reauth_required', 'token expired, no refresh token')
                raise OAuthError('%s %s needs to be reconnected (the sign-in expired).' % (provider, alias))
            try:
                data = {'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'], 'client_id': self.client_id(provider)}
                secret = self.client_secret(provider)
                if secret:
                    data['client_secret'] = secret
                new = self.post(p['token_url'], data)
                if not new.get('access_token'):
                    raise OAuthError(str(new.get('error', 'refresh refused')))
            except Exception as e:
                self._mark(g['id'], 'reauth_required', 'refresh failed: ' + str(e)[:80])
                raise OAuthError('%s %s needs to be reconnected (refresh was refused).' % (provider, alias))
            tok = {'access_token': new['access_token'], 'refresh_token': new.get('refresh_token') or tok['refresh_token'],
                   'token_type': new.get('token_type', 'Bearer'),
                   'expires_at': self.now() + int(new['expires_in']) if str(new.get('expires_in', '')).isdigit() else None}
            self._write_tokens(provider, g['id'], tok)
            with self.db() as db:
                db.execute('UPDATE oauth_grants SET expires_at=?, last_refresh=?, updated=?, last_error=NULL WHERE id=?', (tok['expires_at'], self.now(), self.now(), g['id']))
        return tok['access_token']

    def _mark(self, grant_id, status, error):
        with self.db() as db:
            db.execute('UPDATE oauth_grants SET status=?, last_error=?, updated=? WHERE id=?', (status, error[:200], self.now(), grant_id))

    # ---------- accounts ----------
    def account(self, provider, alias):
        if not isinstance(alias, str) or not ALIAS.match(alias):
            raise OAuthError('Name the account by its alias (social_accounts lists them), e.g. "me" or "acme-engineering".')
        with self.db() as db:
            a = db.execute('SELECT * FROM oauth_accounts WHERE provider=? AND alias=?', (provider, alias)).fetchone()
        if not a or a['status'] != 'active':
            raise OAuthError('No active %s account named "%s". Accounts: %s.' % (provider, alias, ', '.join(x['alias'] for x in self.accounts(provider)) or 'none connected'))
        return dict(a)

    def accounts(self, provider=None):
        with self.db() as db:
            rows = db.execute('SELECT a.*, g.status gstatus FROM oauth_accounts a LEFT JOIN oauth_grants g ON g.id=a.grant_id' +
                              (' WHERE a.provider=?' if provider else '') + ' ORDER BY a.provider, a.alias', (provider,) if provider else ()).fetchall()
        return [{'provider': r['provider'], 'alias': r['alias'], 'display': r['display'], 'kind': r['kind'],
                 'status': r['status'] if r['gstatus'] == 'connected' else (r['gstatus'] or 'disconnected')} for r in rows]

    def rename(self, provider, alias, new):
        if not isinstance(new, str) or not ALIAS.match(new):
            raise OAuthError('An alias is 1-31 lowercase letters, digits or hyphens.')
        self.account(provider, alias)
        with self.db() as db:
            if db.execute('SELECT 1 FROM oauth_accounts WHERE provider=? AND alias=?', (provider, new)).fetchone():
                raise OAuthError('That alias is taken.')
            db.execute('UPDATE oauth_accounts SET alias=? WHERE provider=? AND alias=?', (new, provider, alias))
        return {'provider': provider, 'alias': new}

    def list(self):
        """Everything the Setup page shows. No token values, no token paths."""
        with self.db() as db:
            grants = [{'id': g['id'], 'provider': g['provider'], 'display': g['display'], 'scopes': json.loads(g['scopes'] or '[]'), 'status': g['status'],
                       'expires_at': g['expires_at'], 'connected': g['created'], 'last_refresh': g['last_refresh'], 'last_error': g['last_error']}
                      for g in db.execute("SELECT * FROM oauth_grants WHERE status!='revoked' ORDER BY created")]
        return {'providers': [{'id': k, 'title': v['title'], 'status': self.status(k), 'scopes': v['scopes'], 'note': v.get('note', '')} for k, v in self.providers.items()],
                'grants': grants, 'accounts': self.accounts()}

    def disconnect(self, grant_id):
        with self.db() as db:
            g = db.execute('SELECT * FROM oauth_grants WHERE id=?', (str(grant_id or ''),)).fetchone()
        if not g:
            raise OAuthError('Unknown connection.')
        p = self.providers.get(g['provider'], {})
        revoked = False
        if p.get('revoke_url'):
            try:
                tok = self._read_tokens(g['token_ref'])
                self.post(p['revoke_url'], {'token': tok.get('refresh_token') or tok.get('access_token'), 'client_id': p.get('client_id') or ''})
                revoked = True
            except Exception:
                revoked = False
        try:
            (self.secrets / g['token_ref']).unlink()
        except OSError:
            pass
        with self.db() as db:
            db.execute("UPDATE oauth_grants SET status='revoked', updated=?, token_ref=NULL WHERE id=?", (self.now(), g['id']))
            db.execute("UPDATE oauth_accounts SET status='disconnected' WHERE grant_id=?", (g['id'],))
        return {'disconnected': g['id'], 'provider': g['provider'], 'provider_revoked': revoked, 'tokens_deleted': True}
