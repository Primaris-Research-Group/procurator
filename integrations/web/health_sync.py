"""Apple Health companion — server side (web 0.23.0; build-plans/health/05-apple-health.md).

The iPhone app (integrations/apple-health/) runs the HealthKit queries; this server only receives what the owner chose to share.

Pairing. The owner sends /healthpair in Telegram (optionally naming the types). The broker makes a single-use code (10 minutes; only its
hash is stored) and the address to type into the app. The app calls POST /assistant/dashboard/health/pair {code, device_name}; the
answer is a per-device credential (shown once, kept in the iOS Keychain; only its hash is stored here), the installation name and the
accepted types — the app shows that destination before it sends anything. Five wrong codes in an hour cancel every open code.
/healthdevices lists devices; /healthdevices revoke ID [delete] stops a device (and optionally deletes what it sent).

Sync contract (envelope version 1), POST /assistant/dashboard/health/sync with `Authorization: Bearer <device credential>`:
  {"version": 1, "batch_id": "<stable per batch>", "sequence": <per-device increasing integer>,
   "samples":   [{"type": "body_mass"|"height", "source_id": "<HealthKit UUID>", "start": ISO, "end": ISO, "tz": "America/New_York",
                  "value": 81.2, "unit": "kg"|"lb"|"cm"|"m"|"in", "source_app": "...", "source_device": "..."}],
   "deletions": [{"type": "body_mass", "source_id": "<UUID>"}],
   "statistics":[{"type": "step_count"|"active_energy", "date": "YYYY-MM-DD", "tz": "...", "value": 8421, "unit": "count"|"kcal"|"kJ",
                  "method": "HKStatisticsCollectionQuery cumulativeSum"}]}
  * A batch id is idempotent: the same id and content returns the same acknowledgement; the same id with other content is refused.
    The app advances its HealthKit anchor only after the acknowledgement.
  * Samples upsert by HealthKit UUID (source_id), so a reinstall, a resync or a second device never duplicates history. Deletions are
    tombstones: a sample re-sent by an OLDER batch (lower sequence) after its deletion stays deleted.
  * Daily steps / active energy come from HealthKit statistics (which already merge phone and watch); raw samples are never summed here.
  * Imported weights never replace manual ones; both keep their source.
"""
import datetime as dt
import hashlib
import json
import re
import secrets

from health import canonical, clean

TYPES = ('body_mass', 'height', 'step_count', 'active_energy', 'basal_energy', 'exercise_minutes')   # 0.26.0: + resting energy, exercise minutes
SAMPLE_TYPES = {'body_mass': 'weight', 'height': 'height'}
STAT_TYPES = {'step_count': ('count',), 'active_energy': ('kcal', 'kJ'), 'basal_energy': ('kcal', 'kJ'), 'exercise_minutes': ('min',)}
CODE_SECONDS = 600
DEVICE_DAYS = 365
MAX_ITEMS = 2000
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$')
UUID = re.compile(r'^[A-Za-z0-9-]{8,64}$')


class SyncError(Exception):
    def __init__(self, status, kind, detail=''):
        super().__init__(kind)
        self.status, self.kind, self.detail = status, kind, detail


def h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()


class HealthSync:
    def __init__(self, health, nutrition, installation='PROCURATOR', public_base=''):
        self.h, self.n = health, nutrition
        self.installation, self.public_base = installation, public_base.rstrip('/')
        with self.h.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sync_codes(code_hash TEXT PRIMARY KEY,types TEXT,created REAL,expires REAL,used REAL);
                CREATE TABLE IF NOT EXISTS sync_failures(at REAL);
                CREATE TABLE IF NOT EXISTS sync_devices(id TEXT PRIMARY KEY,name TEXT,token_hash TEXT UNIQUE,types TEXT,created REAL,expires REAL,revoked REAL,
                    last_sync REAL,last_batch TEXT,last_sequence INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sync_batches(batch_id TEXT,device TEXT,hash TEXT,received REAL,ack TEXT,PRIMARY KEY(device,batch_id));
                CREATE TABLE IF NOT EXISTS sync_tombstones(source_id TEXT PRIMARY KEY,type TEXT,device TEXT,sequence INTEGER,at REAL);
                CREATE TABLE IF NOT EXISTS daily_stats(date TEXT,type TEXT,device TEXT,value REAL,unit TEXT,tz TEXT,method TEXT,updated REAL,PRIMARY KEY(date,type,device));''')

    # ---------- pairing ----------
    def pair_code(self, a):
        types = a.get('types') or list(TYPES)
        if isinstance(types, str):
            types = [t.strip() for t in types.replace(',', ' ').split() if t.strip()]
        bad = [t for t in types if t not in TYPES]
        if bad or not types:
            raise ValueError('types are: ' + ', '.join(TYPES))
        alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        code = '-'.join(''.join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
        now = self.h.now()
        with self.h.db() as db:
            db.execute('DELETE FROM sync_codes WHERE expires<?', (now - 86400,))
            db.execute('INSERT INTO sync_codes VALUES(?,?,?,?,NULL)', (h(code), json.dumps(types), now, now + CODE_SECONDS))
        return {'code': code, 'server': self.public_base + '/assistant/dashboard', 'types': types, 'expires_minutes': CODE_SECONDS // 60,
                'say': 'In the PROCURATOR Health app: enter the server address and this code within 10 minutes. The code works once.'}

    def pair(self, body):
        try:
            a = json.loads(body or b'{}')
        except ValueError:
            raise SyncError(400, 'bad_json')
        code = str(a.get('code') or '').strip().upper()
        name = clean(str(a.get('device_name') or 'iPhone'), 'device_name', 60)
        now = self.h.now()
        with self.h.db() as db:
            fails = db.execute('SELECT COUNT(*) FROM sync_failures WHERE at>?', (now - 3600,)).fetchone()[0]
            if fails >= 5:
                raise SyncError(429, 'too_many_attempts', 'ask for a new code with /healthpair later')
            c = db.execute('SELECT * FROM sync_codes WHERE code_hash=?', (h(code),)).fetchone()
            bad = not c or c['used'] or c['expires'] < now
            if bad:
                db.execute('INSERT INTO sync_failures VALUES(?)', (now,))
                if fails + 1 >= 5:
                    db.execute('UPDATE sync_codes SET used=? WHERE used IS NULL', (now,))       # cancel every open code
        if bad:      # raised after the block so the failure is committed
            raise SyncError(401, 'bad_code', 'that code is wrong, used or expired; send /healthpair for a new one')
        with self.h.db() as db:
            db.execute('UPDATE sync_codes SET used=? WHERE code_hash=?', (now, c['code_hash']))
            did, token = secrets.token_hex(8), 'phd_' + secrets.token_urlsafe(32)
            db.execute('INSERT INTO sync_devices(id,name,token_hash,types,created,expires) VALUES(?,?,?,?,?,?)', (did, name, h(token), c['types'], now, now + DEVICE_DAYS * 86400))
        return {'device_id': did, 'credential': token, 'installation': self.installation, 'types': json.loads(c['types']), 'envelope_version': 1,
                'credential_expires_days': DEVICE_DAYS}

    def device_for(self, headers):
        auth = next((v for k, v in headers.items() if k.lower() == 'authorization'), '')
        if not auth.startswith('Bearer phd_'):
            raise SyncError(401, 'no_credential')
        with self.h.db() as db:
            d = db.execute('SELECT * FROM sync_devices WHERE token_hash=?', (h(auth[7:]),)).fetchone()
        if not d:
            raise SyncError(401, 'unknown_device', 'pair again with /healthpair')
        if d['revoked']:
            raise SyncError(401, 'revoked', 'this device was disconnected')
        if d['expires'] < self.h.now():
            raise SyncError(401, 'expired', 'the device credential expired; pair again with /healthpair')
        return dict(d)

    # ---------- ingest ----------
    def sync(self, headers, body):
        d = self.device_for(headers)
        if len(body or b'') > 2_000_000:
            raise SyncError(413, 'too_large', 'send at most 2 MB per batch')
        try:
            a = json.loads(body or b'{}')
        except ValueError:
            raise SyncError(400, 'bad_json')
        if a.get('version') != 1:
            raise SyncError(400, 'unsupported_version', 'envelope version 1 is supported')
        bid = str(a.get('batch_id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,100}', bid):
            raise SyncError(400, 'bad_batch_id')
        seq = a.get('sequence')
        if type(seq) is not int or seq < 0:
            raise SyncError(400, 'bad_sequence', 'sequence is a per-device increasing integer')
        samples, deletions, stats = a.get('samples') or [], a.get('deletions') or [], a.get('statistics') or []
        if not all(isinstance(x, list) for x in (samples, deletions, stats)) or len(samples) + len(deletions) + len(stats) > MAX_ITEMS:
            raise SyncError(400, 'bad_batch', 'samples, deletions and statistics are lists of at most %d items in total' % MAX_ITEMS)
        digest = h(canonical({k: a.get(k) for k in ('sequence', 'samples', 'deletions', 'statistics')}))
        with self.h.db() as db:
            prev = db.execute('SELECT * FROM sync_batches WHERE device=? AND batch_id=?', (d['id'], bid)).fetchone()
        if prev:
            if prev['hash'] != digest:
                raise SyncError(409, 'batch_id_reused', 'this batch id was already used for different content')
            return json.loads(prev['ack']) | {'duplicate': True}
        allowed = set(json.loads(d['types']))
        out = {'batch_id': bid, 'accepted': 0, 'deleted': 0, 'statistics': 0, 'ignored': [], 'stale_after_delete': 0}
        for s in samples:
            why = self.take_sample(d, s, seq, allowed)
            if why == 'ok':
                out['accepted'] += 1
            elif why == 'tombstoned':
                out['stale_after_delete'] += 1
            else:
                out['ignored'].append({'source_id': str(s.get('source_id'))[:64] if isinstance(s, dict) else None, 'why': why})
        for x in deletions:
            if not isinstance(x, dict) or not UUID.match(str(x.get('source_id') or '')) or x.get('type') not in SAMPLE_TYPES:
                out['ignored'].append({'deletion': True, 'why': 'bad deletion'})
                continue
            sid = 'hk:' + x['source_id']
            with self.h.db() as db:
                db.execute('INSERT INTO sync_tombstones VALUES(?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET sequence=MAX(sequence,excluded.sequence),at=excluded.at',
                           (sid, x['type'], d['id'], seq, self.h.now()))
            out['deleted'] += self.n.delete_measurement(source_id=sid)['deleted']
        for s in stats:
            why = self.take_stat(d, s, allowed)
            if why == 'ok':
                out['statistics'] += 1
            else:
                out['ignored'].append({'statistic': True, 'why': why})
        out['ignored'] = out['ignored'][:50]
        out['ack'] = True
        with self.h.db() as db:
            db.execute('INSERT INTO sync_batches VALUES(?,?,?,?,?)', (bid, d['id'], digest, self.h.now(), json.dumps(out)))
            db.execute('UPDATE sync_devices SET last_sync=?,last_batch=?,last_sequence=MAX(last_sequence,?) WHERE id=?', (self.h.now(), bid, seq, d['id']))
        return out

    def take_sample(self, d, s, seq, allowed):
        if not isinstance(s, dict):
            return 'not an object'
        t = s.get('type')
        if t not in SAMPLE_TYPES:
            return 'unknown type'
        if t not in allowed:
            return 'type not shared from this device'
        if not UUID.match(str(s.get('source_id') or '')):
            return 'bad source_id'
        start = str(s.get('start') or s.get('end') or '')
        if not ISO.match(start):
            return 'bad time'
        sid = 'hk:' + s['source_id']
        with self.h.db() as db:
            tomb = db.execute('SELECT sequence FROM sync_tombstones WHERE source_id=?', (sid,)).fetchone()
        if tomb and tomb['sequence'] >= seq:
            return 'tombstoned'
        if tomb:
            with self.h.db() as db:
                db.execute('DELETE FROM sync_tombstones WHERE source_id=?', (sid,))
        try:
            self.n.add_measurement({'kind': SAMPLE_TYPES[t], 'value': s.get('value'), 'unit': s.get('unit'), 'measured_at': start[:25], 'source_id': sid,
                                    'device': d['id']}, source='apple_health')
        except ValueError as e:
            return str(e)[:120]
        return 'ok'

    def take_stat(self, d, s, allowed):
        if not isinstance(s, dict) or s.get('type') not in STAT_TYPES:
            return 'unknown statistic'
        if s['type'] not in allowed:
            return 'type not shared from this device'
        date = str(s.get('date') or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
            return 'bad date'
        try:
            v = float(s.get('value'))
        except (TypeError, ValueError):
            return 'bad value'
        unit = s.get('unit')
        if unit not in STAT_TYPES[s['type']] or v < 0 or v > {'step_count': 200000, 'exercise_minutes': 1440}.get(s['type'], 40000):
            return 'bad unit or value'
        if unit == 'kJ':
            v, unit = v / 4.184, 'kcal'
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO daily_stats VALUES(?,?,?,?,?,?,?,?)', (date, s['type'], d['id'], round(v, 2), unit, clean(str(s.get('tz') or ''), 'tz', 40, required=False),
                       clean(str(s.get('method') or 'statistics'), 'method', 80), self.h.now()))
        return 'ok'

    # ---------- owner views ----------
    def devices(self):
        with self.h.db() as db:
            rows = db.execute('SELECT id,name,types,created,expires,revoked,last_sync,last_sequence FROM sync_devices ORDER BY created').fetchall()
        now = self.h.now()
        return {'devices': [{'id': r['id'], 'name': r['name'], 'types': json.loads(r['types']), 'status': 'revoked' if r['revoked'] else 'expired' if r['expires'] < now else 'active',
                             'last_sync_hours_ago': round((now - r['last_sync']) / 3600, 1) if r['last_sync'] else None,
                             'stale': bool(r['last_sync'] and now - r['last_sync'] > 2 * 86400 and not r['revoked'])} for r in rows]}

    def revoke(self, a):
        did = clean(a.get('device'), 'device', 16)
        with self.h.db() as db:
            n = db.execute('UPDATE sync_devices SET revoked=? WHERE id=? AND revoked IS NULL', (self.h.now(), did)).rowcount
            removed = 0
            # 0.25.0 (H4): disconnecting deletes what the device sent, unless the owner asks to keep it (/healthdevices revoke ID keep).
            if not a.get('keep_data'):
                removed += db.execute('DELETE FROM daily_stats WHERE device=?', (did,)).rowcount
                removed += db.execute("UPDATE measurements SET deleted=1 WHERE source='apple_health' AND device=? AND deleted=0", (did,)).rowcount
                db.execute('DELETE FROM sync_tombstones WHERE device=?', (did,))
                db.execute('DELETE FROM sync_batches WHERE device=?', (did,))
        if not n:
            raise ValueError('No active device %s.' % did)
        return {'device': did, 'revoked': True, 'deleted_records': removed, 'kept_data': bool(a.get('keep_data'))}

    def activity(self, a):
        days = min(max(int(a.get('days') or 14), 1), 120)
        end = dt.date.fromisoformat(clean(a.get('to') or self.n.today(), 'to', 10))
        start = (end - dt.timedelta(days=days - 1)).isoformat()
        with self.h.db() as db:
            rows = db.execute('SELECT * FROM daily_stats WHERE date BETWEEN ? AND ? ORDER BY date, updated DESC', (start, end.isoformat())).fetchall()
        out = {}
        for r in rows:          # one device's value per day and type: the most recently updated (statistics already merge phone + watch)
            out.setdefault(r['date'], {}).setdefault(r['type'], {'value': r['value'], 'unit': r['unit'], 'device': r['device']})
        return {'from': start, 'to': end.isoformat(), 'days': out,
                'note': 'Active energy is shown for information. It is NOT added to the calorie target: the activity level already counts activity.'}

    def call(self, action, a, privileged=False):
        if action == 'health_sync_pair_code':
            if not privileged:
                raise ValueError('Only the owner pairs a phone (/healthpair).')
            return self.pair_code(a)
        if action == 'health_sync_devices':
            return self.devices()
        if action == 'health_sync_revoke':
            if not privileged:
                raise ValueError('Only the owner disconnects a device (/healthdevices revoke ID).')
            return self.revoke(a)
        if action == 'health_sync_activity':
            return self.activity(a)
        raise ValueError('Unknown sync action.')
