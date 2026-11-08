"""Telegram Mini App sign-in (web 0.28.0; plan v2 Phase 5, built early because it is broker-only).

The bot's menu button opens https://<box>/assistant/dashboard/app/ inside Telegram. The page sends Telegram's signed launch data
(initData) to POST /api/v1/auth/telegram. This module checks it the way Telegram's Web Apps documentation describes:

  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  data_check_string = every field except "hash", as key=value, sorted by key, joined with "\\n"
  valid when hex(HMAC_SHA256(key=secret_key, msg=data_check_string)) == hash

and then requires the user id to be the owner's primary Telegram channel and auth_date to be at most 5 minutes old. A session with
method telegram_miniapp lasting 12 hours is issued. Re-check the current Telegram spec when Phase 5 wires the menu button
(setChatMenuButton), and test the cookie on the iOS, Android, macOS and web clients (the web client frames the page).

No approval can be completed through the Mini App: approvals stay Telegram commands.
"""
import hashlib
import hmac
import json
import time
import urllib.parse

MAX_AGE = 300
SESSION_SECONDS = 12 * 3600


class MiniAppError(ValueError):
    pass


def verify(init_data, bot_token, owner_id, now=None, max_age=MAX_AGE):
    """The Telegram user dict when the launch data is genuine, fresh and the owner's; raises MiniAppError otherwise."""
    if not isinstance(init_data, str) or not 10 <= len(init_data) <= 4096:
        raise MiniAppError('No Telegram launch data.')
    if not bot_token:
        raise MiniAppError('The bot token is not configured on the server.')
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields = {}
    for k, v in pairs:
        if k in fields:
            raise MiniAppError('Launch data repeats a field.')
        fields[k] = v
    got = fields.pop('hash', '')
    if not got:
        raise MiniAppError('Launch data is not signed.')
    check = '\n'.join('%s=%s' % (k, fields[k]) for k in sorted(fields))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        raise MiniAppError('Launch data signature does not match.')
    try:
        auth = int(fields.get('auth_date', '0'))
    except ValueError:
        auth = 0
    if abs((now if now is not None else time.time()) - auth) > max_age:
        raise MiniAppError('Launch data is too old; close and reopen the app from the chat.')
    try:
        user = json.loads(fields.get('user') or '{}')
    except ValueError:
        user = {}
    if str(user.get('id')) != str(owner_id):
        raise MiniAppError('This dashboard belongs to another Telegram account.')
    return user


def sign(fields, bot_token):
    """Builds valid launch data (tests and the fixture lane only)."""
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    check = '\n'.join('%s=%s' % (k, fields[k]) for k in sorted(fields))
    return urllib.parse.urlencode({**fields, 'hash': hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def page(nonce, root):
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>PROCURATOR</title><script nonce="%s" src="https://telegram.org/js/telegram-web-app.js"></script>'
            '<style nonce="%s">body{font:16px -apple-system,system-ui,sans-serif;margin:24px;color:var(--tg-theme-text-color,#222);background:var(--tg-theme-bg-color,#fff)}</style></head>'
            '<body><p id="m">Opening your dashboard…</p><script nonce="%s">(async()=>{const m=document.getElementById("m");'
            'try{const tg=window.Telegram&&Telegram.WebApp;if(!tg||!tg.initData){m.textContent="Open this from the PROCURATOR chat in Telegram.";return;}tg.ready();'
            'const r=await fetch("%s/api/v1/auth/telegram",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({init_data:tg.initData})});'
            'const j=await r.json();if(!j.ok){m.textContent=j.error||"Not signed in.";return;}location.replace(j.go);}catch(e){m.textContent="Could not sign in: "+e.message;}})();</script></body></html>'
            ) % (nonce, nonce, nonce, root)
