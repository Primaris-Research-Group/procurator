"""Public posts and workspace messages: draft, publish preview, hash-bound approval (web 0.27.0).

Risk class "public" (connectors.py): the audience is everyone who can see a profile, page or channel, not a recipient. The
flow mirrors mail (immutable draft -> exact preview to the owner -> /postsend ID HASH within 30 minutes), with what a post
needs on top:

  * account   tools name the account by alias ("me", "acme-engineering", "primaris", "zenithiot"; oauth_store.py). The approval
              hash binds provider, alias AND the provider's account id, plus the draft id: an approval cannot move to
              another page, another draft, or an alias that was later pointed somewhere else.
  * media     each file's sha256 (media.py) is in the hash; at send time every file is re-hashed and must match, so an image
              swapped after approval is refused and nothing is posted. The provider's own upload receipt must report the same
              sha256 when it gives one.
  * health    labeled health text never reaches a public or workspace audience (health_taint.py hard block), checked when the
              draft is made and again at send.
  * limits    the manifest's rate_limit (per account: per_hour, per_day, cooldown) is checked when a review is requested and
              enforced at send; an uncertain outcome counts.
  * outcome   posted (with the provider's post id and link) | uncertain (the provider may have published: read back with
              /poststatus, never blind-retried; the idempotency key makes a provider-side retry a no-op) | failed (nothing
              went out) | refused (a check failed at send; nothing went out).

Adapters do the provider calls. Only the mock site's adapter exists (mock_social.py); real providers ship after the release.
"""
import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from ratelimit import keys_for

CONNECTOR = 'social'
REVIEW_TTL = 1800
VISIBILITY = {'public': {'public': 'Public — anyone on the internet can see this', 'connections': 'Connections/followers only (still outside your control once seen)'},
              'workspace': {'channel': 'Everyone in the channel'}}
TEXT_MAX = 3000
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
HIDDEN = re.compile('[​-‏‪-‮⁠-⁩﻿]')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def visible(text):
    return HIDDEN.sub(lambda m: '⟨U+%04X⟩' % ord(m.group()), str(text))


class PublishError(ValueError):
    pass


# ---------- adapters ----------
class MockSocialAdapter:
    """mock_social.py over HTTP (loopback). Synthetic accounts only."""
    provider = 'mock-social'
    kinds = ('image', 'video')

    def __init__(self, base, timeout=15):
        self.base, self.timeout = base.rstrip('/'), timeout

    def _req(self, method, path, token, body=None, ctype='application/json'):
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Authorization': 'Bearer ' + token, 'Content-Type': ctype, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or '{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or '{}')
            except ValueError:
                return e.code, {}

    def identities(self, token):
        st, me = self._req('GET', '/v1/me', token)
        st2, accts = self._req('GET', '/v1/me/accounts', token)
        if st != 200 or st2 != 200:
            raise PublishError('The mock site did not list the accounts.')
        return [{'external_id': me['id'], 'display': me['name'], 'kind': 'person'}] + [
            {'external_id': a['id'], 'display': a['name'], 'kind': a.get('kind', 'page')} for a in accts.get('accounts', [])]

    def publish(self, token, external_id, post, media_bytes, idem):
        media_ids = []
        for item, data in media_bytes:
            st, up = self._req('POST', '/v1/media', token, data, item['mime'])
            if st != 201:
                raise PublishError('Media upload refused (%s): %s' % (st, str(up.get('error', ''))[:80]))
            if up.get('sha256') and up['sha256'] != item['sha256']:
                raise PublishError('The site received different bytes for %s than were approved. Nothing was posted.' % item['name'])
            media_ids.append(up['media_id'])
        st, out = self._req('POST', '/v1/accounts/%s/posts' % external_id, token,
                            {'text': post['text'], 'visibility': post['visibility'], 'media_ids': media_ids, 'idempotency_key': idem})
        if st in (200, 201):
            return {'status': 'posted', 'post_id': out['id'], 'url': out.get('url')}
        if st == 429:
            return {'status': 'failed', 'error': 'The site refused: posting too often (429). Nothing was posted.', 'retry_after_s': out.get('retry_after_s')}
        if st in (400, 401, 403, 404, 413, 422):
            return {'status': 'failed', 'error': 'The site refused the post (%d): %s' % (st, str(out.get('error', ''))[:120])}
        raise PublishError('No clear answer from the site (%d).' % st)

    def find(self, token, external_id, idem):
        st, out = self._req('GET', '/v1/posts?idempotency_key=' + idem, token)
        if st != 200:
            raise PublishError('Read-back failed (%d).' % st)
        p = next((x for x in out.get('posts', []) if x.get('account') == external_id), None)
        return {'status': 'posted', 'post_id': p['id'], 'url': p.get('url')} if p else {'status': 'not_found'}


class Publisher:
    def __init__(self, path, oauth, media, registry=None, limiter=None, taint=None, telegram=None, adapters=None, now=time.time):
        """taint(text, audience) -> {'allowed', 'reason'}; telegram(text) -> owner review delivery."""
        self.path, self.oauth, self.media, self.registry, self.limiter = str(path), oauth, media, registry, limiter
        self.taint = taint or (lambda text, audience, media=(): {'allowed': True})
        self.telegram = telegram or (lambda text: None)
        self.adapters = adapters or {}
        self.now = now
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, request_id TEXT UNIQUE, provider TEXT, alias TEXT, external_id TEXT,
                audience TEXT, visibility TEXT, channel TEXT, text TEXT, media TEXT, hash TEXT, status TEXT, created REAL, reviewed REAL, sent REAL,
                result TEXT, error TEXT)''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def adapter(self, provider):
        a = self.adapters.get(provider)
        if not a:
            raise PublishError('Posting to %s is not built yet (its connector ships after the public release). Nothing was drafted.' % provider)
        return a

    def row(self, pid):
        if not isinstance(pid, str) or not re.fullmatch(r'p_[a-f0-9]{12}', pid):
            raise PublishError('Post ids look like p_ followed by 12 hex digits.')
        with self.db() as db:
            r = db.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()
        if not r:
            raise PublishError('Unknown post draft.')
        return r

    def public(self, r):
        media = json.loads(r['media'] or '[]')
        out = {'id': r['id'], 'provider': r['provider'], 'account': r['alias'], 'audience': r['audience'], 'visibility': r['visibility'],
               'text': r['text'], 'media': [{'name': m['name'], 'mime': m['mime'], 'bytes': m['bytes']} for m in media], 'status': r['status'],
               'posted': r['status'] == 'posted'}
        if r['channel']:
            out['channel'] = r['channel']
        if r['result']:
            res = json.loads(r['result'])
            out.update({k: res[k] for k in ('post_id', 'url') if k in res})
        if r['error']:
            out['error'] = r['error']
        if r['status'] == 'uncertain':
            out['note'] = 'The site may have published this. Do NOT draft it again or retry; the owner reads it back with /poststatus %s.' % r['id']
        if r['status'] == 'reviewed':
            out['note'] = 'Waiting for the owner\'s /postsend approval in Telegram (30 minutes). It is NOT posted.'
        return out

    def basis(self, r, media):
        return {'id': r['id'], 'provider': r['provider'], 'alias': r['alias'], 'external_id': r['external_id'], 'audience': r['audience'],
                'visibility': r['visibility'], 'channel': r['channel'], 'text': r['text'],
                'media': [{k: m[k] for k in ('sha256', 'name', 'mime', 'bytes')} for m in media]}

    def limits(self, provider, external_id):
        return keys_for(self.registry, CONNECTOR, '%s.%s' % (provider, external_id)) if self.registry else []

    # ---------- model-facing ----------
    def accounts(self, a):
        provider = a.get('provider')
        rows = self.oauth.accounts(provider) if provider else self.oauth.accounts()
        return {'accounts': [x for x in rows if x['provider'] in self.adapters or x['provider'] == provider],
                'note': 'Name the account by its alias in social_post_draft. Every post needs the owner\'s /postsend approval of the exact preview.'}

    def draft(self, a):
        rid = a.get('request_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', rid):
            raise PublishError('Use a stable request_id of 8-100 letters, digits, underscores or hyphens.')
        if set(a) - {'request_id', 'provider', 'account', 'text', 'visibility', 'media', 'audience', 'channel'}:
            raise PublishError('Unsupported post field.')
        provider = a.get('provider')
        adapter = self.adapter(provider)
        audience = a.get('audience', 'public')
        if audience not in VISIBILITY:
            raise PublishError('audience is public (a profile or page) or workspace (a channel).')
        visibility = a.get('visibility') or next(iter(VISIBILITY[audience]))
        if visibility not in VISIBILITY[audience]:
            raise PublishError('visibility for a %s post is %s.' % (audience, ' or '.join(VISIBILITY[audience])))
        channel = None
        if audience == 'workspace':
            channel = str(a.get('channel') or '')
            if not re.fullmatch(r'[A-Za-z0-9#_.-]{1,80}', channel):
                raise PublishError('A workspace message names its channel.')
        acct = self.oauth.account(provider, a.get('account'))
        text = a.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            raise PublishError('text is 1-%d characters, exactly as it will appear.' % TEXT_MAX)
        if CONTROL.search(text):
            raise PublishError('Unsupported control character in the text.')
        if re.search('[‪-‮⁦-⁩]', text):
            raise PublishError('Direction override characters are not supported.')
        media = self.media.bind(a.get('media'), kinds=adapter.kinds, max_files=10)
        verdict = self.taint(text + '\n' + '\n'.join(m['name'] for m in media), audience, [m['sha256'] for m in media])
        if not verdict.get('allowed', False):
            raise PublishError(verdict.get('reason') or 'This text carries labeled details; it cannot be posted. Nothing was drafted.')
        with self.db() as db:
            old = db.execute('SELECT * FROM posts WHERE request_id=?', (rid,)).fetchone()
        pid = old['id'] if old else 'p_' + secrets.token_hex(6)
        r = {'id': pid, 'provider': provider, 'alias': acct['alias'], 'external_id': acct['external_id'], 'audience': audience,
             'visibility': visibility, 'channel': channel, 'text': text}
        h = digest(self.basis(r, media))
        if old:
            if old['hash'] != h:
                raise PublishError('That request_id belongs to different content. Use a new request_id for an intentional edit.')
            return self.public(old)
        with self.db() as db:
            db.execute('INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)', (pid, rid, provider, acct['alias'], acct['external_id'], audience,
                       visibility, channel, text, json.dumps(media), h, 'draft', self.now()))
        return self.public(self.row(pid))

    def review(self, a):
        r = self.row(a.get('id'))
        if r['status'] not in ('draft', 'reviewed'):
            raise PublishError('This draft cannot be reviewed (status %s).' % r['status'])
        hit = self.limiter.check(self.limits(r['provider'], r['external_id'])) if self.limiter else None
        if hit:
            raise PublishError(hit['reason'].replace('Nothing was sent.', 'No review was sent.'))
        acct = self.oauth.account(r['provider'], r['alias'])
        media = json.loads(r['media'] or '[]')
        for m in media:
            self.media.read_verified(m)
        head = 'PUBLIC POST REVIEW' if r['audience'] == 'public' else 'WORKSPACE MESSAGE REVIEW'
        lines = [head + ' — ' + r['id'],
                 'Account: %s (%s, alias "%s", %s id %s)' % (visible(acct['display']), r['provider'], r['alias'], acct['kind'], r['external_id']),
                 'Visibility: ' + VISIBILITY[r['audience']][r['visibility']] + (' — channel %s' % r['channel'] if r['channel'] else '')]
        lines.append('Media: none' if not media else 'Media (%d, bound by sha256):\n%s' % (len(media), '\n'.join(self.media.preview_lines(media))))
        lines += ['', 'Text, exactly as it will appear (%d characters):' % len(r['text']), visible(r['text']), '',
                  'Approve within 30 minutes by sending this ENTIRE line:', '/postsend %s %s' % (r['id'], r['hash'][:16]),
                  'Cancel: /postcancel ' + r['id'], 'Any edit needs a new draft and a new approval.']
        text = '\n'.join(lines)
        if len(text) > 3900:
            raise PublishError('The preview is too long for one complete Telegram message; shorten the text.')
        self.telegram(text)
        with self.db() as db:
            db.execute("UPDATE posts SET status='reviewed', reviewed=? WHERE id=?", (self.now(), r['id']))
        return {'id': r['id'], 'status': 'reviewed', 'review_delivered_to_owner': True, 'posted': False}

    def cancel(self, a):
        r = self.row(a.get('id'))
        with self.db() as db:
            db.execute("UPDATE posts SET status='cancelled' WHERE id=? AND status IN ('draft','reviewed')", (r['id'],))
        return self.public(self.row(r['id']))

    def status(self, a, privileged=False):
        r = self.row(a.get('id'))
        if r['status'] == 'uncertain':
            try:
                adapter = self.adapter(r['provider'])
                tok = self.oauth.access_token(r['provider'], r['alias'])
                found = adapter.find(tok, r['external_id'], self.idem(r))
                if found.get('status') == 'posted':
                    with self.db() as db:
                        db.execute("UPDATE posts SET status='posted', result=?, error=NULL WHERE id=?", (json.dumps(found), r['id']))
                elif found.get('status') == 'not_found' and privileged and self.now() - (r['sent'] or 0) > 120:
                    with self.db() as db:
                        db.execute("UPDATE posts SET status='failed', error=? WHERE id=?", ('Read back: the site has no such post. Nothing was published.', r['id']))
            except Exception:
                pass
        return self.public(self.row(r['id']))

    @staticmethod
    def idem(r):
        return 'procurator-post-%s-%s' % (r['id'], r['hash'][:16])

    # ---------- owner only ----------
    def send(self, a):
        r = self.row(a.get('id'))
        if not isinstance(a.get('hash'), str) or a['hash'] != r['hash'][:16]:
            raise PublishError('Approval does not match this exact post. Nothing was posted.')
        if r['status'] in ('posted', 'sending', 'uncertain'):
            return {**self.public(r), 'already': True}
        if r['status'] != 'reviewed' or not r['reviewed'] or not 0 <= self.now() - r['reviewed'] <= REVIEW_TTL:
            raise PublishError('A fresh review is required (approvals last 30 minutes). Nothing was posted.')

        def refuse(reason):
            with self.db() as db:
                db.execute("UPDATE posts SET status='refused', error=? WHERE id=?", (reason[:300], r['id']))
            raise PublishError(reason)

        media = json.loads(r['media'] or '[]')
        if digest(self.basis(r, media)) != r['hash']:
            refuse('The stored draft no longer matches its approval hash. Nothing was posted.')
        try:
            acct = self.oauth.account(r['provider'], r['alias'])
        except ValueError as e:
            refuse(str(e) + ' Nothing was posted.')
        if acct['external_id'] != r['external_id']:
            refuse('The alias "%s" now points to a different account than the one approved. Nothing was posted.' % r['alias'])
        verdict = self.taint(r['text'] + '\n' + '\n'.join(m['name'] for m in media), r['audience'], [m['sha256'] for m in media])
        if not verdict.get('allowed', False):
            refuse(verdict.get('reason') or 'Health details: not posted.')
        try:
            payload = [(m, self.media.read_verified(m)) for m in media]
        except ValueError as e:
            refuse(str(e))
        keys = self.limits(r['provider'], r['external_id'])
        hit = self.limiter.check(keys) if self.limiter else None
        if hit:
            raise PublishError(hit['reason'] + ' The approval stays valid until it expires.')
        adapter = self.adapter(r['provider'])
        tok = self.oauth.access_token(r['provider'], r['alias'])
        with self.db() as db:
            db.execute("UPDATE posts SET status='sending', sent=? WHERE id=?", (self.now(), r['id']))
        try:
            out = adapter.publish(tok, r['external_id'], {'text': r['text'], 'visibility': r['visibility'], 'channel': r['channel']}, payload, self.idem(r))
            status = out['status']
        except Exception as e:
            out, status = {'error': 'No clear answer from the site: %s' % str(e)[:120]}, 'uncertain'
        if status in ('posted', 'uncertain') and self.limiter:
            self.limiter.record([k for k, _ in keys], r['id'])
        with self.db() as db:
            db.execute('UPDATE posts SET status=?, result=?, error=? WHERE id=?', (status, json.dumps(out) if status == 'posted' else None,
                                                                                  out.get('error') if status != 'posted' else None, r['id']))
        return self.public(self.row(r['id']))

    def call(self, action, a, privileged=False):
        a = a if isinstance(a, dict) else {}
        if action == 'social_accounts':
            return self.accounts(a)
        if action == 'social_post_draft':
            return self.draft(a)
        if action == 'social_post_review':
            return self.review(a)
        if action == 'social_post_status':
            return self.status(a, privileged)
        if action == 'social_post_cancel':
            return self.cancel(a)
        if action == 'social_post_send':
            if not privileged:
                raise PublishError('Posting is available only through the owner\'s /postsend approval in Telegram.')
            return self.send(a)
        if action == 'social_post_list':
            with self.db() as db:
                rows = db.execute('SELECT * FROM posts ORDER BY created DESC LIMIT 20').fetchall()
            return {'posts': [self.public(r) for r in rows]}
        raise PublishError('Unknown social action.')
