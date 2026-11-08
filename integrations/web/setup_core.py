"""First-run setup, shared by the console (bin/connect-accounts.py) and the browser wizard (setup_wizard.py). Tier 1 of the lay-person
install plan (0.30.0): nobody types a pairing code or looks up their own Telegram id.

How the owner is paired, with no code to copy:
  1. the bot token is checked with getMe (gives the bot's @username);
  2. a one-time code is minted and the owner taps https://t.me/<bot>?start=<code> on their phone — Telegram opens the bot and the
     Start button sends "/start <code>";
  3. this module long-polls getUpdates (the gateway's Telegram channel is stopped or not yet enabled, so nothing else is reading)
     until that exact message arrives from a private chat; whoever sent it is the owner. A stranger who finds the bot cannot claim
     it without the code. The update is acknowledged so the gateway never replays it;
  4. config/owner.json (schema 2) and PROCURATOR_OWNER_ID in .env are written, and the Telegram channel is set to
     dmPolicy "allowlist" with allowFrom [owner id] — OpenClaw's recommended setting for one-owner bots, so there is no pairing
     approval step either.

Secrets are only ever written to .env (0600) and are never printed, logged or returned. Every network call goes through an
injectable `opener` so tests run without the internet.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INSTALL_DIR = Path(os.environ.get('PROCURATOR_INSTALL_DIR') or os.environ.get('INSTALL_DIR') or '/opt/procurator')
ANTHROPIC_KEY = re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}')
BOT_TOKEN = re.compile(r'[0-9]{5,12}:[A-Za-z0-9_-]{30,}')
CODE = re.compile(r'[A-Za-z0-9_-]{8,32}')
TELEGRAM_API = 'https://api.telegram.org/bot%s/%s'


DASHES = dict.fromkeys(map(ord, '\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d'), '-')


def _clean(value, pattern):
    """What people actually paste: the key or token plus a line break where the app wrapped it, spaces, a smart dash, or the
    surrounding sentence ("Use this token to access the HTTP API: …"). Normalise dashes and invisible characters, then try, in
    order: one whitespace-separated piece, two or three adjacent pieces joined (a wrapped line), the whole paste without spaces."""
    v = str(value or '').translate(DASHES)
    v = ''.join(ch for ch in v if ch not in '\u200b\u200c\u200d\u2060\ufeff')
    parts = [x.strip('.,;:()[]<>"\'') if not pattern.fullmatch(x) else x for x in v.split()]
    for n in (1, 2, 3):
        for i in range(len(parts) - n + 1):
            cand = ''.join(parts[i:i + n])
            if pattern.fullmatch(cand):
                return cand
    joined = ''.join(v.split())
    return joined if pattern.fullmatch(joined) else joined


class SetupError(ValueError):
    """A message the owner can act on. Never contains a secret."""


def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------- .env and owner.json

def read_env(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, sep, v = line.partition('=')
            if sep and k and not k.startswith('#'):
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def write_env(path, replacements):
    """Replace or add KEY=value lines atomically; the file stays 0600. Other lines (comments included) are kept in order."""
    path = Path(path)
    for k, v in replacements.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', k) or '\n' in str(v) or '\r' in str(v):
            raise SetupError('Refusing to write an unusual setting name or value.')
    lines = [ln for ln in (path.read_text().splitlines() if path.exists() else []) if ln.partition('=')[0].strip() not in replacements]
    lines.extend('%s=%s' % (k, v) for k, v in replacements.items())
    fd, tmp = tempfile.mkstemp(prefix='.env-', dir=str(path.parent))
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_owner(path, owner_id, name='', username='', timezone=''):
    """config/owner.json, schema 2. Keeps fields the owner already set (name, email, timezone) and replaces the primary channel."""
    path = Path(path)
    data = {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data['schema'] = 2
    if name and not data.get('name'):
        data['name'] = name[:80]
    if name and not data.get('short_name'):
        data['short_name'] = name.split()[0][:40]
    if timezone and not data.get('timezone'):
        data['timezone'] = timezone
    data.setdefault('language', 'en')
    others = [c for c in data.get('channels') or [] if isinstance(c, dict) and c.get('type') != 'telegram']
    for c in others:
        c['primary'] = False
    chan = {'type': 'telegram', 'id': str(owner_id), 'primary': True}
    if username:
        chan['label'] = '@' + username[:40]
    data['channels'] = [chan] + others
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.owner-', dir=str(path.parent))
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=1)
        f.write('\n')
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    return data


def owner_configured(owner_file):
    try:
        d = json.loads(Path(owner_file).read_text())
        return any(c.get('type') == 'telegram' and str(c.get('id', '')).isdigit() for c in d.get('channels') or [])
    except (OSError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- key checks

def verify_anthropic(key, opener=_urlopen):
    key = _clean(key, ANTHROPIC_KEY)
    if not ANTHROPIC_KEY.fullmatch(key):
        raise SetupError('That does not look like an Anthropic API key. It starts with "sk-ant-" — copy the whole key from console.anthropic.com › API keys.')
    req = urllib.request.Request('https://api.anthropic.com/v1/models', headers={'x-api-key': key, 'anthropic-version': '2023-06-01'})
    try:
        with opener(req, 20) as r:
            if getattr(r, 'status', 200) != 200:
                raise SetupError('Anthropic answered unexpectedly. Try again in a minute.')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SetupError('Anthropic did not accept that key. Make a new one at console.anthropic.com › API keys and paste all of it.') from None
        raise SetupError('Anthropic answered with an error (HTTP %d). Try again in a minute.' % e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SetupError('This server could not reach Anthropic to check the key. Check its internet connection and try again.') from None
    return key


def telegram(token, method, params=None, opener=_urlopen, timeout=20):
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(TELEGRAM_API % (token, method), data=data)
    try:
        with opener(req, timeout) as r:
            body = json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or '{}')
        except Exception:
            body = {}
        body.setdefault('error_code', e.code)
        body['ok'] = False
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SetupError('This server could not reach Telegram. Check its internet connection and try again.') from None
    return body


def verify_bot(token, opener=_urlopen):
    token = _clean(token, BOT_TOKEN)
    if not BOT_TOKEN.fullmatch(token):
        raise SetupError('That does not look like a bot token. BotFather sends a line like 123456789:AA… — copy the whole thing.')
    r = telegram(token, 'getMe', opener=opener)
    if not r.get('ok') or not (r.get('result') or {}).get('is_bot'):
        raise SetupError('Telegram did not accept that token. In BotFather send /mybots › your bot › API Token, and paste it again.')
    me = r['result']
    return {'token': token, 'username': me.get('username') or '', 'id': me.get('id')}


# ---------------------------------------------------------------- pairing

def new_code():
    return secrets.token_urlsafe(12)[:16]


def pair_link(username, code):
    return 'https://t.me/%s?start=%s' % (username, code)


class GatewayBusy(SetupError):
    """Telegram says another process is reading this bot's updates (the gateway): stop it and retry."""


def wait_for_start(token, code, timeout_s=900, opener=_urlopen, sleep=time.sleep, clock=time.monotonic, poll_s=25):
    """Long-poll getUpdates until "/start <code>" arrives in a private chat. Returns {id, first_name, last_name, username}.
    Acknowledges every update it reads so the gateway does not replay them. Raises SetupError on timeout."""
    if not CODE.fullmatch(code or ''):
        raise SetupError('Internal: bad pairing code.')
    telegram(token, 'deleteWebhook', {'drop_pending_updates': 'false'}, opener=opener)
    deadline = clock() + timeout_s
    offset = None
    want = '/start ' + code
    while clock() < deadline:
        params = {'timeout': str(max(1, min(poll_s, int(deadline - clock())))), 'allowed_updates': json.dumps(['message'])}
        if offset is not None:
            params['offset'] = str(offset)
        r = telegram(token, 'getUpdates', params, opener=opener, timeout=poll_s + 10)
        if not r.get('ok'):
            if r.get('error_code') == 409:
                raise GatewayBusy('Another program is reading this bot (the assistant is already running with it).')
            sleep(2)
            continue
        for u in r.get('result') or []:
            offset = max(offset or 0, int(u.get('update_id', 0)) + 1)
            m = u.get('message') or {}
            chat, frm = m.get('chat') or {}, m.get('from') or {}
            if chat.get('type') == 'private' and str(m.get('text') or '').strip() == want and frm.get('id') and not frm.get('is_bot'):
                telegram(token, 'getUpdates', {'offset': str(offset), 'timeout': '0'}, opener=opener)   # acknowledge
                return {'id': str(frm['id']), 'first_name': frm.get('first_name') or '', 'last_name': frm.get('last_name') or '',
                        'username': frm.get('username') or ''}
    raise SetupError('Nobody pressed Start in time. Open the link again on your phone and tap Start.')


def telegram_batch(owner_id):
    """OpenClaw config for a one-owner bot: allow-list with the owner's numeric id; no groups."""
    return [{'path': 'channels.telegram.enabled', 'value': True},
            {'path': 'channels.telegram.dmPolicy', 'value': 'allowlist'},
            {'path': 'channels.telegram.allowFrom', 'value': [str(owner_id)]},
            {'path': 'channels.telegram.groupPolicy', 'value': 'disabled'},
            {'path': 'channels.telegram.linkPreview', 'value': False},            # a previewed link can leak data to the linked site
            {'path': 'channels.telegram.markdown', 'value': {'tables': 'bullets'}},  # Telegram cannot show tables
            {'path': 'commands.ownerAllowFrom', 'value': ['telegram:' + str(owner_id)]}]   # owner-only slash commands


def say_hello(token, owner_id, text, opener=_urlopen):
    try:
        telegram(token, 'sendMessage', {'chat_id': str(owner_id), 'text': text, 'link_preview_options': json.dumps({'is_disabled': True})}, opener=opener)
        return True
    except SetupError:
        return False


# ---------------------------------------------------------------- applying it on the box

class Box:
    """The few host operations setup needs. `run` is injectable (tests record the commands instead of running them)."""

    def __init__(self, root=INSTALL_DIR, run=None):
        self.root = Path(root)
        self.env = self.root / '.env'
        self.owner_file = Path(os.environ.get('PROCURATOR_OWNER_FILE') or (self.root / 'config' / 'owner.json'))
        self._run = run or (lambda cmd, **kw: subprocess.run(cmd, cwd=str(self.root), check=True, capture_output=True, text=True, timeout=kw.get('timeout', 300)))

    def run(self, cmd, timeout=300):
        try:
            return self._run(cmd, timeout=timeout)
        except subprocess.CalledProcessError as e:
            # The reason goes to the applier's journal (journalctl -u <name>-setup), secrets masked; the page gets the short message.
            why = ((e.stdout or '') + (e.stderr or '')) if isinstance(e.stdout, str) or isinstance(e.stderr, str) else ''
            why = BOT_TOKEN.sub('[bot token]', ANTHROPIC_KEY.sub('[key]', why)).strip()[-1200:]
            if why:
                print('setup: %s failed:\n%s' % (' '.join(cmd[:4]), why), file=sys.stderr)
            raise SetupError('A setup step failed on the server (%s). Run "sudo bash bin/install.sh" again, or see the logs with "docker compose logs --tail 50".' % ' '.join(cmd[:3])) from None
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError('A setup step did not finish on the server (%s).' % ' '.join(cmd[:3])) from None

    def stop_gateway(self):
        self.run(['docker', 'compose', 'stop', 'openclaw-gateway'], timeout=120)

    ONE_OFF = ['docker', 'compose', 'run', '--rm', '--no-deps', '--entrypoint', 'node', 'openclaw-gateway']

    def gateway_running(self):
        try:
            out = self._run(['docker', 'compose', 'ps', '--status', 'running', '-q', 'openclaw-gateway'], timeout=60)
        except Exception:
            return False
        return bool(str(getattr(out, 'stdout', '') or '').strip())

    def cli(self, args, timeout=180):
        """Run the OpenClaw CLI. A running gateway holds the config lock and a one-off container cannot take it (different PID
        namespace: "GatewayLockNamespaceError"), so run it inside the gateway then; use a one-off container only when it is stopped."""
        if self.gateway_running():
            return self.run(['docker', 'compose', 'exec', '-T', 'openclaw-gateway', 'node'] + list(args), timeout=timeout)
        return self.run(self.ONE_OFF + list(args), timeout=timeout)

    def apply_telegram(self, owner_id):
        """Write the allow-list config (inside the running gateway, or a one-off container when it is stopped)."""
        state = self.root / 'state'
        state.mkdir(exist_ok=True)
        batch = state / 'telegram-setup.json'
        batch.write_text(json.dumps(telegram_batch(owner_id)))
        os.chmod(batch, 0o600)
        try:
            os.chown(batch, 1000, 1000)
        except (PermissionError, OSError):
            pass
        self.cli(['dist/index.js', 'config', 'set', '--batch-file', '/home/node/.openclaw/telegram-setup.json'], timeout=180)

    def start_gateway(self):
        # --force-recreate so the container re-reads .env (bot token, owner id, edition).
        self.run(['docker', 'compose', 'up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '150', 'openclaw-gateway'], timeout=240)

    def mark_welcome_pending(self):
        """Ask the assistant to run its welcome interview on the owner's first message (web-plugin/welcome.mjs). The gateway's state
        directory is ./state on the host and /home/node/.openclaw (uid 1000) in the container."""
        d = self.root / 'state' / 'procurator-admin'
        d.mkdir(parents=True, exist_ok=True)
        f = d / 'welcome.json'
        f.write_text(json.dumps({'status': 'pending', 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}) + '\n')
        os.chmod(f, 0o600)
        for p in (d, f):
            try:
                os.chown(p, 1000, 1000)
            except (PermissionError, OSError):
                pass

    def broker_units(self):
        return [u for u in ('procurator-web', 'procurator-mail', 'procurator-web', 'procurator-mail') if (Path('/etc/systemd/system') / (u + '.service')).exists()]

    def restart_brokers(self):
        """The brokers read the owner at start-up. Called by the console and by the setup applier (never by the web broker itself)."""
        units = self.broker_units()
        if units:
            self.run(['systemctl', 'restart'] + units, timeout=120)
        return units


def finish(box, anthropic_key, bot, owner, timezone='', opener=_urlopen, restart=True):
    """Write everything and bring the assistant up. `bot` from verify_bot, `owner` from wait_for_start. Returns a summary
    without secrets."""
    repl = {'TELEGRAM_BOT_TOKEN': bot['token'], 'PROCURATOR_OWNER_ID': owner['id']}
    if anthropic_key:
        repl['ANTHROPIC_API_KEY'] = anthropic_key
    write_env(box.env, repl)
    name = ' '.join(x for x in (owner.get('first_name'), owner.get('last_name')) if x)
    box.apply_telegram(owner['id'])
    box.mark_welcome_pending()
    box.start_gateway()
    # Last, once the assistant is up: a step that fails before this leaves no owner, so the same setup link can simply be retried.
    write_owner(box.owner_file, owner['id'], name=name, username=owner.get('username', ''), timezone=timezone)
    units = box.restart_brokers() if restart else []
    say_hello(bot['token'], owner['id'], 'PROCURATOR is set up and you are its owner. Say hi and I will ask a few quick questions (about two '
              'minutes) so I know what to call you and how I can help. Send /panel any time for your dashboard.', opener=opener)
    return {'bot': '@' + bot['username'], 'owner_id': owner['id'], 'owner_name': name, 'restarted': units}


def setup_status(root=INSTALL_DIR, owner_file=None):
    """What is configured, without secrets: for the console summary and the dashboard Setup tab."""
    root = Path(root)
    env = read_env(root / '.env')
    owner_file = Path(owner_file or os.environ.get('PROCURATOR_OWNER_FILE') or (root / 'config' / 'owner.json'))
    inst = read_env('/etc/procurator/instance.env')
    return {'model_key': bool(ANTHROPIC_KEY.fullmatch(env.get('ANTHROPIC_API_KEY', ''))),
            'openai_key': bool(env.get('OPENAI_API_KEY')),
            'telegram_token': bool(BOT_TOKEN.fullmatch(env.get('TELEGRAM_BOT_TOKEN', ''))),
            'owner': owner_configured(owner_file),
            'edition': (env.get('PROCURATOR_EDITION') or inst.get('EDITION') or 'standard'),
            'domain': inst.get('DOMAIN', ''),
            'browserbase': bool(env.get('BROWSERBASE_API_KEY')) or _exists(STATE_WEB / 'browserbase.env'),
            'twilio': _exists(STATE_VOICE / 'twilio.json')}


STATE_WEB = Path(os.environ.get('PROCURATOR_WEB_STATE', '/var/lib/procurator-web'))
STATE_VOICE = Path(os.environ.get('PROCURATOR_VOICE_STATE', '/var/lib/procurator-voice'))


def _exists(p):
    try:
        return Path(p).exists()
    except OSError:
        return False


# ---------------------------------------------------------------- hand-off to the privileged applier
# The web broker is sandboxed (ProtectSystem=strict): it cannot write .env or run docker, and it should not. The browser wizard
# therefore writes one request file into its own state directory; a root one-shot unit (procurator-setup.path ->
# procurator-setup.service -> bin/setup-apply.py) applies it, deletes it, and writes a result file without secrets.

class FileApplier:
    def __init__(self, state_dir=None, sleep=time.sleep, clock=time.monotonic):
        self.dir = Path(state_dir or STATE_WEB) / 'setup'
        self.sleep, self.clock = sleep, clock

    def __call__(self, action, payload, timeout_s=240):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        rid = secrets.token_hex(8)
        req = self.dir / 'request.json'
        if req.exists():
            raise SetupError('Another setup step is still being applied. Wait a few seconds and try again.')
        fd, tmp = tempfile.mkstemp(prefix='.req-', dir=str(self.dir))
        with os.fdopen(fd, 'w') as f:
            json.dump({'id': rid, 'action': action, **payload}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, req)
        deadline = self.clock() + timeout_s
        res = self.dir / 'result.json'
        while self.clock() < deadline:
            try:
                r = json.loads(res.read_text())
            except (OSError, ValueError):
                r = {}
            if isinstance(r, dict) and r.get('id') == rid:
                # Outside the try: SetupError is a ValueError, and swallowing it left the page on "finishing" until the timeout.
                if not r.get('ok'):
                    raise SetupError(r.get('error') or 'That step did not work on the server.')
                return r
            self.sleep(1)
        raise SetupError('The server did not apply the step in time. Check that the setup service is installed (sudo bash bin/install.sh again).')


def apply_request(req, box, opener=_urlopen):
    """Run one request from the wizard (as root, from bin/setup-apply.py). Returns a result dict without secrets."""
    action = req.get('action')
    if action == 'save_model':
        key = verify_anthropic(req.get('key'), opener=opener)
        write_env(box.env, {'ANTHROPIC_API_KEY': key})
        return {'ok': True}
    if action == 'stop_gateway':
        box.stop_gateway()
        return {'ok': True}
    if action == 'finish':
        bot = verify_bot(req.get('bot_token'), opener=opener)
        owner = req.get('owner') or {}
        if not str(owner.get('id', '')).isdigit():
            raise SetupError('Internal: no owner id.')
        out = finish(box, None, bot, owner, timezone=req.get('timezone', ''), opener=opener, restart=False)
        return {'ok': True, 'bot': out['bot'], 'owner_name': out['owner_name'], 'owner_first': str(owner.get('first_name') or '')[:40], 'restart': True}
    raise SetupError('Unknown setup step.')
