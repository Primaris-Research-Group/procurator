#!/usr/bin/env python3
"""Mock social site (web 0.27.0): a synthetic stand-in for a social platform, off by default.

Like the mock EHR, it lets the test battery exercise the real flows end to end with no real account: OAuth2 with PKCE, several
postable accounts behind one sign-in, media upload with a server-side sha256 receipt, posts with idempotency keys, comments,
and signed webhooks for inbound events. ALL DATA IS SYNTHETIC.

  OAuth   GET  /oauth/authorize   (test mode approves at once and redirects with code + state; exact redirect_uri match;
                                   PKCE S256 required)
          POST /oauth/token       authorization_code (single-use code) | refresh_token (rotated)
          POST /oauth/revoke
  API     GET  /v1/me, /v1/me/accounts                     (Bearer)
          POST /v1/media                                   raw bytes -> {media_id, sha256, bytes}
          POST /v1/accounts/<id>/posts                     {text, visibility, media_ids, idempotency_key} -> 201 {id, url}
                                                           403 when the token's user does not manage <id>; 429 when posting
                                                           faster than min_interval_s for that account
          GET  /v1/posts?idempotency_key=K, /v1/posts/<id>
  Admin   POST /admin/comment {post, text, sender}        stores a comment and delivers a signed webhook (X-Procurator-Signature)
          POST /admin/fault {...}                          drop_after_commit: next post is stored, then the connection drops
          GET  /admin/state                                posts, comments, uploads (for assertions)
  Admin calls need the admin token (header X-Admin-Token) and loopback.

Accounts: user u1 "Test Owner" manages p1 "Acme Engineering (synthetic)", p2 "Primaris (synthetic)", p3 "ZenithIoT (synthetic)".
User u2 "Someone Else" manages p9 "Other Page (synthetic)".

  python3 mock_social.py serve [--port 3355] [--redirect URL ...] [--webhook URL] [--secret-file PATH]
"""
import base64
import hashlib
import http.server
import json
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request

import events as events_mod

USERS = {'u1': {'name': 'Test Owner', 'pages': ['p1', 'p2', 'p3']}, 'u2': {'name': 'Someone Else', 'pages': ['p9']}}
PAGES = {'p1': 'Acme Engineering (synthetic)', 'p2': 'Primaris (synthetic)', 'p3': 'ZenithIoT (synthetic)', 'p9': 'Other Page (synthetic)'}


class Drop(Exception):
    pass


class Social:
    def __init__(self, redirects=(), user='u1', min_interval_s=60, admin_token=None, webhook=None, secret=None, now=time.time):
        self.redirects, self.user, self.min_interval, self.now = set(redirects), user, min_interval_s, now
        self.admin_token = admin_token or secrets.token_urlsafe(24)
        self.webhook, self.secret = webhook, secret or secrets.token_urlsafe(24)   # webhook: URL or callable(headers, body)
        self.codes, self.tokens, self.refresh = {}, {}, {}
        self.media, self.posts, self.comments, self.faults = {}, {}, [], {}
        self.lock = threading.Lock()

    # ---------- oauth ----------
    def authorize(self, q):
        uri = q.get('redirect_uri', '')
        if uri not in self.redirects:
            return 400, {'error': 'redirect_uri not registered'}, None
        if q.get('response_type') != 'code' or q.get('code_challenge_method') != 'S256' or not q.get('code_challenge'):
            return 400, {'error': 'PKCE S256 required'}, None
        code = secrets.token_urlsafe(24)
        self.codes[code] = {'user': self.user, 'challenge': q['code_challenge'], 'redirect_uri': uri, 'at': self.now()}
        return 302, {}, uri + ('&' if '?' in uri else '?') + urllib.parse.urlencode({'code': code, 'state': q.get('state', '')})

    def issue(self, user):
        tok, ref = 'at_' + secrets.token_urlsafe(24), 'rt_' + secrets.token_urlsafe(24)
        self.tokens[tok] = {'user': user, 'exp': self.now() + 3600}
        self.refresh[ref] = user
        return {'access_token': tok, 'refresh_token': ref, 'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'profile pages publish comments'}

    def token(self, f):
        if f.get('grant_type') == 'authorization_code':
            c = self.codes.pop(f.get('code', ''), None)
            if not c or self.now() - c['at'] > 300 or c['redirect_uri'] != f.get('redirect_uri'):
                return 400, {'error': 'invalid_grant'}
            want = base64.urlsafe_b64encode(hashlib.sha256(f.get('code_verifier', '').encode()).digest()).rstrip(b'=').decode()
            if want != c['challenge']:
                return 400, {'error': 'invalid_grant', 'error_description': 'PKCE verifier mismatch'}
            return 200, self.issue(c['user'])
        if f.get('grant_type') == 'refresh_token':
            user = self.refresh.pop(f.get('refresh_token', ''), None)
            return (200, self.issue(user)) if user else (400, {'error': 'invalid_grant'})
        return 400, {'error': 'unsupported_grant_type'}

    def who(self, headers):
        auth = headers.get('Authorization') or headers.get('authorization') or ''
        t = self.tokens.get(auth[7:]) if auth.startswith('Bearer ') else None
        return t['user'] if t and t['exp'] > self.now() else None

    # ---------- api ----------
    def handle(self, method, path, query, headers, body):
        if path == '/oauth/token' and method == 'POST':
            return self.token(dict(urllib.parse.parse_qsl(body.decode())))
        if path == '/oauth/revoke' and method == 'POST':
            f = dict(urllib.parse.parse_qsl(body.decode()))
            self.refresh.pop(f.get('token', ''), None)
            self.tokens.pop(f.get('token', ''), None)
            return 200, {'revoked': True}
        if path.startswith('/admin/'):
            if headers.get('X-Admin-Token') != self.admin_token:
                return 403, {'error': 'admin token'}
            return self.admin(method, path, body)
        user = self.who(headers)
        if not user:
            return 401, {'error': 'invalid token'}
        if path == '/v1/me':
            return 200, {'id': user, 'name': USERS[user]['name']}
        if path == '/v1/me/accounts':
            return 200, {'accounts': [{'id': p, 'name': PAGES[p], 'kind': 'page'} for p in USERS[user]['pages']]}
        if path == '/v1/media' and method == 'POST':
            if not body or len(body) > 50 * 1024 * 1024:
                return 413, {'error': 'empty or too large'}
            mid = 'md_' + secrets.token_hex(6)
            self.media[mid] = {'user': user, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body), 'mime': headers.get('Content-Type')}
            return 201, {'media_id': mid, 'sha256': self.media[mid]['sha256'], 'bytes': len(body)}
        m = re.fullmatch(r'/v1/accounts/([a-z0-9]+)/posts', path)
        if m and method == 'POST':
            acct = m.group(1)
            if acct != user and acct not in USERS[user]['pages']:
                return 403, {'error': 'this token does not manage that account'}
            p = json.loads(body or b'{}')
            key = p.get('idempotency_key')
            with self.lock:
                same = next((x for x in self.posts.values() if key and x['idempotency_key'] == key), None)
                if same:
                    return 200, same
                last = max([x['at'] for x in self.posts.values() if x['account'] == acct] or [0])
                if self.now() - last < self.min_interval:
                    return 429, {'error': 'posting too often', 'retry_after_s': int(self.min_interval - (self.now() - last)) + 1}
                for mid in p.get('media_ids') or []:
                    if mid not in self.media or self.media[mid]['user'] != user:
                        return 422, {'error': 'unknown media'}
                pid = 'post_' + secrets.token_hex(5)
                post = {'id': pid, 'account': acct, 'text': p.get('text', ''), 'visibility': p.get('visibility'), 'media': [self.media[x]['sha256'] for x in p.get('media_ids') or []],
                        'idempotency_key': key, 'at': self.now(), 'url': 'https://mock-social.invalid/%s/%s' % (acct, pid)}
                self.posts[pid] = post
                if self.faults.pop('drop_after_commit', False):
                    raise Drop()
            return 201, post
        if path == '/v1/posts':
            key = (query.get('idempotency_key') or [''])[0]
            return 200, {'posts': [x for x in self.posts.values() if x['idempotency_key'] == key]}
        m = re.fullmatch(r'/v1/posts/(post_[a-f0-9]+)', path)
        if m:
            return (200, self.posts[m.group(1)]) if m.group(1) in self.posts else (404, {'error': 'not found'})
        return 404, {'error': 'not found'}

    def admin(self, method, path, body):
        if path == '/admin/state':
            return 200, {'posts': list(self.posts.values()), 'comments': self.comments, 'uploads': len(self.media)}
        p = json.loads(body or b'{}')
        if path == '/admin/fault':
            self.faults.update(p)
            return 200, {'faults': self.faults}
        if path == '/admin/comment':
            post = self.posts.get(p.get('post'))
            c = {'id': 'c_' + secrets.token_hex(5), 'post': p.get('post'), 'account': post['account'] if post else p.get('account'), 'kind': p.get('kind', 'comment'),
                 'text': str(p.get('text', ''))[:4000], 'sender': {'id': 'x_' + secrets.token_hex(3), 'display': str(p.get('sender', 'A Commenter'))[:80]},
                 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.now()))}
            self.comments.append(c)
            delivered = self.deliver(c)
            return 201, {'comment': c, 'webhook': delivered}
        return 404, {'error': 'not found'}

    def deliver(self, c):
        if not self.webhook:
            return 'no webhook configured'
        body = json.dumps(c).encode()
        ts = str(int(self.now()))
        headers = {'Content-Type': 'application/json', 'X-Procurator-Timestamp': ts, 'X-Procurator-Signature': events_mod.sign_generic(self.secret, ts, body)}
        if callable(self.webhook):
            return self.webhook(headers, body)
        try:
            req = urllib.request.Request(self.webhook, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except Exception as e:
            return 'failed: %s' % str(e)[:80]


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def make_server(site, host='127.0.0.1', port=0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def go(self, method):
            path, _, qs = self.path.partition('?')
            q = urllib.parse.parse_qs(qs)
            if path == '/oauth/authorize' and method == 'GET':
                st, out, loc = site.authorize({k: v[0] for k, v in q.items()})
                if st == 302:
                    self.send_response(302)
                    self.send_header('Location', loc)
                    self.end_headers()
                    return
                return self.reply(st, out)
            if path.startswith('/admin/') and self.client_address[0] not in ('127.0.0.1', '::1'):
                return self.reply(403, {'error': 'loopback only'})
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            try:
                st, out = site.handle(method, path, q, dict(self.headers), body)
            except Drop:
                self.close_connection = True
                return
            self.reply(st, out)

        def reply(self, st, out):
            data = json.dumps(out).encode()
            self.send_response(st)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.go('GET')

        def do_POST(self):
            self.go('POST')
    return Server((host, port), Handler)


def serve_in_thread(site, host='127.0.0.1'):
    srv = make_server(site, host)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, 'http://%s:%d' % (host, srv.server_address[1])


def main(argv):
    if len(argv) < 2 or argv[1] != 'serve':
        print(__doc__)
        return 2
    args = argv[2:]
    port, redirects, webhook, secret = 3355, [], None, None
    while args:
        k = args.pop(0)
        if k == '--port':
            port = int(args.pop(0))
        elif k == '--redirect':
            redirects.append(args.pop(0))
        elif k == '--webhook':
            webhook = args.pop(0)
        elif k == '--secret-file':
            secret = open(args.pop(0)).read().strip()
    site = Social(redirects, webhook=webhook, secret=secret)
    print('mock-social on 127.0.0.1:%d (admin token printed once): %s' % (port, site.admin_token), flush=True)
    make_server(site, '127.0.0.1', port).serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
