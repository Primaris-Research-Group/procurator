"""PROCURATOR dashboard (Nov 6 2026): one private page for what the assistant is doing, what it costs, the
task packets it runs, builds, machines, activity and settings.

Served on 127.0.0.1:3343 only; nginx forwards https://assistant.example.com/assistant/dashboard/… to it.
Login: /panel in Telegram sends a single-use link (45 minutes); opening it sets a signed, HttpOnly,
Secure, SameSite=Strict cookie that lasts 30 days on up to 3 devices. There is no password.

The dashboard is a window and a control panel onto the brokers' own stores. It can upload, validate,
activate, pause, resume and close packets, change their cadence, hours, budget and model, answer a
packet question, switch quiet mode and the daily budget, and revoke its own devices. It can NEVER
approve a call, email, trash, calendar change, purchase, build, browser task or final submit: those
stay native-owner Telegram commands, and the page only shows the line to tap. No credential of any
kind is ever rendered or returned by the API. Paths and bodies are never logged.
"""
import csv
import datetime as dt
import glob
import hashlib
import hmac
import html
import http.server
import io
import json
import os
import re
import secrets
import socketserver
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import dashboard_assets as A
import packets

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo('America/New_York')
except Exception:  # pragma: no cover
    EASTERN = dt.timezone(dt.timedelta(hours=-5))

PUBLIC = os.environ.get('PROCURATOR_PUBLIC_BASE', 'https://assistant.example.com')
ORIGIN = PUBLIC
ROOT = '/assistant/dashboard'
LOGIN_MINUTES = 45
SESSION_DAYS = 30
MAX_DEVICES = 3
MAX_BODY = 2_600_000
ONLINE_MS = 3 * 60000
COOKIE = 'procurator_session'
NEXT_OK = re.compile(r'^(/compose/[a-f0-9]{16}|/accounts|/setup)$')      # 0.24.0: /compose lands on the session; 0.30.0: setup lands on Accounts
TABS = [('', 'Overview'), ('live', 'Live'), ('packets', 'Packets'), ('compose', 'Compose'), ('library', 'Library'), ('builds', 'Builds'), ('health', 'Health'), ('usage', 'Usage'), ('machines', 'Machines'), ('activity', 'Activity'), ('accounts', 'Accounts'), ('setup', 'Setup'), ('settings', 'Settings')]
MODEL_LABELS = {'anthropic/claude-sonnet-5-5': 'Claude Sonnet 5.5', 'anthropic/claude-opus-5-5': 'Claude Opus 5.5', 'openai/gpt-6.1-sol': 'GPT-6.1 Sol', 'openai/gpt-6.1-terra': 'GPT-6.1 Terra',
                'openai/gpt-5.6-terra': 'GPT-5.6 Terra'}
esc = A.esc


def day_key(t):
    return dt.datetime.fromtimestamp(t, EASTERN).strftime('%Y-%m-%d')


def when(t):
    if not t:
        return ''
    if t > 1e11:
        t = t / 1000.0
    return dt.datetime.fromtimestamp(t, EASTERN).strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')


def ago(ms_or_s, now):
    t = ms_or_s / 1000.0 if ms_or_s > 1e11 else ms_or_s
    d = max(0, now - t)
    if d < 90:
        return 'just now'
    if d < 3600:
        return '%d min ago' % round(d / 60)
    if d < 172800:
        return '%d h ago' % round(d / 3600)
    return '%d days ago' % round(d / 86400)


class Sessions:
    """Login tokens, signed session cookies and the dashboard's own settings (quiet mode, daily budget)."""

    def __init__(self, path, now=time.time):
        self.path, self.now, self.lock = str(path), now, threading.RLock()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS login_tokens(hash TEXT PRIMARY KEY,expires REAL,used REAL);
            CREATE TABLE IF NOT EXISTS sessions(id_hash TEXT PRIMARY KEY,device_label TEXT,created REAL,last_seen REAL,revoked REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS validations(hash TEXT PRIMARY KEY,expires REAL);''')
            cols = [r['name'] for r in db.execute('PRAGMA table_info(sessions)')]
            if 'method' not in cols:
                # 0.28.0 (B9): how the session was issued (telegram_link, telegram_miniapp, reserved device_token) and its own lifetime.
                db.execute("ALTER TABLE sessions ADD COLUMN method TEXT DEFAULT 'telegram_link'")
                db.execute('ALTER TABLE sessions ADD COLUMN ttl REAL')
        self.key_path = Path(self.path).with_name('dashboard.key')
        if not self.key_path.exists():
            self.key_path.write_bytes(secrets.token_bytes(32))
            os.chmod(self.key_path, 0o600)
        self.key = self.key_path.read_bytes()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def h(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def sign(self, value, purpose='session'):
        return hmac.new(self.key, (purpose + ':' + value).encode(), hashlib.sha256).hexdigest()[:40]

    # ---- login links ----
    def login_link(self, next_path=None):
        token = secrets.token_urlsafe(32)
        with self.lock, self.db() as db:
            db.execute('DELETE FROM login_tokens WHERE expires<?', (self.now() - 86400,))
            db.execute('INSERT INTO login_tokens VALUES(?,?,NULL)', (self.h(token), self.now() + LOGIN_MINUTES * 60))
        return PUBLIC + ROOT + '/login/' + token + (('?next=' + next_path) if next_path and NEXT_OK.match(next_path) else '')

    def login(self, token, device_label):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}', token):
            raise ValueError('This sign-in link is not valid.')
        with self.lock, self.db() as db:
            row = db.execute('SELECT * FROM login_tokens WHERE hash=?', (self.h(token),)).fetchone()
            if not row or row['used'] or row['expires'] < self.now():
                raise ValueError('This sign-in link expired or was already used. Send /panel in Telegram for a fresh one.')
            db.execute('UPDATE login_tokens SET used=? WHERE hash=?', (self.now(), row['hash']))
            sid = secrets.token_urlsafe(32)
            now = self.now()
            active = db.execute('SELECT id_hash FROM sessions WHERE revoked IS NULL AND created>? ORDER BY last_seen', (now - SESSION_DAYS * 86400,)).fetchall()
            for old in active[:max(0, len(active) - (MAX_DEVICES - 1))]:
                db.execute('UPDATE sessions SET revoked=? WHERE id_hash=?', (now, old['id_hash']))
            db.execute('INSERT INTO sessions(id_hash,device_label,created,last_seen,revoked,method,ttl) VALUES(?,?,?,?,NULL,?,NULL)',
                       (self.h(sid), str(device_label or 'device')[:80], now, now, 'telegram_link'))
        return sid + '.' + self.sign(sid)

    def issue(self, method, device_label, ttl):
        """0.28.0: a session from another sign-in method (telegram_miniapp: 12 hours). Same device cap as link sign-ins."""
        if method not in ('telegram_miniapp',):
            raise ValueError('Unknown sign-in method.')
        sid, now = secrets.token_urlsafe(32), self.now()
        with self.lock, self.db() as db:
            active = db.execute('SELECT id_hash FROM sessions WHERE revoked IS NULL AND created>? ORDER BY last_seen', (now - SESSION_DAYS * 86400,)).fetchall()
            for old in active[:max(0, len(active) - (MAX_DEVICES - 1))]:
                db.execute('UPDATE sessions SET revoked=? WHERE id_hash=?', (now, old['id_hash']))
            db.execute('INSERT INTO sessions(id_hash,device_label,created,last_seen,revoked,method,ttl) VALUES(?,?,?,?,NULL,?,?)',
                       (self.h(sid), str(device_label)[:80], now, now, method, ttl))
        return sid + '.' + self.sign(sid)

    def session(self, cookie):
        """Session id for a valid cookie value, else None. Touches last_seen about once a minute."""
        if not isinstance(cookie, str) or '.' not in cookie:
            return None
        sid, sig = cookie.split('.', 1)
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,64}', sid) or not hmac.compare_digest(sig, self.sign(sid)):
            return None
        with self.lock, self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE id_hash=?', (self.h(sid),)).fetchone()
            if not row or row['revoked'] or row['created'] < self.now() - SESSION_DAYS * 86400:
                return None
            if row['ttl'] and row['created'] < self.now() - row['ttl']:
                return None
            if self.now() - (row['last_seen'] or 0) > 60:
                db.execute('UPDATE sessions SET last_seen=? WHERE id_hash=?', (self.now(), row['id_hash']))
        return sid

    def csrf(self, sid):
        return self.sign(sid, 'csrf')

    def devices(self, current_sid=None):
        cur = self.h(current_sid) if current_sid else None
        with self.db() as db:
            rows = db.execute('SELECT * FROM sessions WHERE revoked IS NULL AND created>? ORDER BY created DESC', (self.now() - SESSION_DAYS * 86400,)).fetchall()
        return [{'id': r['id_hash'][:12], 'device': r['device_label'], 'signed_in': when(r['created']), 'last_seen': when(r['last_seen']), 'this_device': r['id_hash'] == cur,
                 'method': r['method'] or 'telegram_link'} for r in rows]

    def revoke(self, short_id):
        with self.lock, self.db() as db:
            n = db.execute('UPDATE sessions SET revoked=? WHERE revoked IS NULL AND substr(id_hash,1,12)=?', (self.now(), str(short_id)[:12])).rowcount
        return n

    def logout_all(self):
        with self.lock, self.db() as db:
            return db.execute('UPDATE sessions SET revoked=? WHERE revoked IS NULL', (self.now(),)).rowcount

    # ---- settings ----
    def get(self, key, default=None):
        with self.db() as db:
            r = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(r['value']) if r else default

    def set(self, key, value):
        with self.lock, self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    # ---- validation tokens (a save needs a prior validation of the same files) ----
    def validated(self, files):
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        with self.lock, self.db() as db:
            db.execute('DELETE FROM validations WHERE expires<?', (self.now(),))
            db.execute('INSERT OR REPLACE INTO validations VALUES(?,?)', (digest, self.now() + 900))
        return digest

    def check_validated(self, files, token):
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if token != digest:
            return False
        with self.db() as db:
            r = db.execute('SELECT expires FROM validations WHERE hash=?', (digest,)).fetchone()
        return bool(r and r['expires'] >= self.now())


class Dashboard:
    def __init__(self, broker, state_dir, db_path, vitals=None, now=time.time):
        self.broker, self.state, self.now = broker, Path(state_dir), now
        self.sessions = Sessions(db_path, now)
        self.vitals = vitals
        self.alerts = {}
        # 0.29.0: Accounts (Apple / Google by email + app password). The store lives beside the dashboard db; secrets beside the broker's.
        try:
            from accounts import Accounts
            secrets_dir = Path(os.environ.get('PROCURATOR_SECRETS', str(Path(getattr(broker, 'path', db_path)).parent / 'secrets')))
            self.accounts = Accounts(Path(db_path).with_name('accounts.sqlite3'), secrets_dir, now=now)
        except Exception:
            self.accounts = None
        # 0.30.0: first-run browser setup (setup_wizard.py). The one-time link lives beside the dashboard db; privileged steps go
        # to the root setup applier through a request file (this broker is sandboxed and never writes .env itself).
        try:
            import setup_core
            import setup_wizard
            tz = setup_core.read_env(setup_core.INSTALL_DIR / '.env').get('OPENCLAW_TZ', '')
            self.setup = setup_wizard.Wizard(setup_wizard.SetupLinks(Path(db_path).parent), setup_core.Box(), login_link=self.sessions.login_link,
                                             timezone=tz)
        except Exception:
            self.setup = None

    # ---------- read-only data ----------
    def read_json(self, path, default):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default

    def model_info(self):
        c = self.read_json(self.state / 'openclaw.json', {})
        agents = c.get('agents') or {}
        m = ((agents.get('entries') or {}).get('main') or {}).get('model') or (agents.get('defaults') or {}).get('model')
        primary = m if isinstance(m, str) else (m or {}).get('primary') if isinstance(m, dict) else None
        fallbacks = (m or {}).get('fallbacks') if isinstance(m, dict) else None
        return {'primary': primary, 'label': MODEL_LABELS.get(primary or '', primary or 'unknown'), 'fallbacks': [MODEL_LABELS.get(f, f) for f in (fallbacks or [])]}

    def usage(self, days=14):
        d = self.read_json(self.state / 'procurator-admin' / 'usage.json', {}) or {}
        all_days = d.get('days') if isinstance(d.get('days'), dict) else {}
        today = day_key(self.now())
        # Every calendar day of the window, so charts show the gaps (days with no work) instead of hiding them.
        keys = [(dt.datetime.fromtimestamp(self.now(), EASTERN) - dt.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days - 1, -1, -1)]
        blank = lambda: {'calls': 0, 'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'cost': 0}
        def add(a, b):
            for f in a:
                a[f] += float((b or {}).get(f) or 0)
            return a
        rows, by, by_mission, models = [], {}, {}, {}
        # 0.24.0: the Packet Composer calls the model from the broker, outside the gateway's meter; its spend is kept by the Composer.
        try:
            comp = self.broker.composer.daily(days + 2) if getattr(self.broker, 'composer', None) else {}
        except Exception:
            comp = {}
        for k in keys:
            t = all_days.get(k) or {}
            total = add(blank(), t.get('total'))
            if comp.get(k):
                add(total, comp[k])
            total['day'] = k
            total['by'] = {s: add(blank(), v) for s, v in (t.get('by') or {}).items()}
            if comp.get(k):
                total['by']['composer'] = add(blank(), comp[k])
            total['by_mission'] = {s: add(blank(), v) for s, v in (t.get('by_mission') or {}).items()}
            total['builds'] = float((t.get('builds') or {}).get('cost') or 0)
            total['model'] = t.get('model')
            rows.append(total)
            for s, v in total['by'].items():
                by[s] = add(by.get(s, blank()), v)
            for s, v in total['by_mission'].items():
                by_mission[s] = add(by_mission.get(s, blank()), v)
            if t.get('model'):
                models[t['model']] = models.get(t['model'], 0) + total['input'] + total['output']
        period = add(blank(), None)
        for r in rows:
            add(period, r)
        today_row = next((r for r in rows if r['day'] == today), None)
        return {'days': rows, 'period': period, 'by': by, 'by_mission': by_mission, 'today': today_row, 'models': models, 'recorded': bool(all_days), 'window': days,
                'build_cost': round(sum(r['builds'] for r in rows), 4), 'today_key': today}

    def builders(self):
        out, now = [], self.now()
        for f in sorted(glob.glob(str(self.state / 'procurator-build' / 'builders' / '*.json'))):
            b = self.read_json(f, None)
            if not isinstance(b, dict):
                continue
            seen = float(b.get('seen_at') or 0)
            v = b.get('vitals') if isinstance(b.get('vitals'), dict) else {}
            out.append({'name': os.path.basename(f)[:-5], 'label': str(b.get('label') or '')[:30] or os.path.basename(f)[:-5], 'online': now * 1000 - seen < ONLINE_MS,
                        'last_seen': ago(seen, now) if seen else 'never', 'repos': [r for r in (b.get('repos') or []) if isinstance(r, str)][:50],
                        'agents': [k for k, v2 in (b.get('agents') or {}).items() if v2], 'busy': b.get('busy') if isinstance(b.get('busy'), str) else None,
                        'version': str(b.get('version') or '')[:20], 'host': str(b.get('host') or '')[:60],
                        'vitals': {k: v.get(k) for k in ('load1', 'cpus', 'mem_used', 'mem_total', 'disk_free', 'disk_total', 'uptime', 'os') if k in v}})
        return out

    def builds(self, limit=60):
        j = self.read_json(self.state / 'procurator-build' / 'jobs.json', {}) or {}
        jobs = j.get('jobs') if isinstance(j.get('jobs'), list) else []
        out = []
        for job in reversed(jobs[-limit:]):
            if not isinstance(job, dict):
                continue
            jid = str(job.get('id') or '')
            r = self.read_json(self.state / 'procurator-build' / 'reports' / (jid + '.json'), {}) if re.fullmatch(r'[a-f0-9]{16}', jid) else {}
            plan = job.get('plan') if isinstance(job.get('plan'), dict) else {}
            tests = r.get('tests') if isinstance(r.get('tests'), dict) else {}
            out.append({'id': jid, 'status': str(job.get('status') or r.get('status') or ''), 'title': str(plan.get('title') or '')[:100], 'repo': str(plan.get('repo') or '')[:140],
                        'agent': str(plan.get('agent') or '')[:10], 'builder': str(r.get('builder') or job.get('builder') or plan.get('builder') or '')[:20],
                        'mission': str(job.get('mission') or '')[:24], 'step': str(job.get('step') or '')[:12],
                        'created': float(job.get('created_at') or job.get('created') or 0), 'updated': float(r.get('updated_at') or job.get('updated_at') or 0),
                        'duration_s': r.get('duration_s') if isinstance(r.get('duration_s'), int) else None, 'cost_usd': r.get('cost_usd') if isinstance(r.get('cost_usd'), (int, float)) else None,
                        'pr_url': r.get('pr_url') if isinstance(r.get('pr_url'), str) and re.match(r'^https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/pull/\d+$', r['pr_url']) else None,
                        'tests': {'status': str(tests.get('status') or '')[:20], 'command': str(tests.get('command') or '')[:200]} if tests else None,
                        'summary': str(r.get('summary') or '')[:3000], 'message': str(r.get('message') or '')[:600], 'files': r.get('file_count'),
                        'log_tail': str((tests or {}).get('output_tail') or '')[-2000:],
                        'model': str(plan.get('model') or r.get('model') or '')[:64], 'question': str(r.get('question') or '')[:1500] if r.get('status') == 'waiting_owner' else '',
                        'progress': self.build_progress(jid), 'verification': self.build_verification(r.get('verification'))})
        return out

    def build_verification(self, v):
        """Verified PRs (0.32.0): the builder's acceptance checks, independent review and screenshots, size-limited."""
        if not isinstance(v, dict):
            return None
        checks = [{'n': int(c.get('n') or 0), 'text': str(c.get('text') or '')[:300], 'status': str(c.get('status') or '')[:12], 'output': str(c.get('output') or '')[:600]}
                  for c in (v.get('checks') or [])[:10] if isinstance(c, dict)]
        rv = v.get('review') if isinstance(v.get('review'), dict) else None
        review = {'verdict': str(rv.get('verdict') or '')[:20], 'agent': str(rv.get('agent') or '')[:20], 'model': str(rv.get('model') or '')[:64], 'summary': str(rv.get('summary') or '')[:1500],
                  'issues': [{'severity': str(i.get('severity') or '')[:8], 'file': str(i.get('file') or '')[:200], 'text': str(i.get('text') or '')[:500]} for i in (rv.get('issues') or [])[:12] if isinstance(i, dict)]} if rv else None
        return {'checks': checks, 'review': review, 'rounds': int(v.get('rounds') or 0), 'unresolved': str(v.get('unresolved') or '')[:20], 'screenshots': int(v.get('screenshots') or 0),
                'screenshot_note': str(v.get('screenshot_note') or '')[:300], 'passed': v.get('passed') is True}

    def build_progress(self, jid, n=40):
        """Live builds (0.31.0): the relay appends what the agent is doing to progress/<id>.jsonl; the last n lines, newest last."""
        if not re.fullmatch(r'[a-f0-9]{16}', jid or ''):
            return []
        try:
            lines = (self.state / 'procurator-build' / 'progress' / (jid + '.jsonl')).read_text(errors='replace').splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append({'t': str(e.get('t') or '')[:10], 'text': str(e.get('text') or '')[:400], 'name': str(e.get('name') or '')[:40], 'at': e.get('at') if isinstance(e.get('at'), int) else 0})
        return out

    def missions(self):
        return self.broker.call('mission_list', {}, True).get('missions', [])

    def mission(self, slug):
        return self.broker.call('mission_get', {'mission': slug}, True)

    def approvals(self):
        try:
            items = self.broker.upcoming_items()
        except Exception:
            items = []
        return [x for x in items if x.get('group') == 'Waiting for you']

    def activity(self, days=7, kind=None, limit=100):
        try:
            return self.broker.activity.search({'days': days, 'kind': kind or None, 'limit': limit}).get('entries', [])
        except Exception:
            return []

    def daily_budget_state(self, usage=None):
        u = usage or self.usage(1)
        budget = self.sessions.get('daily_budget_usd')
        spent = float((u.get('today') or {}).get('cost') or 0) + float((u.get('today') or {}).get('builds') or 0)
        state = {'daily_budget_usd': budget, 'spent_today': round(spent, 4), 'fraction': (spent / budget) if budget else None}
        if budget:
            self.budget_alerts(state)
        return state

    def budget_alerts(self, state):
        """One Telegram line at 80% and one at 100% of the daily budget, each once per day (the highest level only)."""
        day = day_key(self.now())
        f = state['fraction'] or 0
        level = 1.0 if f >= 1.0 else 0.8 if f >= 0.8 else None
        if level is None or self.alerts.get((day, level)):
            return
        for lv in (0.8, 1.0):
            if lv <= level:
                self.alerts[(day, lv)] = True
        try:
            if level == 1.0:
                msg = 'All packet work sessions are paused for today: the daily budget of $%.2f is spent ($%.2f). Raise it on the dashboard to continue.' % (state['daily_budget_usd'], state['spent_today'])
            else:
                msg = 'Heads-up: $%.2f of today\'s $%.2f assistant budget is spent (%d%%).' % (state['spent_today'], state['daily_budget_usd'], round(f * 100))
            self.broker.provider.telegram(msg)
        except Exception:
            pass

    def paused_all(self):
        """Reason packet ticks are paused globally (quiet mode or the daily budget), else None. Used by Missions.due()."""
        if self.sessions.get('quiet_mode'):
            return 'quiet mode'
        try:
            s = self.daily_budget_state()
            if s['fraction'] is not None and s['fraction'] >= 1.0:
                return 'daily budget spent'
        except Exception:
            return None
        return None

    def overview(self):
        u = self.usage(14)
        missions = self.missions()
        active = [m for m in missions if m['status'] in ('active', 'blocked', 'over_budget', 'paused')]
        builders = self.builders()
        return {'model': self.model_info(), 'usage': {'today': u['today'] or {'calls': 0, 'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'cost': 0, 'builds': 0},
                                                      'period': u['period'], 'by': u['by'], 'days': u['window'], 'recorded': u['recorded'], 'build_cost': u['build_cost']},
                'daily_budget': self.daily_budget_state(u), 'quiet_mode': bool(self.sessions.get('quiet_mode')), 'paused_all': self.paused_all(),
                'builders': {'online': sum(b['online'] for b in builders), 'total': len(builders), 'list': builders},
                'packets': {'active': len([m for m in missions if m['status'] == 'active']), 'total': len(missions), 'list': active[:10]},
                'approvals': self.approvals(), 'activity': self.activity(3, None, 10), 'droplet': self.vitals.latest() if self.vitals else None, 'at': when(self.now())}

    # ---------- writes (owner session only) ----------
    def checks(self):
        try:
            logins = [s['site'] for s in self.broker.call('site_list', {}, True).get('sites', [])]
        except Exception:
            logins = []
        return {'saved_logins': logins, 'builders': self.builders()}

    def validate_packet(self, files):
        if not isinstance(files, dict) or not files or len(files) > 25:
            raise ValueError('Pick 1–25 .md or .txt files.')
        files = {str(k)[:80]: (v if isinstance(v, str) else '') for k, v in files.items()}
        out = self.broker.call('mission_validate', {'files': files}, True)
        if out.get('ok'):
            out['token'] = self.sessions.validated(files)
        return out

    def save_packet(self, body):
        files = body.get('files')
        if not isinstance(files, dict):
            raise ValueError('files must be an object of name -> text.')
        files = {str(k)[:80]: (v if isinstance(v, str) else '') for k, v in files.items()}
        if not self.sessions.check_validated(files, body.get('token')):
            raise ValueError('Validate the packet first (pick the files again); the check expires after 15 minutes.')
        slug = (body.get('mission') or '').strip().lower() or None
        out = self.broker.missions.save_packet(slug, files, 'owner_upload', title=(body.get('title') or None), activate=body.get('activate') is True)
        try:
            self.broker.provider.telegram('Mission %s saved from the dashboard: %s (revision %s, %s packet), %d file(s), %s.' % (
                out['mission'], out['title'], out['revision'], out.get('kind') or 'administrative', len(out['files']), 'ACTIVE' if out['status'] == 'active' else out['status']))
        except Exception:
            pass
        return out

    def packet_state(self, slug, action):
        if action not in ('activate', 'pause', 'resume', 'close'):
            raise ValueError('action must be activate, pause, resume or close.')
        out = self.broker.call('mission_owner', {'mission': slug, 'action': action, 'reason': 'Paused from the dashboard' if action == 'pause' else None}, True)
        if action in ('activate', 'resume'):
            self.wake_soon()
        return out

    def packet_settings(self, slug, body):
        out = None
        if body.get('cadence') not in (None, ''):
            try:
                minutes = int(body['cadence'])
            except (TypeError, ValueError):
                raise ValueError('Cadence is a number of minutes (60–1440).')
            out = self.broker.call('mission_owner', {'mission': slug, 'action': 'cadence', 'minutes': minutes}, True)
        if body.get('hours') not in (None, ''):
            out = self.broker.call('mission_owner', {'mission': slug, 'action': 'hours', 'hours': str(body['hours'])}, True)
        if 'budget' in body:
            b = str(body.get('budget') or '').strip()
            out = self.broker.call('mission_owner', {'mission': slug, 'action': 'budget', 'budget_usd': b or None}, True)
        if 'model' in body:
            out = self.broker.call('mission_owner', {'mission': slug, 'action': 'model', 'model': str(body.get('model') or '')}, True)
        if out is None:
            raise ValueError('Nothing to change.')
        return out

    def answer_question(self, slug, body):
        qid, answer = str(body.get('question_id') or ''), str(body.get('answer') or '').strip()
        if not answer:
            raise ValueError('Write the answer first.')
        out = self.broker.call('mission_question', {'mission': slug, 'action': 'answer', 'question_id': qid, 'answer': answer[:1200]}, True)
        try:
            self.broker.call('mission_log', {'mission': slug, 'request_id': 'dash-answer-' + qid[:30] + '-' + str(int(self.now())), 'kind': 'owner_answer',
                                             'summary': 'The owner answered %s on the dashboard.' % qid, 'outcome': 'observed'}, True)
        except Exception:
            pass
        self.wake_soon()
        return out

    def wake_soon(self):
        """The plugin's dispatcher polls every 5 minutes; nothing to do here but it is the hook for a push later."""
        return None

    def settings_update(self, body):
        changed = []
        if 'quiet_mode' in body:
            self.sessions.set('quiet_mode', bool(body['quiet_mode']) if not isinstance(body['quiet_mode'], str) else body['quiet_mode'].lower() in ('1', 'true', 'on', 'yes'))
            changed.append('quiet mode ' + ('on' if self.sessions.get('quiet_mode') else 'off'))
        if 'daily_budget_usd' in body:
            v = str(body.get('daily_budget_usd') or '').strip().lstrip('$')
            if v:
                try:
                    amount = round(float(v), 2)
                except ValueError:
                    raise ValueError('Daily budget must be a number of dollars.')
                if not 1 <= amount <= 500:
                    raise ValueError('Daily budget is $1–$500.')
                self.sessions.set('daily_budget_usd', amount)
                changed.append('daily budget $%.2f' % amount)
            else:
                self.sessions.set('daily_budget_usd', None)
                changed.append('daily budget cleared')
            self.alerts = {}
        for field, scope, what in (('composer_model', 'composer', 'Packet Composer'), ('packet_model', 'packets', 'packet work sessions')):
            if body.get(field):
                self.broker.call('model_settings', {'set': {scope: body[field]}}, True)
                changed.append('%s model %s' % (what, body[field]))
        if not changed:
            raise ValueError('Nothing to change.')
        if self.sessions.get('daily_budget_usd'):
            self.daily_budget_state()
        return {'changed': changed}

    def settings(self, sid=None):
        b = self.broker
        try:
            cards = b.purchase_cards()
            cards = len(cards) if isinstance(cards, (list, dict)) else 0
        except Exception:
            cards = 0
        try:
            cap = b.purchase_cap()
        except Exception:
            cap = None
        try:
            siteauto = b.task_autostart()
        except Exception:
            siteauto = []
        try:
            logins = [{'site': s['site'], 'status': s['status']} for s in b.call('site_list', {}, True).get('sites', [])]
        except Exception:
            logins = []
        try:
            models = self.broker.call('model_settings', {}, True)
        except Exception:
            models = None
        return {'model': self.model_info(), 'models': models, 'quiet_mode': bool(self.sessions.get('quiet_mode')), 'daily_budget_usd': self.sessions.get('daily_budget_usd'),
                'purchase_cap_usd': cap, 'purchase_cards': cards, 'siteauto': siteauto, 'logins': logins, 'devices': self.sessions.devices(sid),
                'defaults': {'cadence': 240, 'hours': '07:00-21:00', 'max_parallel_packets': 2}}

    # ---------- API dispatch ----------
    def api(self, method, path, body, sid):
        parts = [p for p in path.split('/') if p]
        if method == 'GET':
            if parts == ['overview']:
                return self.overview()
            if parts == ['packets']:
                return {'packets': self.missions()}
            if len(parts) == 2 and parts[0] == 'packets':
                return self.mission(parts[1])
            if parts == ['templates']:
                return self.broker.call('mission_templates', {}, True)
            if parts == ['builds']:
                return {'builds': self.builds()}
            if len(parts) == 3 and parts[0] == 'builds' and parts[2] == 'log':
                b = next((x for x in self.builds() if x['id'] == parts[1]), None)
                if not b:
                    raise ValueError('No build with that id.')
                return {'id': b['id'], 'log_tail': b['log_tail'], 'summary': b['summary']}
            if parts == ['usage']:
                return self.usage(int(body.get('days', 14)) if str(body.get('days', '')).isdigit() else 14)
            if parts == ['usage.csv']:
                return {'csv': self.usage_csv(int(body.get('days', 30)) if str(body.get('days', '')).isdigit() else 30)}
            if parts == ['browser-week']:
                return self.broker.call('task_week', {'days': int(body.get('days', 7)) if str(body.get('days', '')).isdigit() else 7}, True)
            if parts == ['machines']:
                return {'droplet': self.vitals.latest() if self.vitals else None, 'series': self.vitals.series() if self.vitals else [], 'builders': self.builders()}
            if parts == ['activity']:
                days = int(body.get('days', 7)) if str(body.get('days', '')).isdigit() else 7
                return {'entries': self.activity(max(1, min(400, days)), body.get('kind') or None)}
            if parts == ['accounts']:
                return self.accounts_list()
            if parts == ['settings']:
                return self.settings(sid)
            if parts == ['devices']:
                return self.broker.call('devices_list', {}, True)
            if parts == ['events']:
                return {'events': self.broker.stream.since(int(body.get('since', 0)) if str(body.get('since', '')).isdigit() else 0, 100)}
            if parts == ['live']:
                return {**self.broker.call('live_status', {}, True), 'recent': self.broker.stream.recent(40),
                        'approvals': 'Approvals happen only in Telegram; open the chat to approve.', 'chat': 'https://t.me/'}
            if parts == ['library']:
                return self.broker.call('library_list', {'collection': body.get('collection') or None}, True)
            if len(parts) == 2 and parts[0] == 'library':
                return {'item': self.broker.call('library_read', {'item': parts[1], 'part': int(body.get('part', 0)) if str(body.get('part', '0')).isdigit() else 0}, True)}
            if parts == ['compose']:
                return self.composer().call('composer_list', {}, True)
            if len(parts) == 2 and parts[0] == 'compose':
                return self.composer().call('composer_get', {'id': parts[1]}, True)
            raise ValueError('Not found.')
        if method == 'POST':
            if parts == ['library', 'note']:
                item = self.broker.call('library_note', {'text': body.get('text'), 'title': body.get('title'), 'collection': body.get('collection'),
                                                         'use_chat': body.get('use_chat', '1'), 'use_packets': body.get('use_packets', '1'), 'pinned': body.get('pinned', '0')}, True)
                return {'item': item, 'message': 'Note saved to %s.' % item['collection']}
            if len(parts) == 3 and parts[0] == 'library' and parts[2] == 'update':
                return {'item': self.broker.call('library_update', {'item': parts[1], **{k: body.get(k) for k in ('collection', 'use_chat', 'use_packets', 'pinned', 'title')}}, True), 'message': 'Saved.'}
            if len(parts) == 3 and parts[0] == 'library' and parts[2] == 'delete':
                return {**self.broker.call('library_delete', {'item': parts[1]}, True), 'message': 'Deleted from the server.'}
            if len(parts) == 3 and parts[0] == 'live' and parts[2] == 'reveal':
                return {**self.broker.call('live_reveal_request', {'task': parts[1], 'session': self.sessions.h(sid)[:12]}, True), 'message': 'Confirm in Telegram with the /livereveal line.'}
            if parts == ['oauth', 'begin']:
                r = self.broker.call('oauth_begin', {'provider': body.get('provider')}, True)
                return {'go': r['url'], 'message': 'Opening the provider\'s sign-in…'}
            if parts == ['oauth', 'disconnect']:
                r = self.broker.call('oauth_disconnect', {'grant': body.get('grant')}, True)
                return {**r, 'message': 'Disconnected; tokens deleted.'}
            if parts == ['packets', 'validate']:
                return self.validate_packet(body.get('files'))
            if parts == ['packets']:
                return self.save_packet(body)
            if len(parts) == 3 and parts[0] == 'packets':
                slug = parts[1]
                if parts[2] == 'state':
                    return {**self.packet_state(slug, body.get('action')), 'message': 'Packet %s: %s.' % (slug, body.get('action'))}
                if parts[2] == 'settings':
                    return {**self.packet_settings(slug, body), 'message': 'Saved.'}
                if parts[2] == 'answer':
                    return {**self.answer_question(slug, body), 'message': 'Answer recorded; the packet continues at its next work session.'}
                if parts[2] == 'dryrun':
                    self.broker.call('mission_dry_run', {'mission': slug}, True)
                    return {'message': 'The read-only plan is in Telegram.'}
            if parts[:1] == ['accounts']:
                return self.accounts_post(parts[1:], body)
            if parts == ['settings']:
                out = self.settings_update(body)
                return {**out, 'message': 'Saved: ' + ', '.join(out['changed']) + '.'}
            if parts == ['compose']:
                return self.composer().call('composer_new', {'seed': body.get('seed'), 'from_mission': body.get('from_mission') or None}, True)
            if len(parts) == 3 and parts[0] == 'compose':
                c, sid_ = self.composer(), parts[1]
                if parts[2] == 'message':
                    return c.call('composer_message', {'id': sid_, 'text': body.get('text')}, True)
                if parts[2] == 'preview':
                    return c.call('composer_preview', {'id': sid_}, True)
                if parts[2] == 'save':
                    out = c.call('composer_save', {'id': sid_, 'title': body.get('title')}, True)
                    try:
                        self.broker.provider.telegram('Packet %s saved from the Packet Composer as a DRAFT (revision %s, %s). Open it on the dashboard to review and activate.' % (
                            out['mission'], out['revision'], out.get('kind') or 'administrative'))
                    except Exception:
                        pass
                    return {**out, 'message': 'Saved as a draft packet.'}
                if parts[2] == 'discard':
                    return {**c.call('composer_discard', {'id': sid_}, True), 'message': 'Discarded.'}
                if parts[2] == 'attach':
                    out = c.call('composer_attach', {'id': sid_, 'name': body.get('name'), 'text': body.get('text'), 'remove': bool(body.get('remove'))}, True)
                    return {**out, 'message': ('Removed %s.' % out['removed']) if body.get('remove') else ('Attached %s.' % out['attached'])}
                if parts[2] == 'model':
                    out = c.call('composer_model', {'id': sid_, 'model': body.get('model')}, True)
                    return {**out, 'message': 'This session now uses %s (%s).' % (out['model_label'], out['why'])}
            if parts == ['health', 'visit']:
                out = self.broker.health.call('health_visit_note', {'effect': body.get('effect'), 'questions': body.get('questions') or '', 'bring': body.get('bring') or ''}, True)
                return {**out, 'message': 'Visit notes saved.'}
            if parts == ['devices', 'revoke']:
                return {'revoked': self.sessions.revoke(body.get('id')), 'message': 'Device signed out.'}
            raise ValueError('Not found.')
        raise ValueError('Not found.')

    def composer(self):
        c = getattr(self.broker, 'composer', None)
        if c is None:
            raise ValueError('The Packet Composer is not running on this server.')
        return c

    def page_compose_list(self):
        rows = self.composer().list()['sessions']
        label = {'draft': 'in progress', 'saved': 'saved', 'discarded': 'discarded'}
        table = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="num">%s</td></tr>' % (
            ('<a href="%s/compose/%s">%s</a>' % (ROOT, esc(r['id']), esc(r['title'] or 'Untitled'))) if r['status'] == 'draft' else esc(r['title'] or 'Untitled'),
            esc(r['kind'] or '—'), A.chip(label.get(r['status'], r['status'])),
            ('<a href="%s/packets/%s">%s</a>' % (ROOT, esc(r['mission']), esc(r['mission']))) if r['mission'] else '—', A.fmt_usd(r['cost_usd'])) for r in rows)
        return ('<div class="grid"><section class="card wide"><h2>New packet</h2><p class="small">Describe what you need done in your own words: the task, any dates, '
                'and what "finished" looks like. The Composer asks for what is missing, builds the packet as you talk and checks it. Saving creates a <b>draft</b> packet; '
                'nothing runs until you activate it. Never type passwords or codes here.</p>'
                '<textarea id="c-seed" rows="4" maxlength="4000" style="width:100%%" placeholder="e.g. Report my Nov 20 absence on Leaveco and keep the confirmation number"></textarea>'
                '<div class="actions"><button id="c-new" class="primary">Start</button></div><div id="c-msg" class="msg" hidden></div></section>'
                '<section class="card wide"><h2>Sessions</h2>%s</section></div>') % (
            ('<table><tr><th>Packet</th><th>Kind</th><th>Status</th><th>Saved as</th><th class="num">Model cost</th></tr>%s</table>' % table) if rows else '<p class="empty">No sessions yet.</p>')

    def page_compose(self, sid):
        g = self.composer().get(sid)
        if g['status'] != 'draft':
            return '<section class="card"><p>This session is %s.%s</p><p><a href="%s/compose">Back to the Composer</a></p></section>' % (
                esc(g['status']), (' It was saved as <a href="%s/packets/%s">%s</a>.' % (ROOT, esc(g['mission']), esc(g['mission']))) if g.get('mission') else '', ROOT)
        return ('<div id="compose" data-id="%s" class="compose">'
                '<section class="card"><h2>Conversation</h2><div id="c-chat" class="chat"></div>'
                '<form id="c-form"><textarea id="c-text" rows="3" maxlength="12000" style="width:100%%" placeholder="Your answer (never passwords or codes)"></textarea>'
                '<div class="actions"><button id="c-send" class="primary">Send</button><span id="c-busy" class="sub" hidden>Working on it…</span></div></form></section>'
                '<section class="card"><h2>Draft packet <span id="c-ready" class="chip"></span></h2><div id="c-checks"></div>'
                '<details id="c-diff-box" hidden open><summary>Changes from the current revision</summary><pre id="c-diff"></pre></details>'
                '<details open><summary>Packet file</summary><pre id="c-file"></pre></details>'
                '<div class="small"><b>Attached files</b> <span class="sub">(.md or .txt; they become packet files the work sessions read; never passwords or codes)</span>'
                '<ul id="c-files" class="plain small"></ul><input id="c-attach" type="file" multiple accept=".md,.txt,text/markdown,text/plain"></div>'
                '<div class="actions"><button id="c-preview">Preview the plan</button><button id="c-save" class="primary" disabled>Save as draft packet</button>'
                '<button id="c-discard" class="danger">Discard</button></div><div id="c-why" class="msg warn" hidden></div><pre id="c-plan" hidden></pre>'
                '<div class="sub" id="c-cost"></div>'
                '<label class="small">Model for this session <select id="c-model"><option value="default">your Composer default</option>'
                '<option value="sonnet">Claude Sonnet 5.5</option><option value="opus">Claude Opus 5.5</option><option value="sol">GPT-6.1 Sol</option>'
                '<option value="terra">GPT-6.1 Terra</option><option value="main">same as the chat (/ai)</option></select></label> <span class="sub" id="c-model-now"></span>'
                '</section></div>') % esc(sid)

    def usage_csv(self, days=30):
        u = self.usage(days)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['day', 'model_replies', 'tokens_in', 'tokens_cached', 'tokens_out', 'cost_usd', 'build_cost_usd', 'chat_cost', 'missions_cost', 'phone_cost', 'other_cost', 'model'])
        for r in u['days']:
            by = r['by']
            w.writerow([r['day'], int(r['calls']), int(r['input'] + r['cacheWrite']), int(r['cacheRead']), int(r['output']), '%.4f' % r['cost'], '%.4f' % r['builds'],
                        '%.4f' % by.get('chat', {}).get('cost', 0), '%.4f' % by.get('missions', {}).get('cost', 0), '%.4f' % by.get('phone', {}).get('cost', 0), '%.4f' % by.get('other', {}).get('cost', 0), r.get('model') or ''])
        return buf.getvalue()

    # ---------- pages ----------
    def page(self, tab, body, nonce, csrf, title='PROCURATOR'):
        nav = ''.join('<a href="%s/%s" class="%s">%s</a>' % (ROOT, slug, 'on' if slug == tab else '', esc(label)) for slug, label in TABS)
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                '<meta name="referrer" content="no-referrer"><meta name="csrf" content="%s"><title>%s</title><style nonce="%s">%s</style></head><body>'
                '<header><div class="bar"><h1>PROCURATOR <span>%s</span></h1><a href="%s/logout" class="small" style="margin-left:auto">Sign out</a></div><nav>%s</nav></header>'
                '<main><div id="msg" class="msg" hidden></div>%s</main><footer>Private page · nothing here can approve a call, email, purchase, build or submission; those stay in Telegram.</footer>'
                '<script nonce="%s">%s</script></body></html>') % (esc(csrf), esc(title), nonce, A.CSS, esc(when(self.now())), ROOT, nav, body, nonce, A.JS)

    def page_overview(self):
        o = self.overview()
        u, t = o['usage'], o['usage']['today']
        tok = lambda x: A.fmt_k((x.get('input') or 0) + (x.get('cacheRead') or 0) + (x.get('cacheWrite') or 0))
        model = o['model']
        db_ = o['daily_budget']
        paused = o['paused_all']
        parts = ['<div class="grid">']
        parts.append('<section class="card"><h2>Model</h2><div class="big">%s</div><div class="sub">fallback: %s · change with /ai in Telegram</div>%s</section>' % (
            esc(model['label']), esc(', '.join(model['fallbacks']) or 'none'),
            ('<div class="msg warn">Packet work sessions are paused: %s.</div>' % esc(paused)) if paused else ''))
        parts.append('<section class="card"><h2>Today</h2><div class="row"><div><div class="big" data-key="usage.today.cost" data-fmt="usd">%s</div><div class="sub">cost</div></div>'
                     '<div><div class="big" data-key="usage.today.calls">%d</div><div class="sub">model replies</div></div><div><div class="big">%s</div><div class="sub">tokens in</div></div>'
                     '<div><div class="big">%s</div><div class="sub">tokens out</div></div></div>%s%s</section>' % (
            A.fmt_usd(t.get('cost')), int(t.get('calls') or 0), tok(t), A.fmt_k(t.get('output')),
            ('<div style="margin-top:8px"><div class="sub">daily budget %s of %s</div>%s</div>' % (A.fmt_usd(db_['spent_today']), A.fmt_usd(db_['daily_budget_usd']), A.bar(db_['fraction']))) if db_['daily_budget_usd'] else '',
            '' if u['recorded'] else '<p class="empty">No usage recorded yet (the meter counts from the first reply after this deploy).</p>'))
        parts.append('<section class="card"><h2>Last %d days</h2><div class="row"><div><div class="big">%s</div><div class="sub">cost%s</div></div><div><div class="big">%s</div><div class="sub">tokens in</div></div><div><div class="big">%s</div><div class="sub">tokens out</div></div></div>'
                     '<table style="margin-top:8px"><tr><th>Source</th><th class="num">Replies</th><th class="num">Tokens</th><th class="num">Cost</th></tr>%s</table></section>' % (
            u['days'], A.fmt_usd(u['period'].get('cost')), (' + %s builds' % A.fmt_usd(u['build_cost'])) if u['build_cost'] else '', tok(u['period']), A.fmt_k(u['period'].get('output')),
            ''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td></tr>' % (esc({'chat': 'Your chat', 'missions': 'Packet work', 'phone': 'Phone calls', 'composer': 'Packet Composer', 'other': 'Other'}.get(s, s)), int(v['calls']), A.fmt_k(v['input'] + v['cacheRead'] + v['cacheWrite'] + v['output']), A.fmt_usd(v['cost']))
                    for s, v in sorted(u['by'].items(), key=lambda kv: -kv[1]['cost'])) or '<tr><td colspan="4" class="empty">nothing yet</td></tr>'))
        b = o['builders']
        parts.append('<section class="card"><h2>Builders</h2><div class="big">%d / %d online</div><ul class="plain">%s</ul></section>' % (
            b['online'], b['total'], ''.join('<li>%s <b>%s</b> <span class="sub">%s · %s · %s</span></li>' % (A.chip('online' if x['online'] else 'offline'), esc(x['label']), esc(x['last_seen']), esc(', '.join(x['agents']) or 'no agents'), ('building ' + esc(x['busy'][:8])) if x['busy'] else 'idle') for x in b['list']) or '<li class="empty">none set up yet</li>'))
        ap = o['approvals']
        parts.append('<section class="card wide"><h2>Waiting for you (%d)</h2>%s</section>' % (len(ap), ('<ul class="plain">' + ''.join('<li><b>%s</b><div class="sub">%s</div></li>' % (esc(x.get('what')), esc(x.get('how'))) for x in ap) + '</ul>') if ap else '<p class="empty">Nothing is waiting for an approval.</p>'))
        pk = o['packets']['list']
        parts.append('<section class="card wide"><h2>Packets (%d active of %d)</h2>%s<div class="actions"><a class="btn" href="%s/packets">All packets and upload</a></div></section>' % (
            o['packets']['active'], o['packets']['total'],
            ('<table><tr><th>Packet</th><th>Kind</th><th>Status</th><th>Next session</th><th class="num">Spent / budget</th></tr>' + ''.join(
                '<tr><td><a href="%s/packets/%s">%s</a><div class="sub">%s</div></td><td>%s</td><td>%s</td><td>%s</td><td class="num">%s%s</td></tr>' % (
                    ROOT, esc(m['mission']), esc(m['mission']), esc(m['title']), esc(m['kind']), A.chip(m['status']), esc(m.get('next_tick') or '—'), A.fmt_usd(m['spent_usd']), (' / ' + A.fmt_usd(m['budget_usd'])) if m.get('budget_usd') else '') for m in pk) + '</table>') if pk else '<p class="empty">No active packets.</p>', ROOT))
        parts.append('<section class="card wide"><h2>Recent activity</h2>%s<div class="actions"><a class="btn" href="%s/activity">Full log</a></div></section>' % (self.activity_table(o['activity']), ROOT))
        parts.append('</div>')
        return ''.join(parts)

    def activity_table(self, entries):
        if not entries:
            return '<p class="empty">Nothing recorded yet.</p>'
        return '<table><tr><th>When</th><th>What</th><th>Status</th></tr>%s</table>' % ''.join(
            '<tr><td class="small">%s</td><td>%s<div class="sub">%s%s</div></td><td>%s</td></tr>' % (esc(e.get('when')), esc(e.get('title')), esc(e.get('kind', '').replace('_', ' ')), (' · packet ' + esc(e['mission'])) if e.get('mission') else '', A.chip(e.get('status'))) for e in entries)

    def page_packets(self):
        ms = self.missions()
        rows = ''.join('<tr><td><a href="%s/packets/%s">%s</a><div class="sub">%s · %s</div></td><td>%s</td><td>%s</td><td>%s</td><td class="num">%s%s</td></tr>' % (
            ROOT, esc(m['mission']), esc(m['mission']), esc(m['title']), esc(m['revision']), esc(m['kind']), A.chip(m['status']), esc(m.get('next_tick') or '—'), A.fmt_usd(m['spent_usd']), (' / ' + A.fmt_usd(m['budget_usd'])) if m.get('budget_usd') else '') for m in ms)
        library = ('<table><tr><th>Packet</th><th>Kind</th><th>Status</th><th>Next session</th><th class="num">Spent / budget</th></tr>%s</table>' % rows) if ms else '<p class="empty">No packets yet. Upload the first one below.</p>'
        templates = ''.join('<li><a href="%s/templates/%s" download="%s-packet.md">%s template</a></li>' % (ROOT, k, k, esc(k)) for k in packets.KINDS)
        upload = ('<section class="card wide"><h2>Upload a packet</h2><p class="small">Pick the packet\'s .md files (up to 25). The first file should start with <code>**Revision:**</code>, <code>**Kind:**</code>, <code>**Goal:**</code> and <code>**Done when:**</code> lines; '
                  'then Facts, Steps (<code>1. type: browse: …</code>) or a journals table. Files are checked here before anything is stored, and anything that looks like a password, code, key, card or Social Security number is refused.</p>'
                  '<input id="packet-files" type="file" multiple accept=".md,.txt,text/markdown,text/plain"><ul id="packet-list" class="small"></ul><div id="packet-msg" class="msg" hidden></div><div id="packet-report"></div>'
                  '<div class="actions"><button id="packet-save" class="primary" disabled>Save as ready</button><button id="packet-activate-save" disabled>Save and activate</button></div>'
                  '<details style="margin-top:10px"><summary>Templates for the five kinds</summary><ul class="small">%s</ul><p class="small">Step types: %s. Kinds: %s.</p></details></section>' % (templates, esc(', '.join(packets.STEP_TYPES)), esc(', '.join(packets.KINDS))))
        return '<div class="grid"><section class="card wide"><h2>Library</h2>%s</section>%s</div>' % (library, upload)

    def page_packet(self, slug):
        m = self.mission(slug)
        st = m['status']
        acts = []
        if st in ('draft', 'ready'):
            acts.append(('activate', 'Activate', 'primary', ''))
        if st == 'active':
            acts.append(('pause', 'Pause', '', ''))
        if st in ('paused', 'blocked', 'over_budget', 'done'):
            acts.append(('resume', 'Resume', 'primary', ''))
        if st != 'closed':
            acts.append(('close', 'Close', 'danger', 'Close this packet? Its records stay readable but no more work sessions run.'))
        acts.append(('__dryrun', 'Dry run to Telegram', '', ''))
        revise = '<button data-compose-from="%s">Revise with the Composer</button>' % esc(slug) if st != 'closed' else ''
        buttons = revise + ''.join(('<button data-post="/packets/%s/dryrun" data-body="{}" data-reload="no">%s</button>' % (esc(slug), label)) if a == '__dryrun' else
                          '<button class="%s" data-post="/packets/%s/state" data-body=\'{"action":"%s"}\' %s>%s</button>' % (cls, esc(slug), a, ('data-confirm="%s"' % esc(c)) if c else '', label) for a, label, cls, c in acts)
        if st == 'done' and m.get('done_proposal'):
            buttons = ('<div class="msg ok" style="width:100%%">The assistant reports every done-when item has evidence: %s</div>' % esc('; '.join('%d: %s' % (x['item'], ', '.join(x['refs'])) for x in m['done_proposal']['items']))) + buttons
        if m.get('kind') == 'coding' and (self.state / 'procurator-build' / 'packets' / (slug + '.md')).exists():
            # Spec interview (0.32.0): the Composer saved this packet's BUILD.md for the builder; the owner starts it from Telegram.
            buttons = ('<div class="msg ok" style="width:100%%">This packet carries a BUILD.md. Start the build (BUILD REVIEW, then approval) from Telegram: <span class="tg">/buildfrom %s</span></div>' % esc(slug)) + buttons
        spend = ('<div class="sub">spent %s of %s</div>%s' % (A.fmt_usd(m['spent_usd']), A.fmt_usd(m['budget_usd']), A.bar((m['spent_usd'] or 0) / m['budget_usd']))) if m.get('budget_usd') else '<div class="sub">spent %s · no budget</div>' % A.fmt_usd(m['spent_usd'])
        head = ('<section class="card wide"><div class="row"><h2 style="margin:0">%s</h2>%s<span class="chip">%s</span></div><div class="big" style="font-size:20px;margin-top:4px">%s</div><div class="sub">%s</div>'
                '<p style="margin:8px 0 4px"><b>Goal:</b> %s</p>%s%s<div class="actions">%s</div>%s</section>') % (
            esc(slug), A.chip(st), esc(m['kind']), esc(m['title']), esc('revision %s · cadence %d min · hours %02d:00–%02d:00 · model %s · next session %s' % (
                m['revision'], m['cadence_minutes'], m['hours'][0], m['hours'][1], m.get('model') or 'main', m.get('next_tick') or '—')),
            esc(m.get('goal') or '—'), ('<p class="msg warn" style="margin:4px 0">%s</p>' % esc(m['paused_reason'])) if m.get('paused_reason') else '',
            ('<div style="margin:6px 0"><b class="small">Done when</b><ul class="small" style="margin:2px 0 0 18px;padding:0">%s</ul></div>' % ''.join('<li>%s</li>' % esc(d) for d in m['done_when'])) if m.get('done_when') else '',
            buttons, spend)
        warn = ('<section class="card wide"><h2>Validator notes</h2><ul class="small">%s</ul></section>' % ''.join('<li>%s</li>' % esc(w) for w in m['warnings'])) if m.get('warnings') else ''
        q = m.get('open_questions') or []
        questions = '<section class="card wide"><h2>Open questions (%d)</h2>%s</section>' % (len(q), ''.join(
            '<form class="inline" data-post="/packets/%s/answer" data-msg="#msg"><input type="hidden" name="question_id" value="%s"><div style="flex:1 1 100%%"><b>%s</b> <span class="sub">%s · asked %s</span></div><input type="text" name="answer" maxlength="1200" placeholder="Your answer" style="flex:1 1 260px"><button class="primary">Answer</button></form>' % (
                esc(slug), esc(x['id']), esc(x['question']), esc(x['id']), esc(x['asked'])) for x in q) if q else '<p class="empty">Nothing waiting for an answer.</p>')
        threads = []
        for t in m.get('threads') or []:
            steps = ''.join('<li>%s<div>%s <span class="chip">%s</span>%s%s%s</div></li>' % (A.chip(s['status']), esc(s['title']), esc(s.get('type') or 'work'),
                                                                                           ('<div class="evid"><b>%s</b></div>' % esc(s['awaiting_line'])) if s.get('awaiting_line') else '',
                                                                                           ('<div class="evid">evidence: %s</div>' % esc('; '.join(s['evidence']))) if s.get('evidence') else '',
                                                                                           ('<div class="evid">%s</div>' % esc(s['note'])) if s.get('note') else '') for s in t['steps'])
            threads.append('<section class="card"><h2>%s</h2><div class="sub">priority %d%s</div><ul class="plain steps">%s</ul></section>' % (esc(t['title']), t['priority'], (' · after ' + esc(', '.join(t['depends_on']))) if t.get('depends_on') else '', steps))
        dl = m.get('deadlines') or []
        deadlines = '<section class="card"><h2>Deadlines</h2>%s</section>' % (('<ul class="plain">' + ''.join('<li>%s %s <span class="sub">%s%s</span></li>' % (A.chip(d['status']), esc(d['title']), esc(d.get('due') or 'no date'), (' · trigger ' + esc(d['trigger_status'])) if d.get('trigger_status') else '') for d in dl) + '</ul>') if dl else '<p class="empty">None recorded.</p>')
        files = '<section class="card"><h2>Files (reading order)</h2><ol class="small">%s</ol><div class="sub">facts: %s</div></section>' % (
            ''.join('<li>%s <span class="sub">%sk chars</span></li>' % (esc(f['name']), round(f['chars'] / 1000, 1)) for f in m.get('packet_files') or []),
            esc(', '.join('%s %d' % (k.replace('_', ' '), v) for k, v in (m.get('facts_by_status') or {}).items()) or 'none yet'))
        ev = m.get('recent_events') or []
        events = '<section class="card wide"><h2>Recent events</h2>%s</section>' % (('<table><tr><th>When</th><th>Event</th><th>Outcome</th></tr>' + ''.join('<tr><td class="small">%s</td><td>%s<div class="sub">%s%s%s</div></td><td>%s</td></tr>' % (
            esc(e['at']), esc(e['summary']), esc(e['kind']), (' · ' + esc(e['thread'])) if e.get('thread') else '', (' · ' + esc('; '.join(e['refs']))) if e.get('refs') else '', A.chip(e['outcome'])) for e in ev) + '</table>') if ev else '<p class="empty">No events yet.</p>')
        models = ''.join('<option value="%s" %s>%s</option>' % (v, 'selected' if (m.get('model') or '') == v else '', l) for v, l in (('', 'main model (/ai)'), ('sonnet', 'Claude Sonnet 5.5'), ('opus', 'Claude Opus 5.5'), ('sol', 'GPT-6.1 Sol'), ('terra', 'GPT-6.1 Terra')))
        settings = ('<section class="card wide"><h2>Packet settings</h2><form class="inline" data-post="/packets/%s/settings" data-msg="#msg">'
                    '<div><label>Cadence (minutes)</label><input type="number" name="cadence" min="60" max="1440" value="%d" style="width:110px"></div>'
                    '<div><label>Hours (ET)</label><input type="text" name="hours" value="%02d:00-%02d:00" pattern="\\d{2}:00-\\d{2}:00" style="width:130px"></div>'
                    '<div><label>Budget ($, blank = none)</label><input type="text" name="budget" value="%s" style="width:110px"></div>'
                    '<div><label>Model for this packet</label><select name="model">%s</select></div><button class="primary">Save</button></form>'
                    '<p class="sub">Telegram equivalents: <span class="tg">/mission cadence %s MINUTES</span> · <span class="tg">/mission pause %s</span> · <span class="tg">/mission resume %s</span></p></section>') % (
            esc(slug), m['cadence_minutes'], m['hours'][0], m['hours'][1], ('%g' % m['budget_usd']) if m.get('budget_usd') else '', models, esc(slug), esc(slug), esc(slug))
        return '<div class="grid">%s%s%s%s%s%s%s%s</div>' % (head, warn, questions, ''.join(threads), deadlines, files, events, settings)

    def page_builds(self):
        bs = self.builds()
        if not bs:
            return '<section class="card"><h2>Builds</h2><p class="empty">No builds yet. Ask in Telegram, e.g. "In procurator-sandbox, use Codex to …", then /buildapprove.</p></section>'
        rows = []
        for b in bs:
            rows.append('<tr><td><a href="%s/builds/%s">%s</a><div class="sub">%s%s</div></td><td>%s<div class="sub">%s · %s</div></td><td>%s</td><td class="small">%s</td><td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>' % (
                ROOT, esc(b['id']), esc(b['id'][:8]), esc(b['title']), (' · packet %s step %s' % (esc(b['mission']), esc(b['step']))) if b['mission'] else '',
                esc(b['repo']), esc(b['agent'] or '?'), esc(b['builder'] or 'any'), A.chip(b['status']), esc(when(b['updated'] or b['created'])),
                ('%d s' % b['duration_s']) if b['duration_s'] is not None else '—', A.fmt_usd(b['cost_usd']) if b['cost_usd'] is not None else '—',
                ('<a href="%s">PR</a>' % esc(b['pr_url'])) if b['pr_url'] else (esc(b['tests']['status']) if b['tests'] else '—')))
        return '<section class="card"><h2>Builds (%d)</h2><table><tr><th>Build</th><th>Repo</th><th>Status</th><th>Updated</th><th class="num">Time</th><th class="num">Cost</th><th>Result</th></tr>%s</table><p class="sub">Cancel a running build with <span class="tg">/buildcancel ID</span> in Telegram; re-request a failed one by asking again (it goes to /buildapprove).</p></section>' % (len(bs), ''.join(rows))

    def page_build(self, bid):
        b = next((x for x in self.builds() if x['id'] == bid), None)
        if not b:
            return '<section class="card"><p class="empty">No build with that id.</p></section>'
        live = ''
        if b['question']:
            live += '<div class="msg warn" style="margin-top:12px"><b>Waiting for you:</b> %s<div class="sub">Answer in Telegram: <span class="tg">/buildnote %s your answer</span></div></div>' % (esc(b['question']), esc(b['id']))
        v = b.get('verification')
        if v:
            mark = {'passed': 'ok', 'failed': 'bad', 'timed_out': 'warn', 'missing': 'warn'}
            checks = ''.join('<tr><td>%d</td><td>%s</td><td>%s</td><td class="small">%s</td></tr>' % (c['n'], esc(c['text']), A.chip(c['status'], mark.get(c['status'], '')), esc(c['output'][-200:])) for c in v['checks'])
            rv = v['review']
            review = ('<p><b>%s</b> by %s%s. %s</p>%s' % (esc(rv['verdict'].replace('_', ' ')), esc({'claude': 'Claude Code', 'codex': 'Codex'}.get(rv['agent'], 'reviewer')), (' (' + esc(rv['model']) + ')') if rv['model'] else '', esc(rv['summary']),
                      ('<ul>%s</ul>' % ''.join('<li><b>%s</b> %s%s</li>' % (esc(i['severity']), ('<code>%s</code> — ' % esc(i['file'])) if i['file'] else '', esc(i['text'])) for i in rv['issues'])) if rv['issues'] else '')) if rv else '<p class="sub">Review was off for this build.</p>'
            live += ('<h2 style="margin-top:12px">Verification %s</h2>%s'
                     '<h3>Acceptance checks</h3>%s<h3>Independent review</h3>%s<p class="sub">Screenshots in the PR: %d%s%s</p>') % (
                A.chip('all gates passed' if v['passed'] else 'open items', 'ok' if v['passed'] else 'warn'),
                ('<p class="sub">%d fix round(s) were sent back to the agent.</p>' % v['rounds']) if v['rounds'] else '',
                ('<table><tr><th>#</th><th>Item</th><th>Check</th><th>Output</th></tr>%s</table>' % checks) if checks else '<p class="sub">No acceptance checks (the packet had no Done-when items).</p>',
                review, v['screenshots'], (' · ' + esc(v['screenshot_note'])) if v['screenshot_note'] else '', (' · unresolved: ' + esc(v['unresolved'].lower())) if v['unresolved'] else '')
        if b['progress']:
            icon = {'tool': '&#9656;', 'say': '', 'question': '&#10067;', 'note': 'You: ', 'result': '&#10004; ', 'info': '&#8505; '}
            rows_ = ''.join('<div class="pl"><span class="sub">%s</span> %s%s%s</div>' % (esc(when(e["at"])) if e["at"] else "", icon.get(e['t'], ''), ('<b>%s</b> ' % esc(e['name'])) if e['t'] == 'tool' and e['name'] else '', esc(e['text'])) for e in b['progress'])
            live += '<h2 style="margin-top:12px">Live log</h2><div class="plog">%s</div><p class="sub">Steer it from Telegram: <span class="tg">/buildnote %s what to change</span> · this page refreshes when you reload it.</p>' % (rows_, esc(b['id']))
        return ('<section class="card"><h2>Build %s %s</h2><div class="kv"><b>Title</b><span>%s</span><b>Repo</b><span>%s</span><b>Agent</b><span>%s%s on %s</span><b>Packet</b><span>%s</span><b>Created</b><span>%s</span><b>Updated</b><span>%s</span>'
                '<b>Duration</b><span>%s</span><b>Cost</b><span>%s</span><b>Tests</b><span>%s</span><b>PR</b><span>%s</span><b>Files</b><span>%s</span><b>Message</b><span>%s</span></div>'
                '%s<h2 style="margin-top:12px">Agent summary</h2><pre>%s</pre><h2>Test output (tail)</h2><pre>%s</pre><p class="sub">Cancel: <span class="tg">/buildcancel %s</span></p></section>') % (
            esc(b['id'][:8]), A.chip(b['status']), esc(b['title']), esc(b['repo']), esc(b['agent'] or '?'), (' (' + esc(b['model']) + ')') if b['model'] else '', esc(b['builder'] or 'any'), (esc(b['mission']) + ' step ' + esc(b['step'])) if b['mission'] else '—',
            esc(when(b['created'])), esc(when(b['updated'])), ('%d s' % b['duration_s']) if b['duration_s'] is not None else '—', A.fmt_usd(b['cost_usd']) if b['cost_usd'] is not None else '—',
            (esc(b['tests']['status']) + ' · ' + esc(b['tests']['command'])) if b['tests'] else '—', ('<a href="%s">%s</a>' % (esc(b['pr_url']), esc(b['pr_url']))) if b['pr_url'] else '—',
            esc(b['files'] if b['files'] is not None else '—'), esc(b['message'] or '—'), live, esc(b['summary'] or '(none)'), esc(b['log_tail'] or '(none)'), esc(b['id']))

    def page_usage(self):
        u = self.usage(14)
        days = [r['day'] for r in u['days']]
        srcs = ['chat', 'missions', 'phone', 'composer', 'other']
        names = {'chat': 'Your chat', 'missions': 'Packet work', 'phone': 'Phone calls', 'composer': 'Packet Composer', 'other': 'Other'}
        cost_series = [(names[s], [r['by'].get(s, {}).get('cost', 0) for r in u['days']]) for s in srcs] + [('Builds', [r['builds'] for r in u['days']])]
        token_series = [(names[s], [(r['by'].get(s, {}).get('input', 0) + r['by'].get(s, {}).get('output', 0) + r['by'].get(s, {}).get('cacheRead', 0) + r['by'].get(s, {}).get('cacheWrite', 0)) for r in u['days']]) for s in srcs]
        db_ = self.daily_budget_state(u)
        per_mission = ''.join('<tr><td><a href="%s/packets/%s">%s</a></td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td></tr>' % (ROOT, esc(k), esc(k), int(v['calls']), A.fmt_k(v['input'] + v['cacheRead'] + v['cacheWrite'] + v['output']), A.fmt_usd(v['cost'])) for k, v in sorted(u['by_mission'].items(), key=lambda kv: -kv[1]['cost']))
        models = ''.join('<tr><td>%s</td><td class="num">%s</td></tr>' % (esc(MODEL_LABELS.get(k, k)), A.fmt_k(v)) for k, v in sorted(u['models'].items(), key=lambda kv: -kv[1]))
        table = ''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr>' % (
            esc(r['day']), int(r['calls']), A.fmt_k(r['input'] + r['cacheWrite']), A.fmt_k(r['cacheRead']), A.fmt_k(r['output']), A.fmt_usd(r['cost']), A.fmt_usd(r['builds']) if r['builds'] else '—') for r in reversed(u['days']) if r['calls'] or r['builds'])
        return ('<div class="grid"><section class="card wide"><h2>Cost per day (USD)</h2>%s%s</section><section class="card wide"><h2>Tokens per day</h2>%s</section>'
                '<section class="card"><h2>By packet (%d days)</h2>%s</section><section class="card"><h2>By model (tokens in + out)</h2>%s</section>'
                '<section class="card wide"><h2>Daily detail</h2><table><tr><th>Day</th><th class="num">Replies</th><th class="num">In</th><th class="num">Cached</th><th class="num">Out</th><th class="num">Cost</th><th class="num">Builds</th></tr>%s</table>'
                '<div class="actions"><a class="btn" href="%s/usage.csv" download="procurator-usage.csv">Export CSV (30 days)</a></div>%s</section>%s</div>') % (
            A.stacked_bars(days, cost_series, fmt=lambda v: '$%.2f' % v), ('<div class="sub" style="margin-top:6px">daily budget %s · spent today %s</div>%s' % (A.fmt_usd(db_['daily_budget_usd']), A.fmt_usd(db_['spent_today']), A.bar(db_['fraction']))) if db_['daily_budget_usd'] else '<p class="sub">Set a daily budget in Settings to be warned at 80% and paused at 100%.</p>',
            A.stacked_bars(days, token_series), u['window'], ('<table><tr><th>Packet</th><th class="num">Replies</th><th class="num">Tokens</th><th class="num">Cost</th></tr>%s</table>' % per_mission) if per_mission else '<p class="empty">No packet work sessions recorded yet.</p>',
            ('<table>%s</table>' % models) if models else '<p class="empty">Nothing yet.</p>', table or '<tr><td colspan="7" class="empty">nothing yet</td></tr>', ROOT,
            '' if u['recorded'] else '<p class="empty">The usage meter has nothing yet. If this stays empty after a day of use, the gateway is not reporting usage to plugins; the deploy check says which.</p>',
            self.browser_week_section())

    def browser_week_section(self):
        try:
            w = self.broker.call('task_week', {'days': 7}, True)
        except Exception:
            return ''
        pct = lambda v: '—' if v is None else '%d%%' % round(v * 100)
        num = lambda v: '—' if v is None else ('%.2f' % v).rstrip('0').rstrip('.')
        rows = ''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr>' % (
            esc(x['site']), x['started'], x['completed'], pct(x['completion_without_takeover']), num(x['approvals_per_completed']),
            A.fmt_usd(x['cost_per_completed_usd']) if x['cost_per_completed_usd'] is not None else '—') for x in w['sites'])
        t = w['total']
        return ('<section class="card wide"><h2>Browser tasks, last 7 days</h2>%s<p class="sub">%s</p></section>') % (
            ('<table><tr><th>Site</th><th class="num">Started</th><th class="num">Completed</th><th class="num">Done without takeover</th><th class="num">Approvals per completed</th><th class="num">Model cost per completed</th></tr>%s'
             '<tr><td><b>All sites</b></td><td class="num">%d</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr></table>') % (
                rows, t['started'], t['completed'], pct(t['completion_without_takeover']), num(t['approvals_per_completed']),
                A.fmt_usd(t['cost_per_completed_usd']) if t['cost_per_completed_usd'] is not None else '—') if w['sites'] else '<p class="empty">No browser tasks started in the last 7 days.</p>',
            esc(w['notes']))

    def page_traces(self, scope):
        files = self.broker.fixtures.trace_files(scope)
        rows = ''.join('<tr><td><a href="%s/traces/%s/%s" download>%s</a></td><td class="num">%s KB</td><td>%s</td></tr>' % (
            ROOT, esc(scope), esc(f['name']), esc(f['name']), max(1, f['bytes'] // 1024), esc(when(f['when']))) for f in files)
        return ('<section class="card wide"><h2>Browser traces for %s</h2>%s<p class="sub">Each file is one browser step: page snapshots, screenshots and the actions taken. '
                'Open a file at trace.playwright.dev (it runs in your browser; the file is not uploaded). Kept 7 days on the server; never shown to the assistant.</p></section>') % (
            esc(scope), ('<table><tr><th>File</th><th class="num">Size</th><th>Saved</th></tr>%s</table>' % rows) if rows else '<p class="empty">No traces kept for this task.</p>')

    def page_machines(self):
        v = self.vitals.latest() if self.vitals else None
        series = self.vitals.series() if self.vitals else []
        if v:
            mem_f = (v['mem_used'] / v['mem_total']) if v.get('mem_total') else None
            disk_f = (1 - v['disk_free'] / v['disk_total']) if v.get('disk_total') else None
            drop = ('<div class="row"><div>%s<div class="sub right">CPU</div></div><div>%s<div class="sub right">memory</div></div><div>%s<div class="sub right">disk</div></div></div>'
                    '<div class="kv" style="margin-top:8px"><b>Memory</b><span>%s of %s</span><b>Gateway container</b><span>%s</span><b>Disk free</b><span>%s of %s</span><b>Load (1 min)</b><span>%s</span><b>Uptime</b><span>%s</span><b>Sampled</b><span>%s</span></div>'
                    '<h2 style="margin-top:12px">Last 24 hours</h2>%s') % (
                A.donut((v['cpu'] or 0) / 100, 'CPU'), A.donut(mem_f, 'memory'), A.donut(disk_f, 'disk'), A.fmt_bytes(v['mem_used']), A.fmt_bytes(v['mem_total']), A.fmt_bytes(v['gw_mem']), A.fmt_bytes(v['disk_free']), A.fmt_bytes(v['disk_total']),
                esc(v['load1'] if v['load1'] is not None else '—'), esc('%.1f days' % (v['uptime'] / 86400) if v.get('uptime') else '—'), esc(when(v['t'])),
                A.line_chart(series, ['cpu', 'mem_used', 'gw_mem'], labels=['CPU %', 'memory used', 'gateway memory'], maxima={'cpu': 100, 'mem_used': v.get('mem_total') or None, 'gw_mem': v.get('mem_total') or None}))
        else:
            drop = '<p class="empty">No samples yet (the sampler records one row a minute after the deploy).</p>'
        cards = ['<section class="card wide"><h2>Droplet</h2>%s</section>' % drop]
        for b in self.builders():
            vt = b['vitals'] or {}
            mem = ('%s of %s' % (A.fmt_bytes(vt['mem_used']), A.fmt_bytes(vt['mem_total']))) if vt.get('mem_total') else '—'
            disk = ('%s free of %s' % (A.fmt_bytes(vt['disk_free']), A.fmt_bytes(vt['disk_total']))) if vt.get('disk_total') else ('%s free' % A.fmt_bytes(vt['disk_free'])) if vt.get('disk_free') else '—'
            cards.append('<section class="card"><h2>%s %s</h2><div class="kv"><b>Last seen</b><span>%s</span><b>Worker</b><span>%s%s</span><b>Load</b><span>%s%s</span><b>Memory</b><span>%s</span><b>Disk</b><span>%s</span><b>Uptime</b><span>%s</span><b>Agents</b><span>%s</span><b>Repos</b><span>%s</span><b>Running build</b><span>%s</span></div>%s</section>' % (
                esc(b['label']), A.chip('online' if b['online'] else 'offline'), esc(b['last_seen']), esc(b['version'] or '?'), (' · ' + esc(vt['os'])) if vt.get('os') else '', esc(vt.get('load1', '—')), (' on %s CPUs' % vt['cpus']) if vt.get('cpus') else '',
                mem, disk, esc('%.1f days' % (vt['uptime'] / 86400) if vt.get('uptime') else '—'), esc(', '.join(b['agents']) or 'none'), esc(', '.join(b['repos']) or 'none allowed yet'), esc(b['busy'] or 'idle'),
                '' if vt else '<p class="sub">Vitals arrive once this builder runs worker 1.3.0 (re-run the installer / bin/setup-builder.sh).</p>'))
        # Home browser on the PC (0.19.0): online state, Chrome, site profiles signed in at the PC.
        try:
            for m in self.broker.lanes.machines():
                v = m.get('vitals') or {}
                profiles = ', '.join(esc(p['name']) + (' ✓' if p['ready'] else '') for p in m.get('profiles', [])) or 'none yet'
                cards.append('<section class="card"><h2>Home browser · %s %s</h2><div class="kv"><b>Last seen</b><span>%s</span><b>Chrome</b><span>%s (%s)</span><b>Worker</b><span>%s</span><b>Open browser</b><span>%s</span><b>Load</b><span>%s</span><b>Site profiles</b><span>%s</span></div></section>' % (
                    esc(m.get('label') or m['name']), A.chip('online' if m['online'] else 'offline'), esc('%d min ago' % (m['last_seen_s'] // 60)),
                    esc(m.get('chrome') or '?'), 'windows visible' if m.get('headed') else 'hidden', esc(m.get('version') or '?'),
                    esc(', '.join(m.get('sessions') or []) or 'none'), esc(str(v.get('load1', '—'))), profiles))
        except Exception:
            pass
        return '<div class="grid">%s</div>' % ''.join(cards)

    def page_activity(self, query):
        days = int(query.get('days', ['7'])[0]) if query.get('days', ['7'])[0].isdigit() else 7
        kind = query.get('kind', [''])[0] or None
        kinds = ['', 'call', 'email', 'email_trash', 'browser_task', 'browser_action', 'booking', 'purchase', 'calendar', 'task_list', 'signin', 'mission', 'report', 'other']
        filters = '<form method="get" class="inline"><div><label>Days</label><select name="days">%s</select></div><div><label>Kind</label><select name="kind">%s</select></div><button>Show</button></form>' % (
            ''.join('<option value="%d" %s>%d</option>' % (d, 'selected' if d == days else '', d) for d in (1, 3, 7, 14, 30, 90)),
            ''.join('<option value="%s" %s>%s</option>' % (k, 'selected' if (kind or '') == k else '', k.replace('_', ' ') or 'all') for k in kinds))
        return '<section class="card"><h2>Activity</h2>%s<div style="margin-top:10px">%s</div></section>' % (filters, self.activity_table(self.activity(max(1, min(400, days)), kind, 300)))

    def page_health(self):
        """0.25.0 (H1, F1, F2): the health module's own tab. Health details live here and in private receipts, never in Telegram or memory."""
        h = self.broker.health
        if h.policy().get('adult_confirmed') != 'yes':
            return '<section class="card"><h2>Health</h2><p>The health module is off. If you are 18 or older, send <span class="tg">/healthadult yes</span> in Telegram to turn it on.</p></section>'
        st, dg = h.call('health_status', {}, True), h.call('health_digest', {}, True)
        visits = h.call('health_visit_packet', {}, True)['visits']
        reads = h.call('health_read_allow', {}, True)['scopes']
        def visit(v):
            return ('<section class="card wide"><h2>%s %s · %s</h2><div class="kv"><b>Where</b><span>%s</span><b>With</b><span>%s</span><b>Visit</b><span>%s</span><b>Status</b><span>%s%s</span></div>'
                    '<h3>Bring</h3><ul class="plain">%s</ul><h3>Your questions</h3><ul class="plain">%s</ul>'
                    '<form class="stack" data-post="/health/visit" data-msg="#msg"><input type="hidden" name="effect" value="%s"><label>Questions (one per line)</label><textarea name="questions" rows="3">%s</textarea>'
                    '<label>Bring (one per line)</label><textarea name="bring" rows="3">%s</textarea><button class="primary">Save visit notes</button></form><p class="sub">%s</p></section>') % (
                esc(v['date']), esc(v.get('time') or ''), esc(v.get('institution') or ''), esc(v.get('location') or '—'), esc(v.get('clinician') or '—'),
                esc(v.get('visit_type') or '—') + (' (telehealth)' if v['telehealth'] else ''), A.chip(v['state']), (' · confirmation ' + esc(v['confirmation'])) if v.get('confirmation') else '',
                ''.join('<li>%s</li>' % esc(x) for x in v['bring']), ''.join('<li>%s</li>' % esc(x) for x in v['questions']) or '<li class="empty">none yet</li>',
                esc(v['effect']), esc('\n'.join(v['questions'])), esc('\n'.join(v['bring'])), esc(v['note']))
        grants = ''.join('<li><b>%s</b> <span class="sub">%s · until %s</span></li>' % (esc(g['title']), esc(', '.join(g['actions'])), esc(time.strftime('%Y-%m-%d', time.localtime(g['valid_to'])))) for g in st.get('grants', [])) or '<li class="empty">No active grants.</li>'
        conns = ''.join('<li><b>%s</b> %s <span class="sub">%s · %s%s</span></li>' % (esc(c['institution']), A.chip(c['status']), esc(c['provider']), esc(c['environment']),
                        (' · may receive health details: ' + esc(', '.join(c.get('contacts') or []))) if c.get('contacts') else '') for c in st.get('connections', [])) or '<li class="empty">No connections.</li>'
        coach = self.coach_card()
        return coach + ('<div class="grid"><section class="card"><h2>Needs you</h2><div class="big">%d</div><div class="sub">%d appointment(s) in the next 7 days · %d waiting on you · %d refill(s) in progress</div></section>'
                '<section class="card"><h2>Reading your stored data</h2><div class="kv">%s</div><p class="sub">"ask" means the assistant asks you each time. Change with <span class="tg">/healthread activity 30</span> · <span class="tg">/healthread nutrition off</span>.</p></section>'
                '<section class="card"><h2>Grants</h2><ul class="plain">%s</ul></section><section class="card"><h2>Connections</h2><ul class="plain">%s</ul><p class="sub">Disconnecting (<span class="tg">/healthconnect revoke ID</span>) deletes what it synced.</p></section>'
                '%s</div>') % (dg['total'], dg['appointments_next_7_days'], dg['waiting_on_you'], dg['refills_in_progress'],
                               ''.join('<b>%s</b><span>%s</span>' % (esc(k), esc(v)) for k, v in reads.items()), grants, conns,
                               ''.join(visit(v) for v in visits) or '<section class="card wide"><h2>Upcoming visits</h2><p class="empty">No upcoming appointments on record.</p></section>')

    def coach_card(self):
        """0.26.0: the wellness coach on the Health tab (owner page: numbers are fine here, never in Telegram reminders)."""
        h = self.broker.health
        try:
            st = h.call('health_coach_status', {}, True)
        except Exception:
            return ''
        if not st.get('enabled'):
            return '<section class="card wide"><h2>Wellness coach</h2><p>Off (optional). Send <span class="tg">/coach on</span> in Telegram to start; it asks a short questionnaire.</p></section>'
        parts = ['<section class="card wide"><h2>Wellness coach</h2><div class="kv"><b>Goals</b><span>%s</span><b>Reminders</b><span>%s</span></div>' % (
            esc(', '.join(st.get('goals') or []) or 'questionnaire not done yet'), esc(', '.join('%s %s' % kv for kv in (st.get('reminders') or {}).items()) or 'none'))]
        for lim in st.get('limits') or []:
            parts.append('<p class="msg warn">%s</p>' % esc(lim))
        try:
            b = h.call('health_coach_balance', {'days': 7}, True)
            if b.get('days'):
                rows = ''.join('<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="small">%s%s</td></tr>' % (
                    esc(d['date']), d['eaten_kcal'], d['burned_kcal'] if d['burned_kcal'] is not None else '—', d.get('deficit_kcal', '—'), esc(d['burned_from']),
                    '' if d['complete'] else ' · partial day') for d in b['days'])
                parts.append('<h3>Energy balance (deficit = burned − eaten)</h3><table><tr><th>Day</th><th class="num">Eaten</th><th class="num">Burned</th><th class="num">Deficit</th><th>Burned from</th></tr>%s</table>' % rows)
                if b.get('summary'):
                    sm = b['summary']
                    parts.append('<p class="sub">%d complete day(s): average deficit %d kcal/day · total %d kcal ≈ %+.2f kg (rough guide)%s</p>' % (
                        sm['complete_days'], sm['average_daily_deficit_kcal'], sm['total_deficit_kcal'], sm['rough_change_kg'],
                        (' · goal %d kcal/day' % b['goal']['kcal']) if b.get('goal') else ''))
                if b.get('observed_maintenance'):
                    parts.append('<p class="sub">Maintenance from your own trend: about %d kcal/day (%s).</p>' % (b['observed_maintenance']['kcal'], esc(b['observed_maintenance']['from'])))
        except Exception:
            pass
        try:
            m = h.call('health_coach_metrics', {'days': 14}, True)
            aw = m['activity_week']
            kv = '<b>Activity this week</b><span>%d of %d moderate-equivalent minutes</span>' % (aw['moderate_equivalent_minutes'], aw['goal_minutes'])
            for k, v in m['metrics'].items():
                kv += '<b>%s</b><span>%s</span>' % (esc(k.replace('_', ' ')), esc(', '.join('%s %s' % (kk.replace('_', ' '), vv) for kk, vv in v.items())))
            parts.append('<h3>Last 14 days</h3><div class="kv">%s</div>' % kv)
        except Exception:
            pass
        parts.append('<p class="sub">Estimates from published guidance (COACH-SPEC-0.1 and NUTR-SPEC-0.1, pending physician review). Not medical advice.</p></section>')
        return ''.join(parts)

    def page_settings(self, sid):
        s = self.settings(sid)
        devices = ''.join('<li>%s <span class="sub">signed in %s · last seen %s</span> %s</li>' % (esc(d['device']), esc(d['signed_in']), esc(d['last_seen']), '<span class="chip">this device</span>' if d['this_device'] else '<button class="small" data-post="/devices/revoke" data-body=\'{"id":"%s"}\'>Sign out</button>' % esc(d['id'])) for d in s['devices'])
        ms = s.get('models') or {'composer': {'model': 'sonnet'}, 'packets': {'model': 'main'}, 'choices': {}}
        opts = lambda cur: ''.join('<option value="%s"%s>%s</option>' % (k, ' selected' if k == cur else '', esc(v)) for k, v in (ms.get('choices') or {}).items())
        models_card = ('<section class="card"><h2>Models for packets and the Composer</h2><form class="inline" data-post="/settings" data-msg="#msg">'
                       '<div><label>Packet Composer</label><select name="composer_model">%s</select></div>'
                       '<div><label>Packet work sessions (packets without their own Model)</label><select name="packet_model">%s</select></div><button class="primary">Save</button></form>'
                       '<p class="sub">Same as Telegram: <span class="tg">/ai composer MODEL</span> · <span class="tg">/ai packets MODEL</span>. A packet\'s own Model line (or <span class="tg">/mission model NAME MODEL</span>) always wins; '
                       'each Composer session can also pick its own model on its page.</p></section>') % (opts(ms['composer']['model']), opts(ms['packets']['model']))
        return ('<div class="grid"><section class="card"><h2>Model</h2><div class="big">%s</div><div class="sub">fallback: %s</div><p class="sub">Switch in Telegram: <span class="tg">/ai</span> then /ai_sonnet · /ai_opus · /ai_sol · /ai_terra. Per-packet models are set on each packet\'s page.</p></section>%s'
                '<section class="card"><h2>Quiet mode</h2><p class="small">Pauses every packet work session. Running browser tasks, calls and builds finish normally; your chat is unaffected.</p><div class="actions"><button class="%s" data-post="/settings" data-body=\'{"quiet_mode":%s}\'>%s</button></div></section>'
                '<section class="card"><h2>Daily budget</h2><form class="inline" data-post="/settings" data-msg="#msg"><div><label>Assistant spend per day ($, blank = none)</label><input type="text" name="daily_budget_usd" value="%s" style="width:120px"></div><button class="primary">Save</button></form><p class="sub">Warns you in Telegram at 80%% and pauses packet work sessions at 100%% (chat, calls and approved tasks continue).</p></section>'
                '<section class="card"><h2>Purchases</h2><div class="kv"><b>Per-purchase cap</b><span>%s</span><b>Saved cards</b><span>%s</span></div><p class="sub">Change with <span class="tg">/spendcap AMOUNT</span> and <span class="tg">/spendcard</span> in Telegram. Card details are never shown here.</p></section>'
                '<section class="card"><h2>Saved logins and standing permission</h2><ul class="plain">%s</ul><p class="sub">Standing permission (tasks start without /webtask): %s. Change with <span class="tg">/siteauto NAME on</span> · <span class="tg">/siteauto NAME off</span>. Final submits are always reviewed.</p></section>'
                '<section class="card"><h2>Defaults</h2><div class="kv"><b>Packet cadence</b><span>%d min</span><b>Waking hours</b><span>%s ET</span><b>Packets ticking at once</b><span>%d</span></div></section>'
                '<section class="card wide"><h2>Dashboard devices (up to %d)</h2><ul class="plain">%s</ul><p class="sub">Sign out everywhere: <span class="tg">/panel logout</span> in Telegram.</p></section>' + self.connections_card() + '</div>') % (
            esc(s['model']['label']), esc(', '.join(s['model']['fallbacks']) or 'none'), models_card, 'primary' if not s['quiet_mode'] else 'danger', 'false' if s['quiet_mode'] else 'true', 'Turn quiet mode off' if s['quiet_mode'] else 'Turn quiet mode on',
            ('%g' % s['daily_budget_usd']) if s['daily_budget_usd'] else '', ('$%.2f' % s['purchase_cap_usd']) if isinstance(s['purchase_cap_usd'], (int, float)) else 'not set', s['purchase_cards'],
            ''.join('<li><b>%s</b> %s</li>' % (esc(l['site']), A.chip(l['status'])) for l in s['logins']) or '<li class="empty">none saved</li>', esc(', '.join(s['siteauto']) or 'none'),
            s['defaults']['cadence'], esc(s['defaults']['hours']), s['defaults']['max_parallel_packets'], MAX_DEVICES, devices or '<li class="empty">none</li>')

    # ---------- Accounts (0.29.0): Apple and Google by email + app password, verified live ----------
    def accounts_list(self):
        if not self.accounts:
            raise ValueError('Accounts are not available on this server.')
        return self.accounts.list()

    def accounts_post(self, parts, body):
        """POST /accounts/connect {provider,email,password} · /accounts/<id>/recheck · /accounts/<id>/disconnect. The password is used for
        the live check and the secret file only; it is never echoed, logged or stored in the row."""
        if not self.accounts:
            raise ValueError('Accounts are not available on this server.')
        from accounts import AccountError
        try:
            if parts == ['connect']:
                r = self.accounts.connect(body.get('provider'), body.get('email'), body.get('password'))
                lit = [k for k, v in r['services'].items() if v.get('state') == 'ok']
                blocked = [k for k, v in r['services'].items() if v.get('state') == 'blocked']
                msg = 'Connected %s: %s live' % (r['email'], ', '.join(lit) or 'nothing yet')
                if blocked:
                    msg += ' · %s via the Mac companion or relay' % ', '.join(blocked)
                return {'account': r, 'message': msg + '.'}
            if len(parts) == 2 and re.fullmatch(r'[a-z0-9-]{3,60}', parts[0] or ''):
                if parts[1] == 'recheck':
                    r = self.accounts.recheck(parts[0])
                    return {'account': r, 'message': 'Checked again: ' + ('all good.' if r['status'] == 'connected' else 'please reconnect — ' + (r.get('error') or ''))}
                if parts[1] == 'disconnect':
                    r = self.accounts.disconnect(parts[0])
                    return {**r, 'message': 'Disconnected %s; its password was deleted from this server.' % r['email']}
        except AccountError as e:
            raise ValueError(str(e))
        raise ValueError('Unknown accounts action.')

    def page_accounts(self):
        if not self.accounts:
            return '<section class="card"><p class="msg bad">Accounts are not available on this server.</p></section>'
        data = self.accounts.list()
        by_provider = {}
        for a in data['accounts']:
            by_provider.setdefault(a['provider'], []).append(a)
        labels = {'mail': 'Mail', 'send': 'Send mail', 'calendar': 'Calendar', 'contacts': 'Contacts'}

        def service_row(name, s):
            state = s.get('state')
            extra = ''
            if name == 'mail' and s.get('messages') is not None:
                extra = ' · %d in inbox' % s['messages']
            if name == 'calendar' and s.get('calendars'):
                extra = ' · ' + esc(', '.join(s['calendars'][:6])) + (' …' if len(s['calendars']) > 6 else '')
            if name == 'contacts' and s.get('addressbooks'):
                extra = ' · %d address book%s' % (s['addressbooks'], '' if s['addressbooks'] == 1 else 's')
            note = ('<div class="sub">%s</div>' % esc(s.get('error', ''))) if state != 'ok' and s.get('error') else ''
            return '<li><b>%s</b> %s<span class="sub">%s</span>%s</li>' % (labels.get(name, name), A.chip('live' if state == 'ok' else ('via Mac or relay' if state == 'blocked' else 'not working')), extra, note)

        cards = []
        for p in data['providers']:
            connected = ''.join(
                '<div class="acct"><div><b>%s</b> %s <span class="sub">checked %s</span>%s</div><ul class="plain">%s</ul><div class="actions">'
                '<button class="small" data-post="/accounts/%s/recheck">Check again</button> '
                '<button class="small danger" data-post="/accounts/%s/disconnect" data-confirm="Disconnect %s? Its app password is deleted from this server.">Disconnect</button></div></div>' % (
                    esc(a['email']), A.chip(a['status']), esc(when(a['checked'])), ('<div class="msg bad">%s</div>' % esc(a['error'])) if a.get('error') else '',
                    ''.join(service_row(k, v) for k, v in a['services'].items()), esc(a['id']), esc(a['id']), esc(a['email']))
                for a in by_provider.get(p['id'], []))
            guide = ''
            if p.get('steps'):
                # 0.30.0: numbered walk-through + the mistake people make most, right above the form.
                guide = ('<ol class="steps">%s</ol><p class="sub"><a class="btn" href="%s" target="_blank" rel="noopener">Open the %s page ↗</a></p>%s') % (
                    ''.join('<li>%s</li>' % esc(x) for x in p['steps']), esc(p['password_url']), 'Apple' if p['id'] == 'icloud' else 'Google',
                    ('<p class="sub"><b>Common mistake:</b> %s</p>' % esc(p['mistakes'])) if p.get('mistakes') else '')
            form = guide + ('<form class="inline" data-post="/accounts/connect" data-msg="#msg" autocomplete="off"><input type="hidden" name="provider" value="%s">'
                    '<div><label>Email address</label><input type="email" name="email" required placeholder="%s" autocomplete="username"></div>'
                    '<div><label>%s</label><input type="password" name="password" required autocomplete="new-password" spellcheck="false"></div>'
                    '<button class="primary">Connect and check</button></form>'
                    '<p class="sub">%s <a href="%s" target="_blank" rel="noopener">Open the page where you make one ↗</a></p>'
                    '<p class="sub">We check %s against %s right now, keep the password only on this server (never in chat, logs or the model), and show you what works. %s</p>') % (
                        esc(p['id']), 'you@icloud.com' if p['id'] == 'icloud' else 'you@gmail.com', esc(p['password_label']), esc(p['password_help']), esc(p['password_url']),
                        esc(', '.join(labels.get(s, s) for s in p['services'])), 'Apple' if p['id'] == 'icloud' else 'Google', esc(p['not_here']))
            google_extra = ''
            if p['id'] == 'gmail':
                google_extra = ('<h3>Calendar, Contacts, Tasks and Drive</h3><p class="sub">These need Google sign-in rather than a password. Use <b>Connections</b> on the Settings tab: '
                                'provider <span class="tg">Google</span> › Connect. PROCURATOR asks only for calendar, contacts, tasks, sending mail and files it creates — never to read your mailbox, '
                                'which is why mail reading uses the app password above.</p>')
            cards.append('<section class="card wide"><h2>%s</h2>%s<h3>Add an account</h3>%s%s</section>' % (esc(p['title']), connected or '<p class="sub">No %s account connected yet.</p>' % ('Apple' if p['id'] == 'icloud' else 'Google'), form, google_extra))
        intro = ('<section class="card wide"><h2>Accounts</h2><p class="small">Connect the accounts PROCURATOR should work from. One email address and one app password per account; the check takes a few seconds. '
                 'Passwords never reach the assistant or Telegram. If you change your Apple or Google password later, every app password stops working and you reconnect here.</p>'
                 '<div id="msg"></div></section>')
        return '<div class="grid">' + intro + ''.join(cards) + '</div>'

    def page_setup(self):
        """0.30.0: everything the box needs, with the one next step for each missing piece. No secret is read into the page."""
        import setup_core
        st = setup_core.setup_status()
        n_accounts = len((self.accounts.list() if self.accounts else {'accounts': []})['accounts'])
        try:
            google = next((p for p in self.broker.call('oauth_list', {}, True).get('providers', []) if p.get('id') == 'google'), {})
        except Exception:
            google = {}
        g_ok = bool(google.get('accounts')) or google.get('status') == 'connected'

        def row(ok, title, done_text, todo_html, optional=False):
            mark = A.chip('ready' if ok else ('optional' if optional else 'needed'))
            return '<li><b>%s</b> %s<div class="sub">%s</div></li>' % (esc(title), mark, esc(done_text) if ok else todo_html)

        steps = [
            row(st['model_key'], 'AI model key', 'Anthropic key saved and checked.',
                'On the server: <span class="tg">sudo python3 bin/connect-accounts.py --anthropic-only</span>'),
            row(st['telegram_token'] and st['owner'], 'Telegram, paired to you', 'Your bot answers only to you.',
                'On the server: <span class="tg">sudo bash bin/install.sh --setup-link</span> and open the link, or <span class="tg">sudo python3 bin/connect-accounts.py --telegram-only</span>'),
            row(n_accounts > 0, 'Apple / Google accounts', '%d account%s connected.' % (n_accounts, '' if n_accounts == 1 else 's'),
                'Open the <a href="%s/accounts">Accounts</a> tab: one email and one app password each.' % ROOT),
            row(g_ok, 'Google Calendar, Contacts, Tasks', 'Signed in with Google.',
                'Settings › Connections › Google › Connect (needs the Google client set once: <span class="tg">bash bin/set-oauth-client.sh google</span>).', optional=True),
            row(st['twilio'], 'Phone calls', 'Twilio number connected.', 'On the server: <span class="tg">sudo python3 bin/connect-twilio.py</span>', optional=True),
            row(st['browserbase'], 'Cloud browser', 'Browserbase key saved.', 'On the server: <span class="tg">sudo python3 bin/connect-browserbase.py</span>', optional=True),
        ]
        edition = 'PROCURATOR-X (developer edition)' if st['edition'] == 'x' else 'PROCURATOR (standard edition)'
        done = sum(1 for ok in (st['model_key'], st['telegram_token'] and st['owner'], n_accounts > 0) if ok)
        return ('<div class="grid"><section class="card wide"><h2>Setup</h2><p class="small">%d of 3 essentials done. %s%s</p>'
                '<ul class="plain">%s</ul></section></div>') % (
                    done, esc(edition), (' · ' + esc(st['domain'])) if st['domain'] else '', ''.join(steps))

    def bot_token(self):
        try:
            for line in (Path('/opt/procurator') / '.env').read_text().splitlines():
                if line.startswith('TELEGRAM_BOT_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
        except OSError:
            return None
        return None

    def owner_id(self):
        try:
            import owner_config
            return owner_config.primary(owner_config.load())['id']
        except Exception:
            import broker as _b
            return _b.OWNER

    def page_live(self):
        """0.28.0: what the assistant is doing now — browsers being watched, the activity stream, approvals waiting in Telegram."""
        recent = self.broker.stream.recent(40)
        tasks = []
        for e in reversed(recent):
            if e.get('task_id') and e['task_id'] not in tasks:
                tasks.append(e['task_id'])
        cards = ''.join('<section class="card"><h2>Browser %s</h2><img class="live" data-live="%s" alt="Live view of browser %s (appears while you watch)">'
                        '<p class="sub" data-live-note="%s">Waiting for a frame… Frames are taken only while this page is open, about one a second, '
                        'with password and card fields masked. Banks and health portals show a placeholder.</p>'
                        '<div class="actions"><button class="small" data-post="/live/%s/reveal" data-reload="no" data-confirm="Ask in Telegram to show masked fields for this task?">Show masked parts…</button></div></section>'
                        % (esc(t), esc(t), esc(t), esc(t), esc(t)) for t in tasks[:4])
        rows = ''.join('<li><span class="sub">%s</span> <b>%s</b> %s %s</li>' % (esc(time.strftime('%H:%M:%S', time.localtime(e['ts']))), esc(e['source']), esc(e['kind'].replace('_', ' ')),
                                                                             esc(e['summary'])) for e in reversed(recent))
        return ('<div class="grid">%s<section class="card wide"><h2>Activity now</h2><ul class="plain" id="live-stream">%s</ul>'
                '<p class="sub">Names of tools and sites only: never email bodies, page text, typed values or health details. Approvals stay in Telegram.</p></section></div>') % (
                    cards or '<section class="card"><h2>No browser in use</h2><p class="sub">When the assistant opens a browser, it appears here.</p></section>',
                    rows or '<li class="empty">nothing yet</li>')

    def page_library(self, query):
        """0.28.0: Library — what the owner chooses to put on the server for the assistant and packets to use."""
        lib = self.broker.call('library_list', {'collection': (query.get('collection') or [None])[0]}, True)
        st = self.broker.call('library_status', {}, True)
        chip = lambda on, yes, no: A.chip(yes if on else no)
        items = ''.join('<tr><td><b>%s</b><div class="sub">%s · %s · %s chars%s%s</div></td><td>%s</td><td>%s %s %s</td><td>'
                        '<button class="small" data-post="/library/%s/update" data-body=\'{"use_chat":%s}\'>%s chat</button> '
                        '<button class="small" data-post="/library/%s/update" data-body=\'{"use_packets":%s}\'>%s packets</button> '
                        '<button class="small" data-post="/library/%s/update" data-body=\'{"pinned":%s}\'>%s</button> '
                        '<button class="small danger" data-post="/library/%s/delete" data-confirm="Delete %s from the server?">Delete</button></td></tr>' % (
                            esc(i['title']), esc(i['id']), esc(i['kind']), '{:,}'.format(i['chars'] or 0), (' · ' + esc(i['note'])) if i.get('note') else '',
                            (' · labels: ' + esc(', '.join(i['labels']))) if i['labels'] else '', esc(i['collection']),
                            chip(i['use_chat'], 'chat', 'not in chat'), chip(i['use_packets'], 'packets', 'not in packets'), chip(i['pinned'], 'pinned', ''),
                            esc(i['id']), 'false' if i['use_chat'] else 'true', 'Hide from' if i['use_chat'] else 'Use in',
                            esc(i['id']), 'false' if i['use_packets'] else 'true', 'Hide from' if i['use_packets'] else 'Use in',
                            esc(i['id']), 'false' if i['pinned'] else 'true', 'Unpin' if i['pinned'] else 'Pin as context',
                            esc(i['id']), esc(i['title'])) for i in lib['items'])
        cols = ' '.join('<a class="chip" href="%s/library?collection=%s">%s (%d)</a>' % (ROOT, esc(c['collection']), esc(c['collection']), c['items']) for c in lib['collections'])
        return ('<div class="grid"><section class="card wide"><h2>Add to the library</h2>'
                '<p class="sub">Only what you add here goes to the server. Documents (PDF, Word, text, Markdown, CSV, JSON), notes, and exports of earlier '
                'ChatGPT or Claude chats (the conversations.json or the export .zip from their settings). The assistant and your packets use them as reference; '
                'nothing in them counts as an instruction.</p>'
                '<form id="lib-upload" class="inline"><div><label>File</label><input type="file" id="lib-file"></div>'
                '<div><label>Collection</label><input type="text" id="lib-collection" value="general" style="width:160px"></div>'
                '<div><label><input type="checkbox" id="lib-chat" checked> Use in chat</label></div><div><label><input type="checkbox" id="lib-packets" checked> Use in packets</label></div>'
                '<div><label><input type="checkbox" id="lib-pin"> Pin as project context</label></div><button class="primary" id="lib-send" type="button">Upload</button></form>'
                '<form class="inline" data-post="/library/note" data-msg="#msg"><div style="flex:1"><label>Or paste a note (project context, background)</label><textarea name="text" rows="3" style="width:100%%"></textarea></div>'
                '<div><label>Title</label><input type="text" name="title" style="width:160px"></div><div><label>Collection</label><input type="text" name="collection" value="context" style="width:120px"></div>'
                '<div><label>Pin</label><select name="pinned"><option value="1">yes</option><option value="0">no</option></select></div><button class="primary">Save note</button></form>'
                '<p class="sub">%d items · %.2f MB of text · %d pinned · limits: 25 MB per upload, %d items, 50 MB of text. Packets use a collection when their start file has '
                '<span class="tg">**References:** collection-name</span>. Pinned items (1,500 characters in total) ride with every conversation.</p></section>'
                '<section class="card wide"><h2>Collections</h2><p>%s</p><table><tr><th>Item</th><th>Collection</th><th>Used by</th><th></th></tr>%s</table></section></div>') % (
                    st['items'], st['text_mb'], st['pinned'], st['limits']['items'], cols or '<span class="sub">none yet</span>', items or '<tr><td colspan="4" class="empty">Nothing uploaded yet.</td></tr>')

    def connections_card(self):
        """0.27.0: Setup > Connections — the one OAuth store (oauth_store.py). Connect, rename an account alias, disconnect."""
        try:
            c = self.broker.call('oauth_list', {}, True)
        except Exception as e:
            return '<section class="card wide"><h2>Connections</h2><p class="msg bad">%s</p></section>' % esc(str(e)[:160])
        provs = ''.join('<li><b>%s</b> %s %s<div class="sub">%s</div></li>' % (
            esc(p['title']), A.chip(p['status']),
            '<button class="small" data-post="/oauth/begin" data-body=\'{"provider":"%s"}\'>Connect</button>' % esc(p['id']) if p['status'] in ('ready', 'test') else '',
            esc(p.get('note') or ', '.join(p['scopes']))) for p in c['providers'])
        grants = ''.join('<li><b>%s</b> · %s %s <span class="sub">connected %s%s</span> <button class="small" data-post="/oauth/disconnect" data-body=\'{"grant":"%s"}\' '
                         'data-confirm="Disconnect %s? Its tokens are deleted.">Disconnect</button></li>' % (
                             esc(g['provider']), esc(g['display']), A.chip(g['status']), esc(when(g['connected'])), (' · ' + esc(g['last_error'])) if g.get('last_error') else '',
                             esc(g['id']), esc(g['display'])) for g in c['grants'])
        accts = ''.join('<li><span class="tg">%s</span> %s · %s %s</li>' % (esc(a['alias']), esc(a['display']), esc(a['provider']), A.chip(a['status'])) for a in c['accounts'])
        return ('<section class="card wide"><h2>Connections</h2><p class="sub">One store for every OAuth provider. Tokens stay in the server\'s secrets folder and are never shown. '
                'Posting as any account still needs your <span class="tg">/postsend</span> approval of the exact preview.</p>'
                '<h3>Providers</h3><ul class="plain">%s</ul><h3>Connected</h3><ul class="plain">%s</ul><h3>Accounts (aliases tools use)</h3><ul class="plain">%s</ul>'
                '<p class="sub">Rename an alias in Telegram: <span class="tg">/connections rename PROVIDER ALIAS NEW</span>.</p></section>') % (
                    provs, grants or '<li class="empty">none</li>', accts or '<li class="empty">none</li>')


MOCK_EHR = os.environ.get('PROCURATOR_MOCK_EHR', 'http://127.0.0.1:3351')


def mock_ehr_forward(method, rest, query, headers, body):
    """0.22.0: the synthetic mock EHR (mock_ehr.py, loopback 3351) is reachable at /assistant/dashboard/mock-ehr/… so its sign-in page
    opens on their phone and the portal client reaches it over https, without an nginx change. Its admin API is never forwarded."""
    import urllib.request as ur, urllib.error as ue
    sub = rest[len('/mock-ehr'):] or '/'
    if not re.fullmatch(r'/[a-z]+/(?:fhir|oauth2|rx)(?:/[A-Za-z0-9._/$-]*)?', sub):
        return 404, {'Content-Type': 'text/plain'}, b'Not found.'
    keep = {k: v for k, v in headers.items() if k.lower() in ('authorization', 'content-type', 'accept', 'if-none-exist', 'if-match', 'idempotency-key')}
    req = ur.Request(MOCK_EHR + sub + ('?' + query if query else ''), data=body if method in ('POST', 'PUT') else None, headers=keep, method=method)
    class NoRedirect(ur.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        with ur.build_opener(NoRedirect).open(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except ue.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b''
    except Exception:
        return 502, {'Content-Type': 'text/plain'}, b'The mock EHR is not running (procurator-mock-ehr service).'


def health_post(broker, rest, headers, body):
    """0.23.0: machine endpoints of the health module (no dashboard cookie; each authenticates itself):
      POST /health/pair                 the iPhone companion exchanges a single-use /healthpair code for a device credential
      POST /health/sync                 the companion uploads a batch (Authorization: Bearer <device credential>)
      POST /health/rx-callback/<conn>   a pharmacy status webhook (HMAC-signed with the secret the pharmacy issued)
    Returns (status, JSON-able dict). Health values never go to Telegram; a pharmacy update sends one neutral line."""
    from health_sync import SyncError
    mods = getattr(broker.health, 'modules', None) or {}
    try:
        if rest == '/health/pair':
            return 200, mods['health_sync_'].pair(body)
        if rest == '/health/sync':
            return 200, mods['health_sync_'].sync(headers, body)
        m = re.fullmatch(r'/health/rx-callback/([a-f0-9]{16})', rest)
        if m:
            st, out = mods['health_pharmacy_'].webhook(m.group(1), headers, body)
            if out.get('notify'):
                try:
                    broker.provider.telegram(out['notify'])
                except Exception:
                    pass
            return st, {k: v for k, v in out.items() if k in ('ok', 'duplicate', 'error', 'outcome')}
    except SyncError as e:
        return e.status, {'error': e.kind, 'detail': e.detail}
    except KeyError:
        return 503, {'error': 'not_configured'}
    except ValueError as e:
        return 400, {'error': 'bad_request', 'detail': str(e)[:200]}
    return 404, {'error': 'not_found'}


def message_page(text, title='PROCURATOR'):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title></head>'
            '<body style="font:17px/1.5 -apple-system,system-ui,sans-serif;padding:24px;max-width:560px;margin:0 auto"><h1 style="font-size:20px">PROCURATOR</h1><p>%s</p></body></html>') % (esc(title), esc(text))


def handler_for(dash):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'assistant'
        sys_version = ''

        def log_message(self, *args):  # never log paths (login links carry the token) or bodies
            pass

        def send(self, code, body, ctype='text/html; charset=utf-8', nonce=None, headers=(), frame_src=None, ancestors=None, script_extra=''):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Robots-Tag', 'noindex')
            csp = "default-src 'none'; connect-src 'self'; img-src 'self' data: blob:; form-action 'self'; frame-ancestors %s; base-uri 'none'" % (ancestors or "'none'")
            if nonce:
                csp += "; script-src 'nonce-%s'%s; style-src 'nonce-%s'" % (nonce, (' ' + script_extra) if script_extra else '', nonce)
            if frame_src:
                csp += '; frame-src ' + frame_src
            self.send_header('Content-Security-Policy', csp)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def cookie(self):
            c = self.headers.get('Cookie') or ''
            for part in c.split(';'):
                k, _, v = part.strip().partition('=')
                if k == COOKIE:
                    return v
            return None

        def set_cookie(self, value, max_age):
            return ('Set-Cookie', '%s=%s; Path=%s; Max-Age=%d; HttpOnly; Secure; SameSite=Strict' % (COOKIE, value, ROOT, max_age))

        def need_login(self):
            self.send(401, message_page('Not signed in. Send /panel in Telegram and open the link it sends you.'))

        def setup_get(self, token, status):
            import setup_wizard
            if not dash.setup:
                return self.send(404, message_page('Setup is not available on this server.'))
            if status:
                code, out = dash.setup.handle('GET', token, 'status', {}, True)
                return self.send(code, json.dumps(out), 'application/json', headers=[('Cache-Control', 'no-store')])
            rec, why = dash.setup.allowed(token)
            if not rec:
                return self.send(410, setup_wizard.blocked_page(why))
            nonce = secrets.token_urlsafe(16)
            return self.send(200, setup_wizard.page(nonce, token, dash.setup.status(rec)), nonce=nonce, headers=[('Cache-Control', 'no-store')])

        def setup_post(self, token, sub):
            if not dash.setup:
                return self.send(404, '{"ok":false,"error":"Not found."}', 'application/json')
            origin = self.headers.get('Origin')
            origin_ok = (self.headers.get('X-Procurator-Setup') == '1' and (not origin or origin == ORIGIN))
            if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
                return self.send(415, '{"ok":false,"error":"JSON only."}', 'application/json')
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= 4096:
                return self.send(413, '{"ok":false,"error":"Too large."}', 'application/json')
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self.send(400, '{"ok":false,"error":"Bad request."}', 'application/json')
            code, out = dash.setup.handle('POST', token, sub, body, origin_ok)
            return self.send(code, json.dumps(out), 'application/json', headers=[('Cache-Control', 'no-store')])

        def do_GET(self):
            path, _, qs = self.path.partition('?')
            query = urllib.parse.parse_qs(qs)
            if not path.startswith(ROOT):
                return self.send(404, message_page('Not found.'))
            rest = path[len(ROOT):]
            m = re.fullmatch(r'/setup/([A-Za-z0-9_-]{40,64})(/status)?', rest)
            if m:
                return self.setup_get(m.group(1), bool(m.group(2)))
            if rest in ('/app', '/app/'):
                # 0.28.0: Telegram Mini App entry (menu button). Telegram's web client frames it; every other route keeps frame-ancestors 'none'.
                import miniapp
                nonce = secrets.token_urlsafe(16)
                return self.send(200, miniapp.page(nonce, ROOT), nonce=nonce, ancestors='https://web.telegram.org', script_extra='https://telegram.org')
            v1 = rest.startswith('/api/v1/')
            if v1:
                rest = '/api/' + rest[len('/api/v1/'):]   # 0.28.0 (B9): /api/v1/... is the versioned API; /api/... stays for one release
            m = re.fullmatch(r'/login/([A-Za-z0-9_-]{40,64})', rest)
            if m:
                try:
                    ua = (self.headers.get('User-Agent') or '')[:200]
                    label = 'iPhone' if 'iPhone' in ua else 'iPad' if 'iPad' in ua else 'Mac' if 'Macintosh' in ua else 'Windows' if 'Windows' in ua else 'Android' if 'Android' in ua else 'device'
                    value = dash.sessions.login(m.group(1), label + ' · ' + when(dash.now()))
                except ValueError as e:
                    return self.send(410, message_page(str(e)))
                nxt = (query.get('next') or [''])[0]
                return self.send(302, '', headers=[('Location', ROOT + (nxt if NEXT_OK.match(nxt) else '/')), self.set_cookie(value, SESSION_DAYS * 86400)])
            m = re.fullmatch(r'/watch/([a-f0-9]{64})', rest)
            if m:
                # Watch live (0.18.0): single-use, 15-minute link from Telegram; the live view is shown in a frame that ignores taps.
                try:
                    w = dash.broker.watch_open(m.group(1))
                except (ValueError, AttributeError) as e:
                    return self.send(410, message_page(str(e) if isinstance(e, ValueError) else 'Not available.'))
                nonce = secrets.token_urlsafe(16)
                page = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Watching browser %s</title>'
                        '<style nonce="%s">html,body{margin:0;height:100%%;background:#111;color:#ddd;font:14px -apple-system,system-ui,sans-serif}'
                        '.bar{padding:8px 12px}.wrap{position:absolute;top:36px;left:0;right:0;bottom:0}iframe{width:100%%;height:100%%;border:0;pointer-events:none}</style></head>'
                        '<body><div class="bar">Watching browser %s — view only. To act yourself, use /webtakeover in Telegram.</div>'
                        '<div class="wrap"><iframe src="%s" sandbox="allow-scripts allow-same-origin" referrerpolicy="no-referrer"></iframe></div></body></html>') % (
                            esc(w['session_id']), nonce, esc(w['session_id']), esc(w['live_view_url']))
                return self.send(200, page, nonce=nonce, frame_src='https://www.browserbase.com https://*.browserbase.com')
            if rest.startswith('/mock-ehr/'):
                return self.raw(*mock_ehr_forward('GET', rest, qs, dict(self.headers), b''))
            if rest == '/oauth/callback':
                # 0.27.0: every OAuth provider returns here (oauth_store.py); the single-use state protects it.
                one = lambda k: (query.get(k) or [''])[0]
                try:
                    r = dash.broker.call('oauth_complete', {'state': one('state'), 'code': one('code') or None, 'error': one('error') or None}, True)
                except ValueError as e:
                    return self.send(400, message_page('Not connected. %s' % str(e)[:200], 'Connections'))
                except Exception:
                    return self.send(500, message_page('Not connected: the server could not finish the sign-in. Start again from Settings.', 'Connections'))
                try:
                    dash.broker.provider.telegram('Connected %s (%s). Accounts: %s. Posting still needs your /postsend approval each time.' % (
                        r.get('provider'), r.get('display') or '', ', '.join(r.get('accounts') or []) or 'none'))
                except Exception:
                    pass
                return self.send(200, message_page('Connected. You can close this page.', 'Connections'))
            if rest == '/health/callback':
                # 0.22.0: the portal sends their phone back here after the owner signs in and allows access.
                one = lambda k: (query.get(k) or [''])[0]
                try:
                    r = dash.broker.health.call('health_portal_callback', {'state': one('state'), 'code': one('code') or None, 'error': one('error') or None}, True)
                except Exception as e:
                    r = {'error': 'failed', 'detail': str(e)[:160]}
                if r.get('status') == 'connected':
                    try:
                        dash.broker.provider.telegram('Health portal connected: %s (%s). The assistant can now read and book within the grants you approve.' % (r.get('patient_display') or 'patient', r['connection']))
                    except Exception:
                        pass
                    return self.send(200, message_page('Connected. You can close this page and return to Telegram.', 'Health portal'))
                return self.send(400, message_page('Not connected (%s). %s Start again with /healthauth in Telegram.' % (r.get('error'), r.get('detail') or ''), 'Health portal'))
            m = re.fullmatch(r'/health/receipt/([A-Za-z0-9_-]{24,64})', rest)
            if m:
                # 0.21.0: private health receipt (build-plans/health/01-shared-foundation.md): Telegram carries one neutral line; the
                # details open here from a 7-day link that is counted each time it is read. No session cookie, nothing cached.
                try:
                    r = dash.broker.health.receipt_by_token(m.group(1))
                except Exception:
                    r = None
                if not r:
                    return self.send(410, message_page('This receipt link has expired or does not exist.'))
                nonce = secrets.token_urlsafe(16)
                body = '<br>'.join(esc(line) for line in str(r['body']).split('\n'))
                page = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
                        '<title>Health receipt</title><style nonce="%s">body{margin:0;padding:20px;background:#fafafa;color:#222;font:16px/1.5 -apple-system,system-ui,sans-serif}'
                        '.card{max-width:640px;margin:0 auto;background:#fff;border:1px solid #ddd;border-radius:10px;padding:18px}h1{font-size:18px;margin:0 0 10px}.meta{color:#666;font-size:13px;margin-top:14px}</style></head>'
                        '<body><div class="card"><h1>Private health receipt</h1><div>%s</div><div class="meta">Receipt %s · written %s · this page is private to you and is not kept in the assistant\'s memory.</div></div></body></html>') % (
                            nonce, body, esc(r['id']), esc(when(r['created'])))
                return self.send(200, page, nonce=nonce)
            sid = dash.sessions.session(self.cookie())
            if rest == '/logout':
                if sid:
                    dash.sessions.revoke(dash.sessions.h(sid)[:12])
                return self.send(200, message_page('Signed out on this device.'), headers=[self.set_cookie('', 0)])
            if not sid:
                return self.need_login()
            if rest == '/api/events' and ('text/event-stream' in (self.headers.get('Accept') or '') or query.get('stream')):
                return self.sse(sid, query)
            m = re.fullmatch(r'/api/live/([A-Za-z0-9_-]{1,40})/frame', rest)
            if m:
                return self.live_frame(m.group(1), sid)
            if rest.startswith('/api/'):
                try:
                    out = dash.api('GET', rest[5:], {k: v[0] for k, v in query.items()}, sid)
                    if rest == '/api/usage.csv':
                        return self.send(200, out['csv'], 'text/csv; charset=utf-8')
                    return self.send(200, json.dumps({'ok': True, **out}), 'application/json')
                except ValueError as e:
                    return self.send(404 if str(e) == 'Not found.' else 400, json.dumps({'ok': False, 'error': str(e)}), 'application/json')
                except Exception:
                    return self.send(500, json.dumps({'ok': False, 'error': 'The dashboard could not read that right now.'}), 'application/json')
            if rest == '/usage.csv':
                return self.send(200, dash.usage_csv(30), 'text/csv; charset=utf-8')
            m = re.fullmatch(r'/traces/([a-f0-9]{16})/(\d{8}-\d{6}-[a-f0-9]{1,16}\.zip)', rest)
            if m:
                try:
                    f = dash.broker.fixtures.trace_file(m.group(1), m.group(2))
                except (ValueError, AttributeError):
                    return self.send(404, message_page('Not found.'))
                return self.send(200, f.read_bytes(), 'application/zip', headers=[('Content-Disposition', 'attachment; filename="%s"' % m.group(2))])
            m = re.fullmatch(r'/templates/([a-z]+)', rest)
            if m and m.group(1) in packets.TEMPLATES:
                return self.send(200, packets.TEMPLATES[m.group(1)], 'text/markdown; charset=utf-8')
            nonce = secrets.token_urlsafe(16)
            csrf = dash.sessions.csrf(sid)
            try:
                if rest in ('', '/'):
                    body, tab = dash.page_overview(), ''
                elif rest == '/packets':
                    body, tab = dash.page_packets(), 'packets'
                elif re.fullmatch(r'/packets/[a-z0-9-]{2,24}', rest):
                    body, tab = dash.page_packet(rest.rsplit('/', 1)[1]), 'packets'
                elif rest == '/compose':
                    body, tab = dash.page_compose_list(), 'compose'
                elif re.fullmatch(r'/compose/[a-f0-9]{16}', rest):
                    body, tab = dash.page_compose(rest.rsplit('/', 1)[1]), 'compose'
                elif rest == '/builds':
                    body, tab = dash.page_builds(), 'builds'
                elif re.fullmatch(r'/builds/[a-f0-9]{16}', rest):
                    body, tab = dash.page_build(rest.rsplit('/', 1)[1]), 'builds'
                elif rest == '/health':
                    body, tab = dash.page_health(), 'health'
                elif rest == '/live':
                    body, tab = dash.page_live(), 'live'
                elif rest == '/library':
                    body, tab = dash.page_library(query), 'library'
                elif rest == '/usage':
                    body, tab = dash.page_usage(), 'usage'
                elif rest == '/machines':
                    body, tab = dash.page_machines(), 'machines'
                elif re.fullmatch(r'/traces/[a-f0-9]{16}', rest):
                    body, tab = dash.page_traces(rest.rsplit('/', 1)[1]), 'usage'
                elif rest == '/activity':
                    body, tab = dash.page_activity(query), 'activity'
                elif rest == '/accounts':
                    body, tab = dash.page_accounts(), 'accounts'
                elif rest == '/setup':
                    body, tab = dash.page_setup(), 'setup'
                elif rest == '/settings':
                    body, tab = dash.page_settings(sid), 'settings'
                else:
                    return self.send(404, message_page('Not found.'))
            except ValueError as e:
                body, tab = '<section class="card"><p class="msg bad">%s</p></section>' % esc(str(e)), ''
            self.send(200, dash.page(tab, body, nonce, csrf), nonce=nonce)

        def sse(self, sid, query):
            # 0.28.0 (B10): server-sent events for about 25 seconds, then the browser reconnects with Last-Event-ID.
            try:
                last = int(self.headers.get('Last-Event-ID') or (query.get('since') or ['0'])[0] or 0)
            except ValueError:
                last = 0
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            end = time.time() + 25
            try:
                while time.time() < end:
                    for e in dash.broker.stream.since(last, 50):
                        last = e['id']
                        self.wfile.write(('id: %d\nevent: activity\ndata: %s\n\n' % (e['id'], json.dumps(e))).encode())
                    self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def live_frame(self, task, sid):
            # 0.28.0 (B11): the latest frame from memory; reading it marks this session as watching (capture stops 5 s after the last read).
            f = dash.broker.live.frame(task, dash.sessions.h(sid)[:12])
            if not f:
                return self.send(204, b'', 'text/plain')
            if f['jpeg'] is None:
                return self.send(200, json.dumps({'placeholder': f['placeholder'], 'etag': f['etag'], 'ts': f['ts']}), 'application/json')
            if self.headers.get('If-None-Match') == f['etag']:
                return self.send(304, b'', 'image/jpeg', headers=[('ETag', f['etag'])])
            return self.send(200, f['jpeg'], 'image/jpeg', headers=[('ETag', f['etag'])])

        def raw(self, status, headers, body):
            # Forwarded mock-EHR response: no cookies, no caching, its own content type; Location rewritten to the public path.
            self.send_response(status)
            for k, v in headers.items():
                if k.lower() in ('content-type', 'etag', 'retry-after', 'www-authenticate'):
                    self.send_header(k, v)
                elif k.lower() == 'location':
                    self.send_header('Location', v)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Robots-Tag', 'noindex')
            self.end_headers()
            self.wfile.write(body)

        def mock_body(self):
            n = int(self.headers.get('Content-Length') or 0)
            return self.rfile.read(min(n, 200000)) if n else b''

        def do_PUT(self):
            path, _, qs = self.path.partition('?')
            if path.startswith(ROOT + '/mock-ehr/'):
                return self.raw(*mock_ehr_forward('PUT', path[len(ROOT):], qs, dict(self.headers), self.mock_body()))
            return self.send(404, message_page('Not found.'))

        def miniapp_auth(self):
            import miniapp
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self.send(403, '{"ok":false,"error":"Wrong site."}', 'application/json')
            try:
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(n, 8192)).decode() or '{}') if 0 < n <= 8192 else {}
                miniapp.verify(body.get('init_data'), dash.bot_token(), dash.owner_id())
            except (ValueError, miniapp.MiniAppError) as e:
                return self.send(401, json.dumps({'ok': False, 'error': str(e)}), 'application/json')
            value = dash.sessions.issue('telegram_miniapp', 'Telegram app · ' + when(dash.now()), miniapp.SESSION_SECONDS)
            return self.send(200, json.dumps({'ok': True, 'go': ROOT + '/'}), 'application/json', headers=[self.set_cookie(value, miniapp.SESSION_SECONDS)])

        def library_upload(self, sid):
            # 0.28.0: the Library tab sends the file's bytes (any type the library reads), with name and choices in the query string.
            if (self.headers.get('Origin') or ORIGIN) != ORIGIN or not hmac.compare_digest(self.headers.get('X-Procurator-CSRF') or '', dash.sessions.csrf(sid)):
                return self.send(403, '{"ok":false,"error":"Reload the page and try again."}', 'application/json')
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = -1
            if not 0 < n <= 25 * 1024 * 1024:
                return self.send(413, '{"ok":false,"error":"Uploads are 1 byte to 25 MB."}', 'application/json')
            q = {k: v[0] for k, v in urllib.parse.parse_qs(self.path.partition('?')[2]).items()}
            try:
                import base64 as _b
                item = dash.broker.call('library_add', {'data_b64': _b.b64encode(self.rfile.read(n)).decode(), 'name': q.get('name', 'upload'), 'collection': q.get('collection'),
                                                        'use_chat': q.get('use_chat', '1'), 'use_packets': q.get('use_packets', '1'), 'pinned': q.get('pinned', '0'),
                                                        'kind': q.get('kind') or None, 'title': q.get('title') or None}, True)
                return self.send(200, json.dumps({'ok': True, 'item': item, 'message': 'Added %s (%s, %s).' % (item['title'], item['kind'], item['status'])}), 'application/json')
            except ValueError as e:
                return self.send(400, json.dumps({'ok': False, 'error': str(e)}), 'application/json')

        def do_POST(self):
            path = self.path.split('?')[0]
            if path.startswith(ROOT + '/mock-ehr/'):
                return self.raw(*mock_ehr_forward('POST', path[len(ROOT):], self.path.partition('?')[2], dict(self.headers), self.mock_body()))
            if path.startswith(ROOT + '/health/'):
                n = int(self.headers.get('Content-Length') or 0)
                if n > 2_100_000:
                    return self.send(413, '{"error":"too_large"}', 'application/json')
                st, out = health_post(dash.broker, path[len(ROOT):], dict(self.headers), self.rfile.read(n) if n else b'')
                return self.send(st, json.dumps(out), 'application/json')
            m = re.fullmatch(re.escape(ROOT) + r'/setup/([A-Za-z0-9_-]{40,64})/(model|bot|pair|restart)', path)
            if m:
                return self.setup_post(m.group(1), m.group(2))
            if path.startswith(ROOT + '/api/v1/'):
                path = ROOT + '/api/' + path[len(ROOT + '/api/v1/'):]
            if path == ROOT + '/api/auth/telegram':
                return self.miniapp_auth()
            if not path.startswith(ROOT + '/api/'):
                return self.send(404, '{"ok":false,"error":"Not found."}', 'application/json')
            sid = dash.sessions.session(self.cookie())
            if path == ROOT + '/api/library/upload' and sid:
                return self.library_upload(sid)
            if not sid:
                return self.send(401, '{"ok":false,"error":"Not signed in. Send /panel in Telegram for a link."}', 'application/json')
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self.send(403, '{"ok":false,"error":"Wrong site."}', 'application/json')
            if not hmac.compare_digest(self.headers.get('X-Procurator-CSRF') or '', dash.sessions.csrf(sid)):
                return self.send(403, '{"ok":false,"error":"Reload the page and try again."}', 'application/json')
            if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
                return self.send(415, '{"ok":false,"error":"JSON only."}', 'application/json')
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self.send(413, '{"ok":false,"error":"Too large (2 MB of packet text at most)."}', 'application/json')
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                if not isinstance(body, dict):
                    raise ValueError('Bad request.')
                out = dash.api('POST', path[len(ROOT) + 5:], body, sid)
                return self.send(200, json.dumps({'ok': True, **out}), 'application/json')
            except ValueError as e:
                return self.send(400, json.dumps({'ok': False, 'error': str(e)}), 'application/json')
            except Exception:
                return self.send(500, json.dumps({'ok': False, 'error': 'That did not go through. Nothing was changed beyond what the page now shows.'}), 'application/json')
    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start(broker, state_dir='/opt/procurator/state', db_dir='/var/lib/procurator-web', port=3343, sample=True):
    from vitals import Vitals
    v = Vitals(str(Path(db_dir) / 'vitals.sqlite3'))
    if sample:
        v.start()
    dash = Dashboard(broker, state_dir, str(Path(db_dir) / 'dashboard.sqlite3'), vitals=v)
    broker.dashboard = dash
    broker.missions.checks = dash.checks
    broker.missions.quiet = dash.paused_all
    server = Server(('127.0.0.1', port), handler_for(dash))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return dash, server
