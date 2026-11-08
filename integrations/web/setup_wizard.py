"""Browser setup wizard (0.30.0, Tier 1 of the lay-person install plan).

The installer mints a one-time link and prints it:  https://<domain>/assistant/dashboard/setup/<token>
Opening it walks the owner through three steps on one page, with nothing typed into a terminal:

  1. Model key      — Anthropic key, checked live, saved to .env
  2. Telegram bot   — BotFather token, checked with getMe (shows the bot's @name)
  3. Your phone     — one button opens the bot with a one-time start code; pressing Start makes the presser the owner
                      (setup_core.wait_for_start). Then owner.json, PROCURATOR_OWNER_ID, the Telegram allow-list and a gateway
                      restart are applied, the bot says hello, and the page hands over to the dashboard (Accounts tab) with a
                      single-use sign-in link.

Security:
  * the link is 32 random bytes, stored only as a SHA-256 hash, valid 24 hours, and dead once setup completes;
  * a box that already has an owner refuses the wizard unless the link was minted with --reconfigure;
  * POSTs must be JSON and carry the X-Procurator-Setup header (a cross-site form cannot), and the Origin must match;
  * keys and tokens are never echoed, rendered, logged or returned; status shows only "saved" and the bot's @username.

CLI (the installer runs this as root):
  python3 setup_wizard.py mint --base https://box.example.com [--hours 24] [--reconfigure]   -> prints the URL
"""
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import setup_core as core

ROOT = '/assistant/dashboard'
LINK_FILE = 'setup-link.json'
TOKEN = re.compile(r'[A-Za-z0-9_-]{40,64}')


def _h(v):
    return hashlib.sha256(v.encode()).hexdigest()


class SetupLinks:
    """The one live setup link, stored as a hash in the web broker's state directory."""

    def __init__(self, state_dir, now=time.time):
        self.path = Path(state_dir) / LINK_FILE
        self.now = now

    def mint(self, base, hours=24, reconfigure=False):
        token = secrets.token_urlsafe(32)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write({'hash': _h(token), 'created': self.now(), 'expires': self.now() + hours * 3600,
                     'reconfigure': bool(reconfigure), 'used': None, 'done': None})
        return base.rstrip('/') + ROOT + '/setup/' + token

    def _write(self, rec):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(rec))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def check(self, token, grace_s=7200):
        """The record when the token is the live one. A completed link keeps answering (status only) for `grace_s` so the page
        can show "done" across the broker restart that completion causes."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        rec = self._read()
        if not rec or not secrets.compare_digest(rec.get('hash', ''), _h(token)):
            return None
        if rec.get('used'):
            return rec if (rec.get('done') and rec['used'] + grace_s > self.now()) else None
        return rec if rec.get('expires', 0) >= self.now() else None

    def complete(self, owner_name, bot):
        rec = self._read() or {}
        rec['used'] = self.now()
        rec['done'] = {'owner': str(owner_name or '')[:40], 'bot': str(bot or '')[:60]}
        self._write(rec)


class Wizard:
    """One setup in progress. The verified bot token is held in memory only until the applier writes it to .env."""

    def __init__(self, links, box, apply=None, opener=None, login_link=None, timezone=''):
        self.links, self.box = links, box
        self.apply = apply or core.FileApplier(links.path.parent)
        self.opener = opener or core._urlopen
        self.login_link = login_link            # callable(next_path) -> single-use dashboard sign-in URL
        self.timezone = timezone
        self.lock = threading.RLock()
        self.next_link = ''
        self.reset()

    def reset(self):
        self.bot = None
        self.code = None
        self.state = 'idle'        # idle -> waiting -> finishing -> done | error
        self.error = ''
        self.owner = None
        self.thread = None

    # -- guards
    def allowed(self, token):
        rec = self.links.check(token)
        if not rec:
            return None, ('This setup link has expired or was already used. On the server, run:  sudo bash /opt/procurator/bin/install.sh --setup-link')
        if (not rec.get('done') and not rec.get('reconfigure') and self.state not in ('finishing', 'error')
                and core.owner_configured(self.box.owner_file)):
            return None, 'This assistant already has an owner. Send /panel to your bot for the dashboard.'
        return rec, ''

    # -- steps
    def status(self, rec=None):
        env = core.read_env(self.box.env)
        rec = rec or {}
        with self.lock:
            if rec.get('done'):
                if not self.next_link and self.login_link:
                    try:
                        self.next_link = self.login_link('/accounts')
                    except Exception:
                        self.next_link = ''
                return {'model': True, 'bot': rec['done'].get('bot', ''), 'state': 'done', 'error': '', 'owner': rec['done'].get('owner', ''),
                        'link': '', 'next': self.next_link}
            return {'model': bool(core.ANTHROPIC_KEY.fullmatch(env.get('ANTHROPIC_API_KEY', ''))),
                    'bot': ('@' + self.bot['username']) if self.bot else '',
                    'state': self.state, 'error': self.error,
                    'owner': (self.owner or {}).get('first_name', ''),
                    'link': core.pair_link(self.bot['username'], self.code) if (self.bot and self.code and self.state == 'waiting') else '',
                    'next': ''}

    def save_model(self, key):
        key = core.verify_anthropic(key, opener=self.opener)      # friendly errors here; the applier checks again as root
        self.apply('save_model', {'key': key}, 60)
        return {'model': True}

    def save_bot(self, token):
        bot = core.verify_bot(token, opener=self.opener)
        with self.lock:
            if self.state in ('waiting', 'finishing'):
                raise core.SetupError('Pairing is already in progress. Finish it on your phone, or reload this page to start over.')
            self.bot, self.code, self.state, self.error = bot, None, 'idle', ''
        return {'bot': '@' + bot['username']}

    def start_pairing(self):
        with self.lock:
            if not self.bot:
                raise core.SetupError('Add your bot first (step 2).')
            if not core.ANTHROPIC_KEY.fullmatch(core.read_env(self.box.env).get('ANTHROPIC_API_KEY', '')):
                raise core.SetupError('Add your model key first (step 1).')
            if self.state in ('waiting', 'finishing'):
                return {'link': core.pair_link(self.bot['username'], self.code)}
            self.code, self.state, self.error = core.new_code(), 'waiting', ''
            self.thread = threading.Thread(target=self._pair, daemon=True)
            self.thread.start()
            return {'link': core.pair_link(self.bot['username'], self.code)}

    def _pair(self):
        bot, code = self.bot, self.code
        try:
            try:
                owner = core.wait_for_start(bot['token'], code, opener=self.opener)
            except core.GatewayBusy:
                self.apply('stop_gateway', {}, 120)       # an earlier install is reading the bot; stop it and listen ourselves
                owner = core.wait_for_start(bot['token'], code, opener=self.opener)
            with self.lock:
                self.owner, self.state = owner, 'finishing'
            # The applier writes .env/owner.json, sets the allow-list, restarts the gateway, says hello, marks this link done and
            # then restarts the brokers (this process included); the page picks up "done" from the link record afterwards.
            self.apply('finish', {'bot_token': bot['token'], 'owner': owner, 'timezone': self.timezone}, 300)
            with self.lock:
                self.state = 'done'
        except core.SetupError as e:
            with self.lock:
                self.state, self.error = 'error', str(e)
        except Exception:
            with self.lock:
                self.state, self.error = 'error', 'Setup stopped unexpectedly. Reload the page and try step 3 again.'

    # -- HTTP glue (called from dashboard.py)
    def handle(self, method, token, sub, body, origin_ok):
        rec, why = self.allowed(token)
        if not rec:
            return 410, {'ok': False, 'error': why}
        if method == 'GET':
            if sub == 'status':
                return 200, {'ok': True, **self.status(rec)}
            return 404, {'ok': False, 'error': 'Not found.'}
        if not origin_ok:
            return 403, {'ok': False, 'error': 'Open the setup link directly and try again.'}
        if rec.get('done'):
            return 409, {'ok': False, 'error': 'Setup is already complete.'}
        try:
            if sub == 'model':
                return 200, {'ok': True, **self.save_model(body.get('key'))}
            if sub == 'bot':
                return 200, {'ok': True, **self.save_bot(body.get('token'))}
            if sub == 'pair':
                return 200, {'ok': True, **self.start_pairing()}
            if sub == 'restart':
                with self.lock:
                    if self.state == 'error':
                        self.state, self.error = 'idle', ''
                return 200, {'ok': True}
        except core.SetupError as e:
            return 400, {'ok': False, 'error': str(e)}
        return 404, {'ok': False, 'error': 'Not found.'}


def run_applier(state_dir, box, opener=None):
    """bin/setup-apply.py: apply the pending wizard request (as root), write the result, then restart the brokers if asked."""
    d = Path(state_dir) / 'setup'
    req_path = d / 'request.json'
    try:
        req = json.loads(req_path.read_text())
    except (OSError, ValueError):
        return None
    finally:
        try:
            req_path.unlink()                        # the request may hold a key: it never outlives this read
        except OSError:
            pass
    rid = str(req.get('id', ''))[:32]
    try:
        out = core.apply_request(req, box, opener=opener or core._urlopen)
        if req.get('action') == 'finish':
            SetupLinks(state_dir).complete(out.get('owner_first') or out.get('owner_name'), out.get('bot'))
    except core.SetupError as e:
        out = {'ok': False, 'error': str(e)}
    except Exception:
        out = {'ok': False, 'error': 'That step failed on the server. See: journalctl -u procurator-setup -n 30'}
    restart = out.pop('restart', False)
    tmp = d / '.result.tmp'
    tmp.write_text(json.dumps({'id': rid, **out}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, d / 'result.json')
    if restart:
        time.sleep(2)                                # let the waiting wizard read the result before its broker restarts
        try:
            box.restart_brokers()
        except core.SetupError:
            pass
    return out


def esc(s):
    return html.escape(str(s), quote=True)


def page(nonce, token, status):
    boot = json.dumps(status).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return PAGE.replace('__NONCE__', nonce).replace('__BASE__', esc(ROOT + '/setup/' + token)).replace('__STATUS__', boot)


def blocked_page(message):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex"><title>PROCURATOR setup</title></head><body style="font:17px/1.5 -apple-system,system-ui,sans-serif;'
            'padding:28px;max-width:560px;margin:0 auto;color:#1C2260"><h1 style="font-size:22px">PROCURATOR setup</h1><p>%s</p></body></html>') % esc(message)


PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Set up PROCURATOR</title>
<style nonce="__NONCE__">
:root{--ink:#1C2260;--ink2:#3B46B5;--sf:#FFB224;--sf2:#FFF1D1;--mint:#36D6B0;--mint2:#DDF8F0;--coral:#FF5A5F;--paper:#F5F6FB;--line:#DDE1F0;--muted:#5A6185}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{background:var(--ink);color:#fff;padding:22px 20px}header .w{max-width:680px;margin:0 auto}header b{font-size:19px;letter-spacing:.02em}header span{color:var(--sf);font-size:14px;margin-left:8px}
main{max-width:680px;margin:0 auto;padding:22px 16px 60px}
h1{font-size:28px;line-height:1.15;margin:6px 0 6px}.lede{color:var(--muted);margin:0 0 22px}
.step{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;margin:0 0 16px}
.step h2{font-size:19px;margin:0 0 6px;display:flex;gap:10px;align-items:center}
.n{width:30px;height:30px;border-radius:50%;background:var(--ink);color:var(--sf);display:inline-grid;place-items:center;font-size:15px;font-weight:800;flex:0 0 auto}
.done .n{background:var(--mint);color:var(--ink)}.done .n::after{content:"✓"}.done .n span{display:none}
.locked{opacity:.55}.done label,.done input,.done button{display:none}.done ol{display:none}
ol{margin:8px 0 12px;padding-left:22px;color:var(--muted)}ol li{margin:4px 0}
label{display:block;font-weight:600;font-size:15px;margin:10px 0 6px}
input{width:100%;font:16px ui-monospace,SFMono-Regular,Menlo,monospace;padding:12px;border:1px solid var(--line);border-radius:10px;background:#FBFCFF}
button,.btn{display:inline-block;margin-top:12px;font:600 16px -apple-system,system-ui,sans-serif;background:var(--sf);color:var(--ink);border:0;border-radius:999px;padding:12px 22px;cursor:pointer;text-decoration:none}
button[disabled]{opacity:.5;cursor:wait}.btn.big{font-size:18px;padding:16px 26px}
.ok{color:#0E7F62;font-weight:600}.bad{color:#B5282C;font-weight:600}.sub{color:var(--muted);font-size:14px}
.msg{margin-top:10px;min-height:1px}
.wait{display:flex;gap:10px;align-items:center;margin-top:14px;color:var(--muted)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mint);animation:p 1.2s infinite}@keyframes p{50%{opacity:.25}}
a{color:var(--ink2)}.final{background:var(--ink);color:#fff;border-color:var(--ink)}.final h2{color:#fff}.final .sub{color:#C9CEF5}
</style></head><body>
<header><div class="w"><b>PROCURATOR</b><span>setup</span></div></header>
<main>
<h1>Three steps and you're done.</h1>
<p class="lede">About ten minutes. Keep your phone nearby — the last step happens in Telegram. Nothing you paste here is shown again or sent anywhere except to check it.</p>

<section class="step" id="s1"><h2><span class="n"><span>1</span></span>Your AI model key</h2>
<ol><li>Open <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com › API keys</a> (make an account and add a payment method if it asks).</li>
<li>Press <b>Create key</b>, name it "PROCURATOR", and copy it. It starts with <code>sk-ant-</code>.</li></ol>
<label for="key">Anthropic API key</label><input id="key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-ant-…">
<button id="b1">Check and save</button><div class="msg" id="m1"></div></section>

<section class="step locked" id="s2"><h2><span class="n"><span>2</span></span>Your Telegram bot</h2>
<ol><li>On your phone, install <b>Telegram</b> if you don't have it, then open <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>.</li>
<li>Send <code>/newbot</code>. Give it any name (e.g. "My Assistant"), then a username that ends in <b>bot</b> (e.g. <code>alex_assistant_bot</code>).</li>
<li>BotFather replies with a token like <code>123456789:AA…</code>. Copy it (tap and hold), and paste it here. Using a computer? Telegram Desktop or web.telegram.org works too.</li></ol>
<label for="tok">Bot token</label><input id="tok" type="password" autocomplete="off" spellcheck="false" placeholder="123456789:AA…">
<button id="b2">Check bot</button><div class="msg" id="m2"></div></section>

<section class="step locked" id="s3"><h2><span class="n"><span>3</span></span>Connect your phone</h2>
<p class="sub">Tap the button <b>on your phone</b>. Telegram opens your bot — press <b>Start</b>. Whoever presses Start with this link becomes the only person the assistant listens to.</p>
<button id="b3">Get my link</button>
<div id="pairbox" hidden><a class="btn big" id="plink" href="#" target="_blank" rel="noopener">Open my bot in Telegram</a>
<p class="sub">Doing this on a computer? Tap the button in Telegram Desktop, or open this same setup page on your phone. The link: <span id="ptext"></span></p>
<div class="wait" id="pwait"><span class="dot"></span><span id="pstate">Waiting for you to press Start…</span></div></div>
<div class="msg" id="m3"></div></section>

<section class="step final" id="s4" hidden><h2><span class="n"><span>✓</span></span>You're set up</h2>
<p class="sub" id="donetext">Your assistant just messaged you on Telegram.</p>
<p class="sub">Say hi to it there: it asks a few quick questions — what to call you, he/she/they, and what you want help with.</p>
<a class="btn big" id="dash" href="#">Next: connect your Apple and Google accounts</a>
<p class="sub">That opens your dashboard. Later, send <code>/panel</code> to your bot whenever you want it again.</p></section>
</main>
<script type="application/json" id="boot">__STATUS__</script>
<script nonce="__NONCE__">
(function(){
var BASE="__BASE__", st=JSON.parse(document.getElementById('boot').textContent);
function $(i){return document.getElementById(i)}
function msg(id,t,ok){var m=$(id);m.textContent=t||'';m.className='msg '+(ok?'ok':'bad')}
function post(sub,body,btn,mid){btn.disabled=true;msg(mid,'Checking…',true);
 return fetch(BASE+'/'+sub,{method:'POST',headers:{'Content-Type':'application/json','X-Procurator-Setup':'1'},body:JSON.stringify(body||{}),credentials:'same-origin'})
 .then(function(r){return r.json()}).then(function(j){btn.disabled=false;if(!j.ok){msg(mid,j.error||'That did not work.',false);throw 0}return j})
 .catch(function(e){btn.disabled=false;if(e!==0)msg(mid,'The server did not answer. Check your connection and try again.',false);throw e})}
function paint(s){
 if(s.model){$('s1').classList.add('done');$('s2').classList.remove('locked');msg('m1','Saved and working.',true)}
 if(s.bot){$('s2').classList.add('done');$('s3').classList.remove('locked');msg('m2','Found your bot: '+s.bot,true)}
 if(s.link){$('pairbox').hidden=false;$('plink').href=s.link;$('ptext').textContent=s.link;$('b3').hidden=true}
 if(s.state==='finishing'){$('pstate').textContent='Paired'+(s.owner?' with '+s.owner:'')+'. Finishing — restarting your assistant (about a minute)…'}
 if(s.state==='error'){msg('m3',s.error,false);$('b3').hidden=false;$('b3').textContent='Try again';$('pairbox').hidden=true}
 if(s.state==='done'){$('s3').classList.add('done');$('pairbox').hidden=true;$('s4').hidden=false;
  if(s.next){$('dash').href=s.next}else{$('dash').hidden=true;$('donetext').textContent='Your assistant just messaged you on Telegram. Send /panel there for your dashboard.'}
  if(s.owner)$('donetext').textContent='Welcome, '+s.owner+'. Your assistant just messaged you on Telegram.'}
}
var timer=null;function poll(){fetch(BASE+'/status',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(j){if(j.ok){paint(j);if(j.state==='waiting'||j.state==='finishing'){timer=setTimeout(poll,2000)}}else{msg('m3',j.error,false)}}).catch(function(){timer=setTimeout(poll,4000)})}
$('b1').onclick=function(){post('model',{key:$('key').value.trim()},$('b1'),'m1').then(function(){$('key').value='';paint({model:true})}).catch(function(){})};
$('b2').onclick=function(){post('bot',{token:$('tok').value.trim()},$('b2'),'m2').then(function(j){$('tok').value='';paint({model:true,bot:j.bot})}).catch(function(){})};
$('b3').onclick=function(){post('restart',{},$('b3'),'m3').then(function(){return post('pair',{},$('b3'),'m3')}).then(function(){msg('m3','',true);clearTimeout(timer);poll()}).catch(function(){})};
paint(st);if(st.state==='waiting'||st.state==='finishing')poll();
})();
</script>
</body></html>'''


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='PROCURATOR setup link')
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('mint', help='mint a one-time setup link and print it')
    m.add_argument('--base', required=True, help='https://your.domain')
    m.add_argument('--hours', type=int, default=24)
    m.add_argument('--reconfigure', action='store_true', help='allow the wizard even though an owner is already set')
    m.add_argument('--state-dir', default=os.environ.get('PROCURATOR_WEB_STATE', '/var/lib/procurator-web'))
    ap_apply = sub.add_parser('apply', help='apply the pending wizard request (root; run by the setup service)')
    ap_apply.add_argument('--state-dir', default=os.environ.get('PROCURATOR_WEB_STATE', '/var/lib/procurator-web'))
    a = ap.parse_args(argv)
    if a.cmd == 'apply':
        out = run_applier(a.state_dir, core.Box())
        print('setup: %s' % ('nothing to do' if out is None else ('ok' if out.get('ok') else 'failed: ' + out.get('error', ''))))
        return 0
    if not re.fullmatch(r'https://[a-z0-9.-]+(:\d+)?', a.base.rstrip('/')):
        raise SystemExit('--base must be https://your.domain')
    print(SetupLinks(a.state_dir).mint(a.base, a.hours, a.reconfigure))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
