"""Physical-world policy (web 0.28.0, plan v2 B4). No home connector ships yet; this is the contract every one will run under.

Every tool with effect: physical names a device, and every device has a safety tier from its kind:
  comfort  lights, thermostat, fans, blinds, plugs, speakers  -> runs without a second approval when every parameter is inside an
                                                                 owner grant (/homegrant thermostat setpoint 19-25 07:00-23:00) and
                                                                 the manifest bounds; otherwise asks
  access   locks, garage doors, alarm, cameras                 -> a live, hash-bound owner approval every time (/homeapprove ID HASH),
                                                                 and refused outright from packets, schedules, Composer, MCP requests
                                                                 and inbound events
  safety   smoke, carbon monoxide, leak sensors                 -> disable / mute / remove are hard blocks (policy/hard-blocks.json);
                                                                 anything else asks
/homeoff refuses every physical tool at once and survives restarts; /homeon restores.

Results follow the read-back contract (confirm: readback): after a command the device state is read back, and the outcome is
confirmed (state matches), unconfirmed (no read-back: never reported as success) or failed (state contradicts). If the bridge is
unreachable the result is "unreachable"; nothing is queued silently.

Device and room names are untrusted strings (a device can be named like an instruction): they go to the model fenced.
"""
import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

KIND_TIER = {'light': 'comfort', 'thermostat': 'comfort', 'fan': 'comfort', 'blinds': 'comfort', 'plug': 'comfort', 'speaker': 'comfort',
             'lock': 'access', 'garage': 'access', 'alarm': 'access', 'camera': 'access',
             'smoke': 'safety', 'co': 'safety', 'leak': 'safety'}
DEFAULT_PARAM = {'thermostat': 'setpoint', 'light': 'brightness', 'fan': 'speed', 'blinds': 'position'}
AUTOMATED = ('packet', 'schedule', 'composer', 'mcp', 'event')
APPROVAL_TTL = 600
HERE = Path(__file__).resolve().parent


def digest(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def hhmm(s):
    m = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', str(s))
    if not m:
        raise ValueError('Times are HH:MM.')
    return int(m.group(1)) * 60 + int(m.group(2))


def in_window(window, minute):
    a, b = (hhmm(x) for x in window.split('-'))
    return a <= minute <= b if a <= b else (minute >= a or minute <= b)


class HomeError(ValueError):
    pass


class HomePolicy:
    def __init__(self, path, adapter=None, telegram=None, labels=None, hard_blocks=HERE / 'policy' / 'hard-blocks.json', now=time.time, local_minute=None):
        self.path, self.adapter, self.now = str(path), adapter, now
        self.telegram = telegram or (lambda text: None)
        self.labels = labels
        self.local_minute = local_minute or (lambda: (lambda t: t.hour * 60 + t.minute)(dt.datetime.now()))
        self.blocks = json.loads(Path(hard_blocks).read_text())['rules']
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS home_settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS home_grants(id TEXT PRIMARY KEY, kind TEXT, param TEXT, lo REAL, hi REAL, enum TEXT, window TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS home_pending(id TEXT PRIMARY KEY, hash TEXT, device TEXT, action TEXT, params TEXT, tier TEXT, created REAL,
                status TEXT, result TEXT);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    # ---------- owner switches ----------
    def is_off(self):
        with self.db() as db:
            r = db.execute("SELECT value FROM home_settings WHERE key='off'").fetchone()
        return bool(r and r['value'] == '1')

    def set_off(self, off):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO home_settings VALUES('off', ?)", ('1' if off else '0',))
            if off:
                db.execute("UPDATE home_pending SET status='cancelled' WHERE status='pending'")
        return self.status()

    def grant(self, kind, param=None, lo=None, hi=None, enum=None, window=None):
        if kind not in KIND_TIER or KIND_TIER[kind] != 'comfort':
            raise HomeError('Standing grants are for comfort devices only (%s). Locks, garage doors, alarms and cameras always ask.' %
                            ', '.join(k for k, t in KIND_TIER.items() if t == 'comfort'))
        param = param or DEFAULT_PARAM.get(kind) or 'power'
        if enum is None and (lo is None or hi is None or lo > hi):
            raise HomeError('Give a range (19-25) or allowed values (on,off).')
        if window:
            for part in window.split('-'):
                hhmm(part)
        gid = 'hg_' + secrets.token_hex(4)
        with self.db() as db:
            db.execute('DELETE FROM home_grants WHERE kind=? AND param=?', (kind, param))
            db.execute('INSERT INTO home_grants VALUES(?,?,?,?,?,?,?,?)', (gid, kind, param, lo, hi, json.dumps(enum) if enum else None, window, self.now()))
        return {'grant': gid, 'kind': kind, 'param': param, 'range': [lo, hi] if enum is None else None, 'values': enum, 'window': window}

    def grants(self):
        with self.db() as db:
            return [{'id': r['id'], 'kind': r['kind'], 'param': r['param'], 'range': [r['lo'], r['hi']] if r['lo'] is not None else None,
                     'values': json.loads(r['enum']) if r['enum'] else None, 'window': r['window']} for r in db.execute('SELECT * FROM home_grants ORDER BY kind')]

    def revoke(self, gid):
        with self.db() as db:
            return {'revoked': db.execute('DELETE FROM home_grants WHERE id=?', (gid,)).rowcount}

    def status(self):
        return {'home': 'off (every physical action refused; /homeon restores)' if self.is_off() else 'on', 'grants': self.grants(),
                'bridge': 'connected' if self.adapter else 'none (no home connector installed)'}

    # ---------- the decision ----------
    def blocked(self, tier, action, origin):
        for r in self.blocks:
            if r.get('tier') == tier and action in r.get('actions', []):
                if 'origins_refused' not in r or origin in r['origins_refused']:
                    return r['reason']
        return None

    def within_grant(self, kind, params, bounds=None):
        minute = self.local_minute()
        gs = [g for g in self.grants() if g['kind'] == kind]
        if not params:
            return False
        for k, v in params.items():
            b = (bounds or {}).get(k)
            if b:
                if 'min' in b and (not isinstance(v, (int, float)) or v < b['min']):
                    return False
                if 'max' in b and (not isinstance(v, (int, float)) or v > b['max']):
                    return False
                if 'enum' in b and v not in b['enum']:
                    return False
            g = next((x for x in gs if x['param'] == k), None)
            if not g:
                return False
            if g['window'] and not in_window(g['window'], minute):
                return False
            if g['values'] is not None:
                if v not in g['values']:
                    return False
            elif not isinstance(v, (int, float)) or isinstance(v, bool) or not g['range'][0] <= v <= g['range'][1]:
                return False
        return True

    def decide(self, device, action, params, origin, bounds=None):
        """{'decision': run|ask|refuse, 'tier', 'reason'}. origin: chat | packet | schedule | composer | mcp | event | owner."""
        kind = device.get('kind')
        tier = KIND_TIER.get(kind)
        if not tier:
            return {'decision': 'refuse', 'tier': None, 'reason': 'Unknown device kind; nothing was done.'}
        if self.is_off():
            return {'decision': 'refuse', 'tier': tier, 'reason': 'Home control is off (/homeoff). Nothing was done; /homeon turns it back on.'}
        why = self.blocked(tier, action, origin)
        if why:
            return {'decision': 'refuse', 'tier': tier, 'reason': why, 'hard_block': True}
        if origin == 'event':
            return {'decision': 'refuse', 'tier': tier, 'reason': 'An incoming event never controls a device. Nothing was done.'}
        if tier == 'access':
            if origin in AUTOMATED:
                return {'decision': 'refuse', 'tier': tier, 'reason': 'Locks, garage doors, alarms and cameras need your live approval; a %s cannot do it. Nothing was done.' % origin}
            return {'decision': 'ask', 'tier': tier, 'reason': 'Access action: your approval in Telegram every time.'}
        if tier == 'safety':
            if origin in AUTOMATED:
                return {'decision': 'refuse', 'tier': tier, 'reason': 'Safety devices are changed only with your live approval. Nothing was done.'}
            return {'decision': 'ask', 'tier': tier, 'reason': 'Safety device: your approval in Telegram.'}
        if self.within_grant(kind, params, bounds):
            return {'decision': 'run', 'tier': tier, 'reason': 'Inside your standing grant.'}
        if origin in ('schedule', 'composer', 'mcp'):
            return {'decision': 'refuse', 'tier': tier, 'reason': 'Outside your standing grant, and a %s cannot ask for more. Nothing was done.' % origin}
        return {'decision': 'ask', 'tier': tier, 'reason': 'Outside your standing grant (/homegrant): your approval in Telegram.'}

    # ---------- running ----------
    @staticmethod
    def settle(expected, state):
        if state is None:
            return 'unconfirmed'
        return 'confirmed' if all(state.get(k) == v for k, v in expected.items()) else 'failed'

    def execute(self, device, action, params):
        if not self.adapter:
            return {'status': 'unreachable', 'note': 'No home bridge is connected; nothing was sent or queued.'}
        try:
            expected = self.adapter.command(device['id'], action, params)
        except ConnectionError:
            return {'status': 'unreachable', 'note': 'The home bridge did not answer; nothing was queued. Try again later.'}
        try:
            state = self.adapter.state(device['id'])
        except Exception:
            state = None
        outcome = self.settle(expected, state)
        note = {'confirmed': 'The device reports the new state.', 'unconfirmed': 'Sent, but the device did not report its state: NOT confirmed. Check it before assuming it happened.',
                'failed': 'The device reports a different state: it did not happen.'}[outcome]
        return {'status': outcome, 'note': note}

    def command(self, device, action, params, origin, bounds=None):
        d = self.decide(device, action, params, origin, bounds)
        if d['decision'] == 'refuse':
            return {'status': 'refused', **d}
        if d['decision'] == 'run':
            return {**self.execute(device, action, params), 'tier': d['tier'], 'ran': 'inside your standing grant'}
        pid = 'ha_' + secrets.token_hex(6)
        h = digest({'id': pid, 'device': device['id'], 'action': action, 'params': params})
        with self.db() as db:
            db.execute('INSERT INTO home_pending VALUES(?,?,?,?,?,?,?,?,NULL)', (pid, h, device['id'], action, json.dumps(params), d['tier'], self.now(), 'pending'))
        args = ', '.join('%s=%s' % (k, v) for k, v in sorted(params.items())) or 'none'
        self.telegram('HOME ACTION REVIEW — %s\nDevice: %s (%s, %s tier)\nAction: %s\nSettings: %s\n\nApprove within 10 minutes by sending this ENTIRE line:\n/homeapprove %s %s\nIgnore it to cancel.'
                      % (pid, str(device.get('name', ''))[:60].replace('\n', ' '), device['kind'], d['tier'], action, args, pid, h[:16]))
        return {'status': 'awaiting_approval', 'id': pid, 'tier': d['tier'], 'note': 'Sent to the owner for approval; nothing happened yet.'}

    def approve(self, pid, h, device_lookup):
        with self.db() as db:
            p = db.execute('SELECT * FROM home_pending WHERE id=?', (str(pid),)).fetchone()
        if not p or not isinstance(h, str) or h != p['hash'][:16]:
            raise HomeError('Approval does not match this exact home action.')
        if p['status'] != 'pending':
            return {'id': pid, 'status': p['status'], 'already': True, 'result': json.loads(p['result']) if p['result'] else None}
        if self.now() - p['created'] > APPROVAL_TTL:
            raise HomeError('This approval expired (10 minutes). Ask again.')
        if self.is_off():
            raise HomeError('Home control is off (/homeoff). Nothing was done.')
        device = device_lookup(p['device'])
        why = self.blocked(p['tier'], p['action'], 'owner')
        if why:
            raise HomeError(why)
        with self.db() as db:
            db.execute("UPDATE home_pending SET status='running' WHERE id=?", (pid,))
        result = self.execute(device, p['action'], json.loads(p['params']))
        with self.db() as db:
            db.execute('UPDATE home_pending SET status=?, result=? WHERE id=?', (result['status'], json.dumps(result), pid))
        return {'id': pid, **result}
