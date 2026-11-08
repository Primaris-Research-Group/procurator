"""Phone sign-in page for saved site logins.

Browserbase's live view has no mobile keyboard, so on a phone the owner could tap a login
field but not type into it (Nov 4, Leaveco). Worse, while that live view is embedded, iPhone shows no
keyboard for this page's own boxes either (Nov 5). So phones get a picture of the cloud browser that
refreshes every few seconds; a tap on the picture is a click there, and the boxes below type into it
(or their password manager fills them). Computers still get the live view. Text is held only in memory for that one
keyboard call: never stored, logged, shown to the bot, or sent to Telegram.

Served on 127.0.0.1 only; nginx forwards /assistant/signin/<token>[/keys|/done].
"""
import base64
import html
import http.server
import json
import re
import secrets
import socketserver

PATH = re.compile(r'^/assistant/signin/([A-Za-z0-9_-]{40,64})(/keys|/done|/login|/code|/shot|/tap|/scroll)?$')
import os
ORIGIN = os.environ.get('PROCURATOR_PUBLIC_BASE', 'https://assistant.example.com')

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Private sign-in</title>
<style nonce="{nonce}">
:root{{color-scheme:light dark;--bg:#f6f7f9;--fg:#111;--muted:#5a5f69;--card:#fff;--line:#d5d8de;--accent:#0b63ce}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121417;--fg:#eee;--muted:#a6abb5;--card:#1d2025;--line:#333842;--accent:#5aa2ff}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.4 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
header{{padding:10px 16px;font-weight:600}}header small{{display:block;font-weight:400;color:var(--muted)}}
#view{{border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--card)}}
#view iframe{{display:block;width:100%;height:60vh;border:0;background:#fff}}
#screen{{display:block;width:100%;height:auto;min-height:120px;background:#fff;touch-action:manipulation;cursor:pointer}}
.bar{{display:flex;gap:8px;padding:8px 16px;align-items:center}}.bar button{{padding:8px 12px;font-size:15px}}.bar span{{flex:1;color:var(--muted);font-size:13px;text-align:right}}
main{{padding:4px 16px 40px}}section{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-top:12px}}
h2{{font-size:15px;margin:0 0 6px}}p.hint{{margin:6px 0 0;color:var(--muted);font-size:14px}}
input{{width:100%;font-size:17px;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg);margin-top:8px}}
button{{font-size:16px;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg)}}
button.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}.wide{{width:100%;margin-top:10px}}
.row{{display:flex;gap:8px;align-items:flex-end}}.row input{{flex:1}}
.keys{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}}.keys button{{flex:1 1 22%}}
#status{{min-height:1.4em;margin-top:12px;color:var(--muted)}}[hidden]{{display:none!important}}
</style></head><body>
<header>{heading}: {site}<small>A cloud browser only you control. The assistant never sees what you enter here.</small></header>
<div id="view" data-live="{live}">
<img id="screen" alt="The private browser (tap to press)">
<div class="bar"><button type="button" data-scroll="up" aria-label="Scroll up">▲</button><button type="button" data-scroll="down" aria-label="Scroll down">▼</button><button type="button" id="refresh" aria-label="Refresh">↻</button><button type="button" id="mode">Live view</button><span id="stamp"></span></div>
</div>
<main>
<p class="hint" id="viewhint">This is a picture of the website, updated every few seconds. Tap it to press a button or link, or to choose a box before typing in step 3.</p>
<section><h2>1. Sign in</h2>
<form id="login" autocomplete="on">
<input id="username" name="username" autocomplete="username" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="User ID or email">
<input id="password" name="password" type="password" autocomplete="current-password" placeholder="Password">
<button class="primary wide" type="submit">Sign in</button></form>
<p class="hint">Tap a box: your iPhone offers saved passwords (Face ID fills both lines). If {site} isn’t suggested, tap Passwords / the key icon and search “{site}”. I fill the site’s sign-in boxes and press Log In for you.</p></section>
<section><h2>2. Verification code (if asked)</h2>
<form id="code" class="row" autocomplete="on"><input id="otp" name="otp" autocomplete="one-time-code" inputmode="numeric" placeholder="Code"><button class="primary" type="submit">Send</button></form>
<p class="hint">A texted code appears above the keyboard; tap it.</p></section>
<section><h2>3. Type into any other box</h2>
<p class="hint">Tap the box in the picture above, then type here and press Send.</p>
<form id="free" class="row"><input id="text" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Text"><button type="submit">Send</button></form>
<div class="keys"><button type="button" data-key="Tab">Next field</button><button type="button" data-key="Enter">Enter</button><button type="button" data-key="Backspace">⌫</button><button type="button" data-clear="1">Clear field</button></div>
</section>
<button id="done" class="primary wide">{done}</button>
<div id="status" role="status"></div>
<p class="hint">One-tap tip: in the Passwords app, open your {site} password → Edit → Add Website → assistant.example.com. It is then suggested here first.</p>
</main>
<script nonce="{nonce}">
const base=location.pathname,status=document.getElementById('status'),$=id=>document.getElementById(id);
const view=$('view'),img=$('screen'),stamp=$('stamp'),liveSrc=view.dataset.live;
async function post(path,body,quiet){{if(!quiet)status.textContent='Working…';
 try{{const r=await fetch(base+path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body),cache:'no-store',credentials:'omit'}});
  const j=await r.json();if(!quiet||!j.ok)status.textContent=j.ok?(j.message||'Sent.'):(j.error||'Not sent.');soon();return j;}}
 catch(e){{status.textContent='Connection problem. Try again.';return {{ok:false}};}}}}
// Picture view by default on phones: an embedded live view takes the keyboard focus away on iPhone (Nov 5).
let live=false,frame=null,loading=false,ended=false,fails=0;
function refresh(){{if(live||loading||ended||document.hidden)return;loading=true;img.src=base+'/shot?t='+Date.now();}}
function soon(){{setTimeout(refresh,900);}}
img.addEventListener('load',()=>{{loading=false;fails=0;stamp.textContent='Updated '+new Date().toLocaleTimeString([],{{hour:'numeric',minute:'2-digit',second:'2-digit'}});}});
img.addEventListener('error',()=>{{loading=false;if(++fails>=4){{ended=true;stamp.textContent='Picture stopped. Reload this page or ask the assistant for a new link.';}}}});
img.addEventListener('click',e=>{{const r=img.getBoundingClientRect();if(!r.width||!r.height)return;
 post('/tap',{{x:Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),y:Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))}},true);status.textContent='Tapped.';}});
function setLive(on){{live=on;
 if(on){{frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-same-origin allow-scripts');frame.setAttribute('title','Private browser');frame.src=liveSrc;view.prepend(frame);img.hidden=true;}}
 else{{if(frame){{frame.remove();frame=null;}}img.hidden=false;refresh();}}
 $('mode').textContent=on?'Picture view':'Live view';$('viewhint').hidden=on;}}
$('mode').addEventListener('click',()=>setLive(!live));
$('refresh').addEventListener('click',()=>{{ended=false;fails=0;loading=false;refresh();}});
document.querySelectorAll('[data-scroll]').forEach(b=>b.addEventListener('click',()=>post('/scroll',{{direction:b.dataset.scroll}},true)));
setInterval(refresh,3000);document.addEventListener('visibilitychange',refresh);
setLive(matchMedia('(hover: hover) and (pointer: fine)').matches);
$('login').addEventListener('submit',async e=>{{e.preventDefault();const username=$('username').value,password=$('password').value;
 if(!password){{status.textContent='Fill the password line first.';return;}}$('password').value='';await post('/login',{{username,password}});}});
$('code').addEventListener('submit',async e=>{{e.preventDefault();const code=$('otp').value.trim();if(!code)return;$('otp').value='';await post('/code',{{code}});}});
$('free').addEventListener('submit',async e=>{{e.preventDefault();const text=$('text').value;if(!text)return;$('text').value='';await post('/keys',{{text}});}});
document.querySelectorAll('[data-key]').forEach(b=>b.addEventListener('click',()=>post('/keys',{{key:b.dataset.key}})));
document.querySelector('[data-clear]').addEventListener('click',()=>post('/keys',{{clear:true}}));
$('done').addEventListener('click',async()=>{{const j=await post('/done',{{}});if(j.ok&&j.saved){{$('done').disabled=true;ended=true;}}}});
</script></body></html>'''


def message_page(text):
    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Private sign-in</title><p style="font:17px -apple-system,system-ui,sans-serif;padding:20px">' + html.escape(text) + '</p>')


def handler_for(broker):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'owner'
        sys_version = ''

        def log_message(self, *args):  # never log paths (they carry the sign-in token)
            pass

        def send(self, status, body, ctype, nonce=None):
            data = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            csp = ("default-src 'none'; frame-src https://www.browserbase.com; connect-src 'self'; img-src 'self'; base-uri 'none'; "
                   "form-action 'none'; frame-ancestors 'none'")
            if nonce:
                csp += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
            self.send_header('Content-Security-Policy', csp)
            self.end_headers()
            self.wfile.write(data)

        def send_image(self, data):
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            m = PATH.match(self.path.split('?')[0])
            if m and m.group(2) == '/shot':
                try:
                    out = broker.call('site_signin_shot', {'token': m.group(1)}, True)
                    data = base64.b64decode(out['image_b64'], validate=True)
                    if not data.startswith(b'\xff\xd8') or len(data) > 4_000_000:
                        raise RuntimeError('not a picture')
                    return self.send_image(data)
                except ValueError as e:
                    return self.send(429 if str(e) == 'Too fast.' else 410, json.dumps({'ok': False, 'error': str(e)}), 'application/json')
                except Exception:
                    return self.send(503, '{"ok":false}', 'application/json')
            if not m or m.group(2):
                return self.send(404, message_page('Not found.'), 'text/html; charset=utf-8')
            try:
                info = broker.call('site_signin_page', {'token': m.group(1)}, True)
            except ValueError as e:
                return self.send(410, message_page(str(e)), 'text/html; charset=utf-8')
            except Exception:
                return self.send(503, message_page('The private browser is not available right now. Try again in a minute.'), 'text/html; charset=utf-8')
            live = info['live_view_url'] + ('&' if '?' in info['live_view_url'] else '?') + 'navbar=false'
            nonce = secrets.token_urlsafe(16)
            takeover = info.get('kind') == 'takeover'
            page = PAGE.format(nonce=nonce, site=html.escape(info['site']), live=html.escape(live, quote=True),
                               heading='Private browser' if takeover else 'Private sign-in', done='I’m done' if takeover else 'I’m signed in')
            self.send(200, page, 'text/html; charset=utf-8', nonce)

        def do_POST(self):
            m = PATH.match(self.path.split('?')[0])
            if not m or not m.group(2):
                return self.send(404, '{"ok":false}', 'application/json')
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self.send(403, '{"ok":false,"error":"Wrong site."}', 'application/json')
            if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
                return self.send(415, '{"ok":false}', 'application/json')
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= 2048:
                return self.send(413, '{"ok":false,"error":"Too long."}', 'application/json')
            body = None
            try:
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('Bad request.')
                token = m.group(1)
                if m.group(2) == '/shot':
                    raise ValueError('Not found.')
                if m.group(2) == '/tap':
                    out = broker.call('site_signin_tap', {'token': token, 'x': body.get('x'), 'y': body.get('y')}, True)
                elif m.group(2) == '/scroll':
                    out = broker.call('site_signin_scroll', {'token': token, 'direction': body.get('direction')}, True)
                elif m.group(2) == '/keys':
                    broker.call('site_signin_keys', {'token': token, 'text': body.get('text'), 'key': body.get('key'), 'clear': body.get('clear')}, True)
                    out = {'ok': True, 'message': 'Sent. Check the page above.'}
                elif m.group(2) == '/login':
                    out = broker.call('site_signin_login', {'token': token, 'username': body.get('username'), 'password': body.get('password')}, True)
                elif m.group(2) == '/code':
                    out = broker.call('site_signin_code', {'token': token, 'code': body.get('code')}, True)
                else:
                    r = broker.call('site_signin_done', {'token': token}, True)
                    saved = r.get('status') in ('owner_connected', 'done')
                    out = {'ok': True, 'saved': saved,
                           'message': ('Done. The assistant continues by itself; you can close this page.' if saved else (r.get('note') or 'Not saved yet.'))}
            except ValueError as e:
                out = {'ok': False, 'error': str(e)}
            except Exception:
                out = {'ok': False, 'error': 'Not sent. Try again.'}
            body = None  # the typed text is not kept
            self.send(200, json.dumps(out), 'application/json')
    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start(broker, port=3340):
    import threading
    server = Server(('127.0.0.1', port), handler_for(broker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
