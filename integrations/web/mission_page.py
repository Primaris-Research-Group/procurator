"""Private upload page for mission task packets (.md / .txt), Nov 5 2026.

Served on 127.0.0.1 only; nginx forwards /assistant/missions/<token>[/save]. The token comes from a
Telegram message the broker sends to the owner (45 minutes, one upload). Files are read in the
browser and posted as text; nothing is executed, rendered as HTML, or logged.
"""
import html
import http.server
import json
import re
import secrets
import socketserver

PATH = re.compile(r'^/assistant/missions/([A-Za-z0-9_-]{40,64})(/save)?$')
import os
ORIGIN = os.environ.get('PROCURATOR_PUBLIC_BASE', 'https://assistant.example.com')
MAX_BODY = 2_600_000

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Upload task packets</title>
<style nonce="{nonce}">
:root{{color-scheme:light dark;--bg:#f6f7f9;--fg:#111;--muted:#5a5f69;--card:#fff;--line:#d5d8de;--accent:#0b63ce;--bad:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121417;--fg:#eee;--muted:#a6abb5;--card:#1d2025;--line:#333842;--accent:#5aa2ff;--bad:#ff8a80}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:640px;margin:0 auto;padding:16px 16px 48px}}h1{{font-size:20px;margin:6px 0 2px}}p.sub{{margin:0 0 12px;color:var(--muted)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-top:12px}}
label{{display:block;font-weight:600;font-size:15px;margin:10px 0 4px}}.hint{{color:var(--muted);font-size:14px;margin:4px 0 0}}
input[type=text]{{width:100%;font-size:17px;padding:11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg)}}
input[type=file]{{width:100%;font-size:16px;padding:10px 0}}
ol{{margin:8px 0 0;padding-left:22px}}li{{margin:2px 0;word-break:break-word}}li small{{color:var(--muted)}}
.check{{display:flex;gap:10px;align-items:flex-start;margin-top:12px}}.check input{{width:22px;height:22px;margin-top:2px}}
button{{width:100%;font-size:17px;padding:13px;border-radius:12px;border:1px solid var(--accent);background:var(--accent);color:#fff;margin-top:14px}}
button:disabled{{opacity:.5}}label.plain{{margin:0;font-weight:400}}#status{{min-height:1.4em;margin-top:12px}}.bad{{color:var(--bad)}}
</style></head><body><main>
<h1>Upload task packets</h1>
<p class="sub">{heading}</p>
<section>
<label for="files">Packet files (.md or .txt, up to 25)</label>
<input id="files" type="file" multiple accept=".md,.txt,text/markdown,text/plain">
<p class="hint">Tip: name files so they sort in reading order (00_START_HERE.md, 01_….md). Put a line like <b>**Revision:** BG-2026-11-05-r1</b> in the first file.</p>
<ol id="list"></ol>
<label for="mission">Mission name</label>
<input id="mission" type="text" autocapitalize="off" autocorrect="off" spellcheck="false" maxlength="24" placeholder="e.g. bg" value="{mission}" {lock}>
<p class="hint">Short and lowercase. Uploading again with the same name stores a new revision; earlier revisions are kept.</p>
<label for="title">Title (optional)</label>
<input id="title" type="text" maxlength="120" placeholder="Taken from the first # heading">
<div class="check"><input id="activate" type="checkbox"><label for="activate" class="plain">Activate now: the assistant starts working on it (read-only review first). Calls, emails and filings still need your approval each time.</label></div>
<button id="save" disabled>Save packet</button>
<div id="status" role="status"></div>
</section>
</main>
<script nonce="{nonce}">
const files=document.getElementById('files'),list=document.getElementById('list'),save=document.getElementById('save'),status=document.getElementById('status');
const mission=document.getElementById('mission'),title=document.getElementById('title'),activate=document.getElementById('activate');
let packet={{}};
function say(text,bad){{status.textContent=text;status.className=bad?'bad':'';}}
files.addEventListener('change',async()=>{{
 packet={{}};list.textContent='';let total=0;
 const picked=[...files.files].sort((a,b)=>a.name.localeCompare(b.name));
 if(picked.length>25){{say('Pick at most 25 files.',true);save.disabled=true;return;}}
 for(const f of picked){{
  if(!/\\.(md|txt)$/i.test(f.name)){{say(f.name+' is not a .md or .txt file.',true);save.disabled=true;return;}}
  const text=await f.text();total+=text.length;packet[f.name]=text;
  const li=document.createElement('li');li.textContent=f.name+' ';const s=document.createElement('small');s.textContent=Math.round(text.length/100)/10+'k characters';li.appendChild(s);list.appendChild(li);
 }}
 if(total>2000000){{say('The packet is larger than 2 MB of text.',true);save.disabled=true;return;}}
 const first=packet[picked.map(f=>f.name).find(n=>/start|readme|^00/i.test(n))||picked[0].name]||'';
 const rev=(first.match(/\\*\\*Revision:\\*\\*\\s*([A-Za-z0-9][A-Za-z0-9._-]{{2,60}})/)||[])[1];
 const head=(first.match(/^#\\s+(.+)$/m)||[])[1];
 if(head&&!title.value)title.placeholder=head.slice(0,120);
 if(rev&&!mission.value)mission.value=rev.split('-')[0].toLowerCase().replace(/[^a-z0-9-]/g,'').slice(0,24);
 say(picked.length+' file(s) ready'+(rev?' · revision '+rev:' · no **Revision:** line found (date will be used)'));
 save.disabled=!picked.length;
}});
save.addEventListener('click',async()=>{{
 save.disabled=true;say('Saving…');
 try{{
  const r=await fetch(location.pathname+'/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},
   body:JSON.stringify({{files:packet,mission:mission.value.trim().toLowerCase()||null,title:title.value.trim()||null,activate:activate.checked}})}});
  const j=await r.json();
  if(!j.ok){{say(j.error||'Not saved.',true);save.disabled=false;return;}}
  say('Saved mission '+j.mission+' · '+j.title+' · revision '+j.revision+' · '+(j.status==='active'?'ACTIVE':j.status)+'. You can close this page; a confirmation is in Telegram.');
 }}catch(e){{say('Not saved. Check your connection and try again.',true);save.disabled=false;}}
}});
</script></body></html>'''


def message_page(text):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Upload task packets</title></head><body style="font:17px/1.5 -apple-system,system-ui,sans-serif;padding:24px">'
            + html.escape(text) + '</body></html>')


def handler_for(broker):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'assistant'
        sys_version = ''

        def log_message(self, *args):  # never log paths (they carry the token)
            pass

        def send(self, code, body, ctype, nonce=None):
            data = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            csp = "default-src 'none'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            if nonce:
                csp += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
            self.send_header('Content-Security-Policy', csp)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            m = PATH.match(self.path.split('?')[0])
            if not m or m.group(2):
                return self.send(404, message_page('Not found.'), 'text/html; charset=utf-8')
            try:
                info = broker.call('mission_upload_page', {'token': m.group(1)}, True)
            except ValueError as e:
                return self.send(410, message_page(str(e)), 'text/html; charset=utf-8')
            except Exception:
                return self.send(503, message_page('Uploads are not available right now. Try again in a minute.'), 'text/html; charset=utf-8')
            ex = info.get('existing')
            heading = ('New revision for mission %s: %s (now %s, %s).' % (ex['slug'], ex['title'], ex['revision'], ex['status'])) if ex else \
                'Private page for your assistant. The files become instructions for one mission; they cannot change approvals.'
            nonce = secrets.token_urlsafe(16)
            page = PAGE.format(nonce=nonce, heading=html.escape(heading), mission=html.escape(info.get('mission') or ''),
                               lock='readonly' if info.get('mission') else '')
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
            if not 0 < length <= MAX_BODY:
                return self.send(413, '{"ok":false,"error":"The packet is too large (2 MB of text at most)."}', 'application/json')
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(body, dict):
                    raise ValueError('Bad request.')
                out = broker.call('mission_upload', {'token': m.group(1), 'files': body.get('files'), 'mission': body.get('mission'),
                                                     'title': body.get('title'), 'activate': body.get('activate') is True}, True)
                out = {'ok': True, **{k: out[k] for k in ('mission', 'title', 'revision', 'status', 'reading_order')}}
            except ValueError as e:
                out = {'ok': False, 'error': str(e)}
            except Exception:
                out = {'ok': False, 'error': 'Not saved. Try again.'}
            self.send(200, json.dumps(out), 'application/json')
    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start(broker, port=3341):
    import threading
    server = Server(('127.0.0.1', port), handler_for(broker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
