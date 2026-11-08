"""Owner-connected saved logins for ordinary websites (one isolated Browserbase context per site).

The owner signs in privately through a live-view link; the bot never sees or stores
passwords or codes. Later browsers for that site start already signed in. Actions are
still governed by task grants and reviews; account-changing actions always need review.
"""
import hashlib
import os
import re
import secrets
import time
import urllib.parse

NAME = re.compile(r'[a-z0-9][a-z0-9-]{1,29}')
LOGIN_PATH = re.compile(r'/(?:auth|login|log-in|signin|sign-in|sso|oauth|identity|account/login)(?:/|$|\?)', re.I)


# Well-known sign-in starting points, so the owner can send just "/siteconnect leaveco".
# The owner signs in from there inside the private browser (e.g. taps "Log in").
PRESETS = {'leaveco': 'https://www.leaveco.example/', 'hotels': 'https://www.hotels.com/', 'amazon': 'https://www.amazon.com/',
           'target': 'https://www.target.com/', 'expedia': 'https://www.expedia.com/', 'publix': 'https://www.publix.com/',
           'cvs': 'https://www.cvs.com/', 'walgreens': 'https://www.walgreens.com/'}
_SECOND_LEVEL = {'co', 'com', 'net', 'org', 'gov', 'edu', 'ac', 'gob', 'mil'}


def site_domain(origin):
    """Registrable domain of an origin: https://services.leaveco.example -> leaveco.example (bbc.co.uk kept whole)."""
    host = (urllib.parse.urlsplit(origin or '').hostname or '').lower()
    labels = host.split('.')
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return '.'.join(labels[-3:])
    return '.'.join(labels[-2:])


# Hosting platforms where each subdomain belongs to a different owner: browsing stays on the exact origin.
_MULTI_TENANT = {'github.io', 'herokuapp.com', 'vercel.app', 'netlify.app', 'myshopify.com', 'wixsite.com', 'squarespace.com',
                 'blogspot.com', 'wordpress.com', 'pages.dev', 'web.app', 'firebaseapp.com', 'azurewebsites.net', 'cloudfront.net',
                 'appspot.com', 'webflow.io', 'glitch.me', 'onrender.com', 'fly.dev', 'workers.dev', 'amazonaws.com', 'googleusercontent.com'}


def browse_domain(origin):
    """The website a browser task may move around in: the registrable domain (tables.toasttab.com ->
    toasttab.com), except on shared hosting platforms. Nov 4: Mabella's Toast Tables booking moved from
    tables.toasttab.com to another toasttab.com host after the security check and the bot was locked out."""
    d = site_domain(origin)
    if not d or '.' not in d or d in _MULTI_TENANT or urllib.parse.urlsplit(origin or '').scheme != 'https':
        return None
    return d


def in_scope(session, origin):
    """Same origin, or (signed-in sessions only) any HTTPS host of the saved login's domain.
    Portals move between subdomains after sign-in (www → services → leave.leaveco.example)."""
    if not isinstance(origin, str):
        return False
    if origin == session.get('origin'):
        return True
    domain = session.get('site_domain')
    if not domain:
        return False
    p = urllib.parse.urlsplit(origin)
    host = (p.hostname or '').lower()
    return p.scheme == 'https' and p.port in (None, 443) and bool(host) and (host == domain or host.endswith('.' + domain))


def site_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('Use a short site name: 2–30 lowercase letters, digits or hyphens (e.g. leaveco).')
    return value


def looks_signed_out(snapshot):
    """A visible password field or a login/sign-in URL means the saved login has expired."""
    path = urllib.parse.urlsplit(snapshot.get('url', '')).path
    if LOGIN_PATH.search(path + '/'):
        return True
    return any(c.get('type') == 'password' for c in snapshot.get('controls', []))


class SiteLogins:
    def __init__(self, broker):
        self.b = broker
        with broker.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS site_logins(name TEXT PRIMARY KEY,start_url TEXT,origin TEXT,context_id TEXT,status TEXT,updated REAL,session_id TEXT,sequence INTEGER);
            CREATE TABLE IF NOT EXISTS site_sessions(session_id TEXT PRIMARY KEY,name TEXT);
            CREATE TABLE IF NOT EXISTS owner_pages(page_hash TEXT PRIMARY KEY,session_id TEXT,site TEXT,expires REAL,used INTEGER DEFAULT 0,created REAL);''')
            cols = [r['name'] for r in db.execute('PRAGMA table_info(site_logins)')]
            if 'signin_hash' not in cols:
                db.execute('ALTER TABLE site_logins ADD COLUMN signin_hash TEXT')
                db.execute('ALTER TABLE site_logins ADD COLUMN signin_expires REAL')
        self._key_counts = {}
        self._sent_at = {}
        self._shot_at = {}
        self._events = []      # finished owner sign-ins, taken once by the gateway plugin to wake the assistant

    def row(self, name):
        with self.b.db() as db:
            r = db.execute('SELECT * FROM site_logins WHERE name=?', (site_name(name),)).fetchone()
        return dict(r) if r else None

    def home_login_for(self, origin):
        """0.20.3: the saved login for this website that the owner signed in at the PC, while the PC is online (else None)."""
        lanes = getattr(self.b, 'lanes', None)
        if not lanes or not origin or origin == 'practice':
            return None
        domain = site_domain(origin)
        with self.b.db() as db:
            rows = db.execute('SELECT name,origin FROM site_logins').fetchall()
        for r in rows:
            if site_domain(r['origin']) == domain and lanes.ready_online(r['name'], r['origin']):
                return r['name']
        return None

    def drop_cloud_signin(self, r):
        """0.20.3 (Nov 6 hotels.com): a cloud sign-in link was sent for a site the owner then used on their PC. The PC wins: the link
        stops working and the login no longer counts as "owner signing in", which had blocked the PC browser's snapshots."""
        if not r or r['status'] != 'awaiting_owner_sign_in':
            return False
        status = 'home_only' if not r['context_id'] else 'reauthentication_required'
        with self.b.db() as db:
            db.execute('UPDATE site_logins SET status=?,signin_hash=NULL,signin_expires=NULL,updated=? WHERE name=?', (status, time.time(), r['name']))
        return True

    def open_session(self, r):
        """The browser this saved login is attached to, if it is still open (else None)."""
        if not r or not r.get('session_id'):
            return None
        try:
            s = self.b.session(r['session_id'])
        except Exception:
            return None
        if s.get('status') != 'open' or s.get('site_login') != r['name']:
            return None
        if (s.get('created') or 0) + (s.get('timeout_seconds') or 0) < time.time() + 60:
            return None
        return s

    def public(self, r):
        out = {'site': r['name'], 'start_url': r['start_url'], 'origin': r['origin'], 'status': r['status'],
               'updated': r['updated'], 'signed_in_browsing': r['status'] in ('owner_connected', 'home_only')}
        if r['status'] == 'home_only':
            out['where'] = 'home PC only'
            out['note'] = 'Signed in on their PC (/homesignin). Open with web_browser_open site_login=' + r['name'] + '; it runs on the PC. If the PC shows a sign-in page, call web_send_signin_link: the owner is asked to sign in at the PC.'
            return out
        s = self.open_session(r) if r['status'] == 'owner_connected' else None
        if s:
            # Nov 5 Leaveco: a login made in one cloud browser did not carry over to the next one, so the browser the owner
            # signed in to stays open and the assistant continues in it.
            out['open_browser'] = s['id']
            out['open_browser_note'] = 'The owner signed in to this open browser. Continue there (web_browser_snapshot id ' + s['id'] + '); do not open a new one.'
        return out

    def context(self, name):
        r = self.row(name)
        if r and r['status'] == 'home_only':
            return r      # signed in on their PC only (/homesignin NAME URL); no cloud context
        if not r or not r['context_id']:
            raise ValueError('There is no saved login called "' + name + '" yet. Ask the assistant for a ' + name + ' sign-in link '
                             '(assistant: call web_send_signin_link with site ' + name + ').')
        return r

    def mark(self, name, status):
        with self.b.db() as db:
            db.execute('UPDATE site_logins SET status=?,updated=? WHERE name=?', (status, time.time(), name))
        if status == 'owner_connected' and hasattr(self.b, 'log'):
            self.b.log({'key': 'signin-%s-%d' % (''.join(c for c in name if c.isalnum() or c in '-_.')[:40], int(time.time() // 60)), 'kind': 'signin', 'status': 'completed',
                        'title': 'You signed in to ' + name + '; saved login ready for signed-in browsing',
                        'details': 'You signed in privately on your sign-in page; the assistant never saw or typed the password or codes.'})

    def takeover_page(self, session_id):
        """Nov 6 (Leaveco, again): takeover links used to be the raw Browserbase live view, which has no iPhone keyboard and no
        password manager. Every takeover now gets the private phone page (picture view, typing boxes, password-manager
        sign-in, code box; live view on a computer) bound to that one browser. Owner-only; the link never reaches the bot."""
        s = self.b.session(session_id)
        if s.get('status') != 'open' or not s.get('provider_id'):
            raise ValueError('Browser session is not open.')
        if str(s['provider_id']).startswith('home:'):
            raise ValueError('This browser runs on the PC at home; take over there (or by remote desktop).')
        now = time.time()
        expires = min(now + 45 * 60, (s.get('created') or now) + (s.get('timeout_seconds') or 900) - 30)
        if expires < now + 60:
            raise ValueError('This browser closes in less than a minute; ask the assistant to open a fresh one.')
        site = browse_domain(s.get('url') or '') or 'the website'
        token = secrets.token_urlsafe(32)
        with self.b.db() as db:
            db.execute('UPDATE owner_pages SET used=1 WHERE session_id=? AND used=0', (s['id'],))   # only the newest link works
            db.execute('DELETE FROM owner_pages WHERE expires<?', (now - 86400,))
            db.execute('INSERT INTO owner_pages(page_hash,session_id,site,expires,used,created) VALUES(?,?,?,?,0,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), s['id'], site[:80], expires, now))
        return {'id': s['id'], 'site': site, 'page_url': self.signin_base() + token, 'minutes': max(1, int((expires - now) // 60))}

    def signin(self, token):
        """The pending sign-in (or takeover page) a phone-page token belongs to, or an error."""
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}', token):
            raise ValueError('This sign-in link is not valid.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.b.db() as db:
            r = db.execute('SELECT * FROM site_logins WHERE signin_hash=?', (digest,)).fetchone()
            page = None if r else db.execute('SELECT * FROM owner_pages WHERE page_hash=?', (digest,)).fetchone()
        if page:
            if page['used'] or page['expires'] < time.time():
                raise ValueError('This link has expired or was already used. Ask the assistant for a new takeover link.')
            s = self.b.session(page['session_id'])
            if s.get('status') != 'open':
                raise ValueError('That browser has closed. Ask the assistant for a new takeover link.')
            return {'name': page['site'], 'signin_hash': digest, 'kind': 'takeover'}, s
        if not r or r['status'] != 'awaiting_owner_sign_in' or not r['session_id'] or (r['signin_expires'] or 0) < time.time():
            raise ValueError('This sign-in link has expired or was already used. Send /siteconnect NAME in Telegram for a new one.')
        s = self.b.session(r['session_id'])
        if s.get('status') != 'open':
            raise ValueError('The private browser for this sign-in has closed. Send /siteconnect ' + r['name'] + ' for a new one.')
        return {**dict(r), 'kind': 'signin'}, s

    def signin_call(self, action, args):
        r, s = self.signin(args.get('token'))
        if action == 'site_signin_page':
            link = self.b.call('takeover', {'id': s['id']}, privileged=True)
            return {'site': r['name'], 'live_view_url': link['live_view_url'], 'kind': r['kind']}
        if action == 'site_signin_shot':
            # Picture of the private browser for their phone (the embedded live view blocks the iPhone keyboard).
            # Only ever returned to the sign-in page, never to the bot.
            last = self._shot_at.get(r['signin_hash'], 0)
            if time.time() - last < 0.8:
                raise ValueError('Too fast.')
            self._shot_at[r['signin_hash']] = time.time()
            out = self.b.provider.run(s, 'owner_shot')
            if not out.get('ok') or not isinstance(out.get('image_b64'), str):
                raise ValueError('The picture could not be taken. Try again.')
            return {'ok': True, 'image_b64': out['image_b64'], 'host': str(out.get('host') or '')[:120]}
        if action in ('site_signin_tap', 'site_signin_scroll'):
            count = self._key_counts.get(r['signin_hash'], 0) + 1
            self._key_counts[r['signin_hash']] = count
            if count > 400:
                raise ValueError('Too many entries for one sign-in. Ask the assistant for a new link.')
            if action == 'site_signin_scroll':
                direction = args.get('direction')
                if direction not in ('up', 'down'):
                    raise ValueError('Scroll up or down.')
                out = self.b.provider.run(s, 'owner_scroll', direction=direction)
            else:
                x, y = args.get('x'), args.get('y')
                if not all(type(v) in (int, float) and 0 <= v <= 1 for v in (x, y)):
                    raise ValueError('Tap inside the picture.')
                out = self.b.provider.run(s, 'owner_tap', x=float(x), y=float(y))
            if not out.get('ok'):
                raise ValueError('That did not reach the page. Try again.')
            return {'ok': True}
        if action == 'site_signin_keys':
            text, key, clear = args.get('text') or '', args.get('key'), bool(args.get('clear'))
            if not isinstance(text, str) or len(text) > 256 or re.search(r'[\x00-\x1f\x7f]', text):
                raise ValueError('Type up to 256 characters at a time.')
            if key is not None and key not in ('Tab', 'Enter', 'Backspace', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space'):
                raise ValueError('Unsupported key.')
            count = self._key_counts.get(r['signin_hash'], 0) + 1
            self._key_counts[r['signin_hash']] = count
            if count > 200:
                raise ValueError('Too many entries for one sign-in. Send /siteconnect again for a new link.')
            out = self.b.provider.run(s, 'owner_keys', text=text, key=key, clear=clear)
            if not out.get('ok'):
                raise ValueError('The text could not be sent. Tap the field in the browser and try again.')
            return {'ok': True}
        if action in ('site_signin_login', 'site_signin_code'):
            count = self._key_counts.get(r['signin_hash'], 0) + 1
            self._key_counts[r['signin_hash']] = count
            if count > 200:
                raise ValueError('Too many entries for one sign-in. Send /siteconnect again for a new link.')
            if action == 'site_signin_code':
                code = args.get('code')
                if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,12}', code.strip()):
                    raise ValueError('Enter the code exactly as you received it.')
                out = self.b.provider.run(s, 'owner_code', code=code.strip())
            else:
                user, password = args.get('username') or '', args.get('password')
                for v in (user, password):
                    if not isinstance(v, str) or len(v) > 256 or re.search(r'[\x00-\x1f\x7f]', v):
                        raise ValueError('That entry is not valid.')
                if not password:
                    raise ValueError('Fill the password first (your password manager can do it).')
                out = self.b.provider.run(s, 'owner_login', username=user, password=password)
                user = password = None
            if out.get('ok'):
                return {'ok': True, 'message': 'Submitted. Check the page above; if it asks for a code, use the code line.'}
            reason = out.get('reason')
            raise ValueError({
                'no_login_form': 'I could not find a sign-in box on the page above. Tap its Log in / Sign in link first, then try again.',
                'no_code_box': 'I could not find a code box. Tap it in the page above, then use the typing line instead.',
                'username_needed': 'This site asks for your user ID first; fill both lines and try again.',
                'password_box_not_shown': 'The user ID was sent but no password box appeared yet. Check the page above, then press Sign in again.',
            }.get(reason, 'It could not be filled. Tap the box in the page above and use the typing line instead.'))
        if action == 'site_signin_done' and r['kind'] == 'takeover':
            with self.b.db() as db:
                db.execute('UPDATE owner_pages SET used=1 WHERE page_hash=?', (r['signin_hash'],))
            if s.get('site_login'):
                # The owner may have signed in to a saved login in this browser: record it the same way the sign-in page does.
                try:
                    self.call('site_ready', {'site': s['site_login']}, True)
                except Exception:
                    pass
            self._events.append({'site': r['name'], 'browser': s['id'], 'at': time.time(), 'kind': 'takeover'})
            del self._events[:-20]
            try:
                self.b.provider.telegram('Done in browser ' + s['id'] + ' (from your private page). The assistant takes a fresh look and continues; no need to reply.')
            except Exception:
                pass
            return {'status': 'done'}
        if action == 'site_signin_done':
            done = self.call('site_ready', {'site': r['name']}, True)
            if done.get('status') == 'owner_connected':
                with self.b.db() as db:
                    db.execute('UPDATE site_logins SET signin_hash=NULL WHERE name=?', (r['name'],))
                kept = self.open_session(self.row(r['name']))
                if kept:
                    self._events.append({'site': r['name'], 'browser': kept['id'], 'at': time.time()})
                    del self._events[:-20]
                try:
                    self.b.provider.telegram('Signed in to ' + r['name'] + ' (confirmed from the sign-in page). ' +
                                             ('The assistant is continuing in that same browser; no need to reply.' if kept else
                                              'Saved login ready; the assistant can open it already signed in.'))
                except Exception:
                    pass
            return done
        raise ValueError('Unknown sign-in operation.')

    def request_signin(self, args):
        """Bot-requested sign-in: the broker sends the private link straight to the owner's Telegram.
        The model only learns that a link was sent, never the link itself (Nov 4: owner-typed
        /siteconnect commands were not reaching the command handler).
        Every request sends a link that works now (Nov 4, 7 pm: a link made in the terminal blocked
        the bot's request for 10 minutes, so the bot pointed them to a "link in this chat" that was
        never sent). Only the hash of a link is stored, so an earlier link cannot be resent: while
        the private browser is still open the link is re-issued for it (the old one stops working);
        otherwise a fresh sign-in starts."""
        name = site_name(args.get('site'))
        r, now, pending = self.row(name), time.time(), False
        home = self.home_signin_needed(name, r, args)
        if home:
            return home
        if r and r['status'] == 'awaiting_owner_sign_in' and r['session_id'] and (r['signin_expires'] or 0) > now + 600:
            try:
                pending = self.b.session(r['session_id']).get('status') == 'open'
            except Exception:
                pending = False
        inplace = None
        if not pending and r and r['status'] in ('owner_connected', 'reauthentication_required', 'signing_up'):
            inplace = self.open_session(r)
            if inplace and (inplace['created'] + inplace['timeout_seconds']) - now < 300:
                inplace = None
            if inplace:
                with self.b.db() as db:
                    busy = db.execute("SELECT id FROM plans WHERE session_id=? AND status IN ('executing','uncertain','verification_pending') LIMIT 1",
                                      (inplace['id'],)).fetchone()
                if busy:
                    raise ValueError('An action is still being checked in browser ' + inplace['id'] + '; resolve it before asking them to sign in.')
        if inplace:
            # Nov 5 Leaveco: a sign-in in a separate private browser did not carry over to the next browser (twice), so the
            # owner signs in to the SAME browser the assistant is using, then the assistant continues there.
            creating = r['status'] == 'signing_up'
            token = secrets.token_urlsafe(32)
            expires = min(now + 45 * 60, inplace['created'] + inplace['timeout_seconds'] - 60)
            with self.b.db() as db:
                db.execute("UPDATE site_logins SET status='awaiting_owner_sign_in',signin_hash=?,signin_expires=?,updated=? WHERE name=?",
                           (hashlib.sha256(token.encode()).hexdigest(), expires, now, name))
            getattr(self, '_ready_checked', set()).discard((name, inplace['id']))
            self._sent_at[name] = now
            minutes = str(max(1, int((expires - now) // 60)))
            if creating:
                # Nov 6 Publix: account creation. The assistant filled the ordinary fields; the owner sets the password, enters any
                # code and presses the create button themselves, so no account is made in their name without their tap.
                self.b.provider.telegram('Finish creating your ' + name + ' account (private link, works on your phone):\n\n' + self.signin_base() + token +
                                         '\n\nThis opens the sign-up page the assistant has been filling in. Check its entries, choose your password '
                                         '(let your iPhone suggest a strong one and save it), enter any texted or emailed code, and tap the site\'s '
                                         'Create account / Sign up button yourself. When you can see your new account, tap "I\'m signed in" and the assistant '
                                         'continues in the same browser. It never sees your password or codes. The link works for ' + minutes + ' minutes.')
            else:
                self.b.provider.telegram('Sign in to ' + name + ' (private link, works on your phone):\n\n' + self.signin_base() + token +
                                     '\n\nThis opens the browser the assistant is already using. Tap Sign in and let your iPhone fill your saved password, '
                                     'then tap Sign in; a texted code goes in the code box. You can also tap the page picture to press its buttons. When you '
                                     'can see your account, tap "I\'m signed in" and the assistant continues by itself. It never sees what you enter. '
                                     'The link works for ' + minutes + ' minutes.')
            return {'site': name, 'sent': True, 'browser': inplace['id'],
                    'note': 'The private sign-in link for browser ' + inplace['id'] + ' (the one you are using) was sent to the owner in Telegram. '
                    'You cannot read this browser until the owner finishes. When the owner taps "I\'m signed in" you are woken automatically: then take a fresh '
                    'web_browser_snapshot of the SAME browser and continue. Do not open a new browser. Do not ask for passwords or codes.'}
        if pending and now - self._sent_at.get(name, 0) < 90:
            return {'site': name, 'sent': False, 'note': 'A working sign-in link for ' + name + ' went to their Telegram less than 2 minutes ago. '
                    'Ask them to open the newest sign-in message there. Do not ask for passwords or codes.'}
        if pending:
            token = secrets.token_urlsafe(32)
            with self.b.db() as db:
                db.execute('UPDATE site_logins SET signin_hash=? WHERE name=?', (hashlib.sha256(token.encode()).hexdigest(), name))
            url, minutes = self.signin_base() + token, int((r['signin_expires'] - now) // 60)
        else:
            result = self.call('site_connect', {'site': name, **({'url': args['url']} if args.get('url') else {})}, True)
            url, minutes = result['signin_url'], 45
        self._sent_at[name] = now
        self.b.provider.telegram('Sign in to ' + name + ' (private link, works on your phone):\n\n' + url +
                                 '\n\nOpen it, tap Sign in and let your iPhone fill your saved password, then tap Sign in. '
                                 'A texted code goes in the code box; you can also tap the page picture to press its buttons. When you can see your account, tap "I\'m signed in". '
                                 'The assistant never sees what you enter. The link works for ' + str(minutes) + ' minutes; '
                                 'any earlier sign-in link for ' + name + ' no longer works.')
        return {'site': name, 'sent': True, 'note': 'The private sign-in link was sent to the owner in Telegram just now. Tell them to open the newest '
                'sign-in message there. When the owner taps "I\'m signed in" you are woken automatically; then check web_site_logins: it lists the '
                'open_browser the owner signed in to. Continue in THAT browser (fresh web_browser_snapshot), do not open a new one. Do not ask for passwords or codes.'}

    def home_signin_needed(self, name, r, args):
        """Nov 6 (Leaveco): a site that runs on their PC is signed in at the PC (/homesignin), never through a cloud link. Returns
        the answer for the bot (and tells them in Telegram, with a copy button), or None when the cloud sign-in applies."""
        lanes = getattr(self.b, 'lanes', None)
        origin = (r or {}).get('origin') or ''
        if not origin and args.get('url'):
            try:
                origin = self.b.url_validator(args['url'])[1]
            except Exception:
                origin = ''
        domain = site_domain(origin) if origin else ''
        # 0.20.3 (Nov 6 hotels.com): a login the owner signed in at the PC counts too, even with no /weblane rule (the assistant's
        # sign-in request never sets one), so the assistant is not sent a cloud link for a site that already works at home.
        if not lanes or not domain or not ((r or {}).get('status') == 'home_only' or lanes.wants_home(domain) or (r and lanes.ready_online(name, origin))):
            return None
        if lanes.online_machine():
            # Nov 6: open the sign-in window on the PC right away (the owner signs in there and sends /homeready).
            try:
                out = self.b.call('home_signin_request', {'site': name})
                return {'site': name, 'sent': out.get('sent', False), 'lane': 'home', 'note': out['note']}
            except ValueError:
                pass
        last = self._sent_at.get('home:' + name, 0)
        if time.time() - last > 90:
            self._sent_at['home:' + name] = time.time()
            message = (name + ' runs on your PC, and it needs you to sign in there. At the PC, send /homesignin ' + name +
                       ': a Chrome window opens on ' + domain + '. Sign in (tick "remember me"), then send /homeready ' + name +
                       ' and tell me to continue. Your password stays on the PC.')
            try:
                send = getattr(self.b, 'send_with_buttons', None)
                rows = [[{'text': '🖥 Copy /homesignin', 'copy_text': {'text': '/homesignin ' + name}},
                         {'text': '✅ Copy /homeready', 'copy_text': {'text': '/homeready ' + name}}]]
                send(message, rows) if send else self.b.provider.telegram(message)
            except Exception:
                pass
        return {'site': name, 'sent': True, 'lane': 'home',
                'note': name + ' runs on their home PC (lane rule for ' + domain + '), so no cloud sign-in link was sent. The owner was asked in Telegram to sign in '
                'at the PC (/homesignin ' + name + ', then /homeready ' + name + '). Wait for them to say the owner is done, then open the site again with '
                'web_browser_open site_login=' + name + ' (it opens on the PC). Do not open a cloud browser for it and do not ask for passwords or codes.'}

    def ensure_context(self, name, start, origin):
        """Saved-login row + its Browserbase context (one isolated cookie jar per site)."""
        r = self.row(name)
        if r and site_domain(r['origin']) != site_domain(origin):
            raise ValueError('That name is already used for ' + r['origin'] + '. Remove it with /sitedisconnect first, or pick another name.')
        if not r:
            with self.b.db() as db:
                db.execute('INSERT INTO site_logins(name,start_url,origin,context_id,status,updated,session_id,sequence) VALUES(?,?,?,NULL,?,?,NULL,0)', (name, start, origin, 'context_creating', time.time()))
            try:
                created = self.b.provider.api('POST', 'contexts', {'name': 'procurator-site-' + name})
                identifier = created.get('id')
                if not isinstance(identifier, str) or not re.fullmatch(r'[a-fA-F0-9-]{20,64}', identifier):
                    raise RuntimeError('Unconfirmed context')
            except Exception:
                self.mark(name, 'context_uncertain')
                raise ValueError('Saved-login storage could not be confirmed. The operator must inspect it; do not create another.') from None
            with self.b.db() as db:
                db.execute('UPDATE site_logins SET context_id=? WHERE name=?', (identifier, name))
        else:
            with self.b.db() as db:
                db.execute('UPDATE site_logins SET start_url=? WHERE name=?', (start, name))
        return self.context(name)

    def free_the_browser(self):
        """Only one cloud browser runs at a time; idle ones (including an unfinished private sign-in) give way."""
        with self.b.db() as db:
            busy = db.execute("SELECT session_id FROM plans WHERE status IN ('executing','uncertain','verification_pending') LIMIT 1").fetchone()
            others = [x['id'] for x in db.execute("SELECT id FROM sessions WHERE status IN ('open','creating')").fetchall()]
        if others and busy:
            raise ValueError('Another browser is in the middle of an action; finish or resolve it first (browser ' + busy['session_id'] + ').')
        for other in others:
            try:
                self.b.call('close', {'id': other})
            except Exception:
                pass

    def signup(self, args):
        """Nov 6 Publix: create a NEW account on a site. Opens the site's sign-up page in a browser the assistant can see,
        so it fills the ordinary fields (name, birth date, address, email, phone) in a form-filling task. Password, security
        questions, codes and the final create button are their, through the private link (web_send_signin_link) for THIS
        browser. Afterwards the login is saved like any other."""
        name = site_name(args.get('site'))
        address = args.get('url') or PRESETS.get(name)
        if not address:
            raise ValueError('Pass the official sign-up page (or the site\'s home page) as url for ' + name + '.')
        start, origin = self.b.url_validator(address)
        if hasattr(self.b, 'site_rule'):
            rule = self.b.site_rule(origin)
            if rule['mode'] in ('never', 'read_only'):
                raise ValueError('No account creation on ' + rule['domain'] + ': ' + rule['reason'])
        r = self.row(name)
        if r and r['status'] == 'owner_connected':
            raise ValueError('There is already a saved ' + name + ' login. Open it with web_browser_open site_login=' + name + ' instead of creating another account.')
        if r and r['status'] == 'signing_up':
            kept = self.open_session(r)
            if kept:
                return {**self.public(r), 'browser': kept['id'], 'reused': True,
                        'note': 'The sign-up browser is still open; continue in it (web_browser_snapshot id ' + kept['id'] + ').'}
        self.ensure_context(name, start, origin)
        self.free_the_browser()
        self.mark(name, 'signing_up')
        sequence = (self.row(name)['sequence'] or 0) + 1
        with self.b.db() as db:
            db.execute('UPDATE site_logins SET sequence=?,signin_hash=NULL,signin_expires=NULL WHERE name=?', (sequence, name))
        session = self.b.call('open', {'url': start, 'session_minutes': 120, 'request_id': f'signup-{name}-{sequence}', 'site_login': name}, privileged=True)
        return {**self.public(self.row(name)), 'browser': session['id'], 'snapshot': session.get('snapshot'),
                'note': ('Sign-up browser ' + session['id'] + ' is open on ' + start + ' and you can see it. 1) Find the site\'s Sign up / Create account / Register page '
                         '(for pharmacies, the pharmacy account sign-up). 2) Fill the ordinary fields with ONE autonomous task (web_task_prepare on this browser, '
                         'mode autonomous, fill_form true, facts from owner_profile: name, birth date, address, email, phone); ask them only for values you do not have. '
                         '3) Never type a password, security answer, code, SSN or card number: when the form reaches those fields (or a CAPTCHA), call '
                         'web_send_signin_link site=' + name + ': the owner finishes them in THIS browser and taps the create button themselves. 4) When the owner taps "I\'m signed in" '
                         'you are woken: continue in the same browser (e.g. add them as a patient). Do not open another browser.')}

    def signin_base(self):
        return os.environ.get('PROCURATOR_PUBLIC_BASE', 'https://assistant.example.com') + '/assistant/signin/'

    def call(self, action, args, privileged):
        if action == 'site_takeover_page':
            if not privileged:
                raise ValueError('Takeover links go only to the owner.')
            return self.takeover_page(args.get('id'))
        if action == 'site_signin_request':
            return self.request_signin(args)
        if action == 'site_signin_events':
            # Finished owner sign-ins (site + browser id only), handed once to the gateway plugin so it can wake the assistant.
            out, self._events = self._events, []
            return {'events': [e for e in out if time.time() - e['at'] < 1800]}
        if action in ('site_signin_page', 'site_signin_keys', 'site_signin_done', 'site_signin_login', 'site_signin_code',
                      'site_signin_shot', 'site_signin_tap', 'site_signin_scroll'):
            if not privileged:
                raise ValueError('Only the owner sign-in page can do this.')
            return self.signin_call(action, args)
        if action == 'site_signup':
            # Not owner-only: the owner asked the assistant to create the account; opening a public sign-up page commits nothing,
            # every field is still gated, and the owner sets the password and taps the create button themselves.
            return self.signup(args)
        if action == 'site_list':
            with self.b.db() as db:
                rows = db.execute('SELECT * FROM site_logins ORDER BY name').fetchall()
            return {'sites': [self.public(dict(r)) for r in rows],
                    'note': 'Signed-in sites open with web_browser_open site_login=NAME. Passwords and codes are only ever entered by the owner.'}
        if not privileged:
            raise ValueError('Only the authenticated owner can connect, confirm or remove a saved site login.')
        name = site_name(args.get('site'))
        if action == 'site_connect':
            address = args.get('url') or PRESETS.get(name)
            if not address:
                raise ValueError('Which website? Send /siteconnect ' + name + ' followed by the address where you normally sign in (for example the one in your benefits email).')
            start, origin = self.b.url_validator(address)
            if hasattr(self.b, 'site_rule'):
                rule = self.b.site_rule(origin)
                if rule['mode'] in ('never', 'read_only'):
                    raise ValueError('No saved login for ' + rule['domain'] + ': ' + rule['reason'])
            self.ensure_context(name, start, origin)
            self.mark(name, 'awaiting_owner_sign_in')
            sequence = (self.row(name)['sequence'] or 0) + 1
            with self.b.db() as db:
                db.execute('UPDATE site_logins SET sequence=? WHERE name=?', (sequence, name))
            # Only one cloud browser runs at a time. An idle earlier browser (e.g. the bot's own
            # signed-out Leaveco attempt) blocked every sign-in on Nov 4; the owner's sign-in wins,
            # unless an action is mid-flight in that browser.
            self.free_the_browser()
            session = self.b.call('open', {'url': start, 'session_minutes': 45, 'request_id': f'site-{name}-{sequence}', 'site_login': name}, privileged=True)
            link = self.b.call('takeover', {'id': session['id']}, privileged=True)
            # Phone sign-in page: the Browserbase live view has no mobile keyboard, so the owner types
            # in our page and the server types it into the field the owner tapped. One link per sign-in,
            # 45 minutes, only while this sign-in is pending; only its hash is stored.
            token = secrets.token_urlsafe(32)
            with self.b.db() as db:
                db.execute('UPDATE site_logins SET signin_hash=?,signin_expires=? WHERE name=?',
                           (hashlib.sha256(token.encode()).hexdigest(), time.time() + 45 * 60, name))
            return {**self.public(self.row(name)), 'session_id': session['id'], 'live_view_url': link['live_view_url'],
                    'signin_url': self.signin_base() + token}
        r = self.context(name)
        if action == 'site_ready':
            if r['status'] == 'owner_connected':
                return self.public(r)
            if r['status'] != 'awaiting_owner_sign_in' or not r['session_id']:
                raise ValueError('There is no sign-in in progress for ' + name + ', so nothing was saved. '
                                 'Ask me for a new sign-in link, or send /siteconnect ' + name + '.')
            # The second /siteready for the same sign-in is trusted (the owner can see the page; we can't).
            checked = getattr(self, '_ready_checked', set()); self._ready_checked = checked
            if r['status'] == 'awaiting_owner_sign_in' and r['session_id'] and (name, r['session_id']) not in checked:
                checked.add((name, r['session_id']))
                # Confirm the private browser really is signed in before saving (only a yes/no is used;
                # no page content leaves this check).
                closed = False
                try:
                    s = self.b.session(r['session_id'])
                    if s.get('status') != 'open':
                        closed, out = True, {'ok': False}
                    else:
                        out = self.b.provider.run(s, 'snapshot')
                except ValueError as e:
                    closed, out = 'ended' in str(e) or 'not open' in str(e), {'ok': False}
                except Exception:
                    out = {'ok': False}
                if closed:
                    # Nov 4: a /siteready after the 45-minute sign-in window had closed was saved as
                    # "ready" without anyone having signed in. Never save an unverified closed window.
                    checked.discard((name, r['session_id']))
                    self.mark(name, 'reauthentication_required')
                    raise ValueError('That private sign-in window has already closed (they last 45 minutes), so nothing was saved. '
                                     'Ask me for a new sign-in link, or send /siteconnect ' + name + '.')
                if not out.get('ok'):
                    return {**self.public(r), 'status': 'still_signed_out',
                            'note': 'I could not check the sign-in browser just now. If you can see your account there, send /siteready ' + name + ' again.'}
                if looks_signed_out(out.get('snapshot') or {}):
                    # (one-time check; the next /siteready saves without re-checking)
                    return {**self.public(r), 'status': 'still_signed_out',
                            'note': 'The private browser still shows a sign-in page. Finish signing in there (including any code), then send /siteready ' + name + ' again.'}
            # The browser the owner just signed in to stays open (until its lifetime ends or the assistant closes it):
            # some portals (Leaveco, Nov 5) do not carry a login into a new cloud browser. Closing it later saves the login.
            self.mark(name, 'owner_connected')
            return self.public(self.row(name))
        if action == 'site_disconnect':
            if r['session_id']:
                try:
                    self.b.call('close', {'id': r['session_id']})
                except Exception:
                    pass
            try:
                self.b.provider.api('DELETE', 'contexts/' + r['context_id'])
            except Exception:
                pass
            with self.b.db() as db:
                db.execute('DELETE FROM site_logins WHERE name=?', (name,))
            return {'site': name, 'status': 'removed', 'note': 'The saved login was deleted. Signing out on the website itself also ends its server session.'}
        raise ValueError('Unknown saved-login operation.')
