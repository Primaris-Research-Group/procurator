"""Consumer health module — shared foundation (web 0.21.0, Nov 7 2026; build-plans/health/01-shared-foundation.md).

One permission-and-outcome system for every health operation (appointments, pharmacy, portal reads, nutrition logs):

  * Subjects: who receives care. The owner acting for themselves is the only subject accepted today; caregiver subjects
    exist in the schema and are refused until their own increment (an explicit grant model, never the owner's identity).
  * Connections: an account at a provider/institution (portal, pharmacy, clinic site, companion app). Tokens live in
    restricted files under health-tokens/ (mode 600) and never enter tool results, packets, logs or model context: the
    connection row keeps a token *reference* only.
  * Grants: owner-approved authority with a revision, exact action set, connection + subject, constraints, disclosures and
    validity. The model can only PROPOSE a grant (a dry run: candidate actions, unresolved facts, expected disclosures);
    the owner approves it with a hashed Telegram line (/healthgrant ID HASH), the same way /webtask works. A revision
    supersedes queued work bound to the old authority; revocation stops queued work and flags dispatched work for
    reconciliation. External pages, callers, records and model assertions cannot create or widen a grant.
  * Effects: every consequential operation is persisted BEFORE dispatch, bound to a stable request_id, a canonical payload
    hash, subject, connection, grant revision and workflow step. A reused request_id with different content is rejected.
    An equivalent operation (same subject, kind and target) already in flight or confirmed stops as awaiting_owner.
    Lifecycle: draft → authorized → queued → dispatching → confirmed, with awaiting_owner / failed / cancelled / uncertain.
    Attempted, provider-accepted and confirmed are recorded separately with their source and time. After an ambiguous
    result the effect is `uncertain` and the next step is a READ of provider state (reconcile), never a retry.
  * Policy: recall off for health turns by default, minimal notification text with a private receipt, retention.
  * Receipts: private detail pages (single-use links like the sign-in pages); Telegram gets one neutral line.

Authority checks happen here, on the broker side of the tool boundary; prompts only describe them.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ACTIONS = ('appointment.search', 'appointment.book', 'appointment.reschedule', 'appointment.cancel',
           'rx.status', 'rx.refill', 'portal.read', 'health.log', 'health.sync')
EFFECT_KINDS = {'appointment.book': 'appointment', 'appointment.reschedule': 'appointment', 'appointment.cancel': 'appointment',
                'rx.refill': 'refill', 'health.log': 'log', 'health.sync': 'sync'}
CONSTRAINT_KEYS = ('clinician', 'provider', 'specialty', 'visit_type', 'locations', 'time_windows', 'weekdays', 'time_of_day', 'spend_cap_usd',
                   'cancellation_policy', 'reschedule_order', 'prescriptions', 'pharmacy', 'fulfillment')   # = CONSTRAINT_KEYS in web-plugin/health.mjs
READ_ACTIONS = ('appointment.search', 'rx.status', 'portal.read')
STATES = ('draft', 'authorized', 'queued', 'dispatching', 'confirmed', 'awaiting_owner', 'failed', 'cancelled', 'uncertain')
SETTLE = {'confirmed', 'failed', 'uncertain', 'cancelled', 'requested', 'waitlisted', 'held'}
PROVIDERS = ('clinic_web', 'clinic_phone', 'epic', 'oracle', 'mock_ehr', 'mock_pharmacy', 'cvs', 'walgreens', 'apple_health', 'fdc', 'manual')
ENVIRONMENTS = ('production', 'sandbox', 'fake')
CONNECTION_STATUS = ('pending', 'connected', 'reauth_required', 'revoked', 'retired')
POLICY_DEFAULTS = {'recall': 'off', 'notify_detail': 'minimal', 'retention_days': 365, 'cloud_models': 'default', 'caregivers': 'off', 'calendar_detail': 'minimal',
                   'adult_confirmed': 'no'}
ID = re.compile(r'^[a-f0-9]{16}$')
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_:.-]{7,99}$')
# What may never be stored in a grant, payload or receipt as text (the vault holds identity values; tokens stay in files).
SECRET_SHAPES = re.compile(r'(?i)\b(?:password|passcode|otp|one[- ]time code|bearer\s+[A-Za-z0-9._~+/=-]{20,}|refresh_token|access_token)\b|'
                           r'(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])|(?<![\w-])(?:\d[ -]?){13,19}(?![\w-])')
LEASE_SECONDS = 600
# 0.25.0 (H2): reading stored health data asks first, unless the owner allowed that kind of read for a while (/healthread).
READ_CATEGORIES = {'activity': 'Apple Health activity and measurements', 'nutrition': 'food log and nutrition history',
                   'appointments': 'health appointments on record', 'wellness': 'wellness coach metrics (blood pressure, sleep, stress, exercise)'}
READ_GATED = {'health_sync_activity': 'activity', 'health_nutrition_day': 'nutrition', 'health_nutrition_history': 'nutrition',
              'health_nutrition_trends': 'nutrition', 'health_nutrition_estimate': 'nutrition', 'health_bookings': 'appointments',
              'health_coach_balance': 'nutrition', 'health_coach_metrics': 'wellness'}   # 0.26.0: the wellness coach
# 0.25.0 (H8): the health module works only after the owner confirms once that the owner is 18 or older (/healthadult).
ADULT_FREE = ('health_status', 'health_policy', 'health_adult_confirm', 'health_receipt_read', 'health_prune', 'health_read_allow',
              'health_taint_check', 'health_connection_status', 'health_forget_connection', 'health_sync_revoke')
PHONE_SOURCE = re.compile(r'(?i)phone|call|transcript|voicemail|receptionist')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def payload_hash(payload):
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def clean(value, name, limit, required=True):
    if value is None or value == '':
        if required:
            raise ValueError('%s is required.' % name)
        return ''
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('%s must be text of at most %d characters.' % (name, limit))
    return ' '.join(value.split())


def no_secrets(text, where):
    if SECRET_SHAPES.search(str(text or '')):
        raise ValueError('%s looks like it contains a credential, code, card or identity number; those never go into health records. '
                         'Use {{owner.*}} facts for identity values and the server-side token store for credentials.' % where)


def target_key(kind, payload, action=None):
    """The normalized identity of what an effect does, for the equivalence check (same booking / same refill)."""
    p = payload or {}
    if action == 'appointment.cancel':
        # A cancellation is about one existing appointment, not about a visit: two cancels of it are equivalent; a cancel never
        # matches the booking it cancels.
        return 'appointment-cancel:' + str(p.get('appointment_id') or p.get('of_effect') or canonical(p)).lower()
    if kind == 'appointment':
        parts = [p.get('clinician') or p.get('specialty') or '', p.get('location') or '', p.get('date') or p.get('window') or '', p.get('visit_type') or '']
    elif kind == 'refill':
        parts = [p.get('prescription_id') or '', p.get('pharmacy') or p.get('store') or '']
    elif kind == 'log':
        parts = [p.get('entry_id') or p.get('time') or '']
    elif kind == 'sync':
        parts = [p.get('batch_id') or '']
    else:
        parts = [canonical(p)]
    return kind + ':' + '|'.join(' '.join(str(x).lower().split()) for x in parts)


class Health:
    def __init__(self, broker, state_dir, now=time.time):
        self.b, self.now = broker, now
        self.dir = Path(state_dir)
        self.path = self.dir / 'health.sqlite3'
        self.tokens = self.dir / 'health-tokens'
        self.evidence = self.dir / 'health-evidence'
        for d in (self.tokens, self.evidence):
            try:
                d.mkdir(mode=0o700, exist_ok=True)
            except OSError:
                pass
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS subjects(id TEXT PRIMARY KEY,owner TEXT,display_name TEXT,relation TEXT,status TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY,owner TEXT,subject TEXT,provider TEXT,institution TEXT,environment TEXT,
                    endpoint TEXT,scopes TEXT,capabilities TEXT,status TEXT,token_ref TEXT,created REAL,updated REAL,note TEXT);
                CREATE TABLE IF NOT EXISTS grant_proposals(id TEXT PRIMARY KEY,hash TEXT,subject TEXT,connection TEXT,actions TEXT,constraints TEXT,
                    disclosures TEXT,valid_days INTEGER,title TEXT,dry_run TEXT,unresolved TEXT,created REAL,status TEXT,grant_id TEXT);
                CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,revision INTEGER,subject TEXT,connection TEXT,actions TEXT,constraints TEXT,
                    disclosures TEXT,title TEXT,valid_from REAL,valid_to REAL,status TEXT,approved_hash TEXT,created REAL,updated REAL);
                CREATE TABLE IF NOT EXISTS grant_history(grant_id TEXT,revision INTEGER,actions TEXT,constraints TEXT,disclosures TEXT,valid_to REAL,
                    status TEXT,at REAL,why TEXT);
                CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,payload_hash TEXT,subject TEXT,connection TEXT,grant_id TEXT,
                    grant_revision INTEGER,action TEXT,kind TEXT,target TEXT,step TEXT,state TEXT,provider_state TEXT,payload TEXT,created REAL,
                    attempted_at REAL,provider_accepted_at REAL,confirmed_at REAL,settled_at REAL,source TEXT,external_id TEXT,receipt_ref TEXT,
                    lease_until REAL,note TEXT,duplicate_of TEXT);
                CREATE TABLE IF NOT EXISTS effect_events(effect_id TEXT,at REAL,state TEXT,source TEXT,detail TEXT);
                CREATE TABLE IF NOT EXISTS policy(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,effect_id TEXT,created REAL,body TEXT,page_hash TEXT,expires REAL,used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS read_scopes(category TEXT PRIMARY KEY,until REAL,once INTEGER DEFAULT 0,updated REAL);
                CREATE TABLE IF NOT EXISTS read_requests(id TEXT PRIMARY KEY,category TEXT,created REAL,status TEXT);
                CREATE TABLE IF NOT EXISTS visit_notes(effect_id TEXT PRIMARY KEY,questions TEXT,bring TEXT,updated REAL);
            ''')
            cols = {r[1] for r in db.execute('PRAGMA table_info(connections)')}
            if 'contacts' not in cols:
                db.execute("ALTER TABLE connections ADD COLUMN contacts TEXT DEFAULT '[]'")   # 0.25.0 (H6)
            if not db.execute("SELECT 1 FROM subjects WHERE relation='self'").fetchone():
                db.execute('INSERT INTO subjects VALUES(?,?,?,?,?,?)', (secrets.token_hex(8), 'owner', 'Owner (self)', 'self', 'active', self.now()))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # ---------- policy ----------
    def policy(self):
        out = dict(POLICY_DEFAULTS)
        with self.db() as db:
            for r in db.execute('SELECT key,value FROM policy'):
                try:
                    out[r['key']] = json.loads(r['value'])
                except ValueError:
                    out[r['key']] = r['value']
        return out

    def set_policy(self, key, value):
        if key not in POLICY_DEFAULTS:
            raise ValueError('Policy keys: ' + ', '.join(POLICY_DEFAULTS))
        if key == 'recall' and value not in ('on', 'off'):
            raise ValueError('recall is on or off.')
        if key == 'notify_detail' and value not in ('minimal', 'normal'):
            raise ValueError('notify_detail is minimal or normal.')
        if key == 'retention_days' and (type(value) is not int or not 30 <= value <= 3650):
            raise ValueError('retention_days is 30 to 3650.')
        if key == 'cloud_models' and value not in ('default', 'none'):
            raise ValueError('cloud_models is default or none.')
        if key == 'caregivers' and value not in ('on', 'off'):
            raise ValueError('caregivers is on or off.')
        if key == 'adult_confirmed' and value not in ('yes', 'no'):
            raise ValueError('adult_confirmed is yes or no (/healthadult).')
        if key == 'calendar_detail' and value not in ('minimal', 'clinic', 'full'):
            raise ValueError('calendar_detail is minimal ("Appointment"), clinic (adds the clinic) or full (adds clinician and visit type).')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO policy VALUES(?,?)', (key, json.dumps(value)))
        return self.policy()

    # ---------- subjects / connections ----------
    def subject(self, db, subject_id=None):
        if subject_id in (None, '', 'self'):
            r = db.execute("SELECT * FROM subjects WHERE relation='self'").fetchone()
        else:
            r = db.execute('SELECT * FROM subjects WHERE id=?', (subject_id,)).fetchone()
        if not r:
            raise ValueError('Unknown subject.')
        if r['relation'] != 'self' and self.policy().get('caregivers') != 'on':
            raise ValueError('Caregiver subjects are not enabled yet; only the owner acting for themselves is supported.')
        return dict(r)

    def connection(self, db, connection_id, need_active=False):
        r = db.execute('SELECT * FROM connections WHERE id=?', (connection_id,)).fetchone()
        if not r:
            raise ValueError('Unknown health connection.')
        c = dict(r)
        if need_active and c['status'] not in ('connected', 'pending'):
            raise ValueError('Connection %s is %s; reconnect it before using it.' % (c['id'], c['status']))
        return c

    def public_connection(self, c):
        return {k: (json.loads(c[k]) if k in ('scopes', 'capabilities') and c[k] else c[k]) for k in
                ('id', 'subject', 'provider', 'institution', 'environment', 'endpoint', 'status', 'created', 'updated', 'note') + ('scopes', 'capabilities')} | \
               {'has_token': bool(c.get('token_ref')), 'contacts': json.loads(c.get('contacts') or '[]')}

    def add_connection(self, a):
        provider = clean(a.get('provider'), 'provider', 40).lower()
        if provider not in PROVIDERS:
            raise ValueError('provider is one of: ' + ', '.join(PROVIDERS))
        env = clean(a.get('environment') or 'production', 'environment', 20).lower()
        if env not in ENVIRONMENTS:
            raise ValueError('environment is production, sandbox or fake.')
        institution = clean(a.get('institution'), 'institution', 120)
        endpoint = clean(a.get('endpoint') or '', 'endpoint', 500, required=False)
        if endpoint and not re.match(r'^https://[a-z0-9.-]+(?::\d+)?(?:/\S*)?$', endpoint, re.I) and env != 'fake' and not (provider == 'clinic_phone' and re.match(r'^tel:\+1\d{10}$', endpoint)):
            raise ValueError('endpoint must be an https:// address (a clinic_phone connection: tel:+1 and ten digits).')
        # 0.25.0 (H6): other recipients this institution uses (scheduling email, fax-to-email, second phone, booking site). Text that
        # carries health details may go to the connection's endpoint and these, under an active grant, and to no one else.
        contacts = a.get('contacts') or []
        if not isinstance(contacts, list) or len(contacts) > 10 or any(not isinstance(x, str) or not re.match(
                r'^(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+1\d{10}|https://[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?)$', x.strip()) for x in contacts):
            raise ValueError('contacts is a list of up to 10 recipients: an email address, a +1 phone number or an https:// site.')
        scopes = a.get('scopes') or []
        caps = a.get('capabilities') or {}
        if not isinstance(scopes, list) or len(scopes) > 30 or any(not isinstance(s, str) or len(s) > 120 for s in scopes):
            raise ValueError('scopes is a list of up to 30 short strings.')
        if not isinstance(caps, dict) or len(caps) > 40:
            raise ValueError('capabilities is an object of operation -> supported|unsupported|unknown.')
        for k, v in caps.items():
            if v not in ('supported', 'unsupported', 'unknown'):
                raise ValueError('capability %s must be supported, unsupported or unknown.' % k)
        note = clean(a.get('note') or '', 'note', 300, required=False)
        no_secrets(canonical([institution, endpoint, scopes, caps, note]), 'connection')
        with self.db() as db:
            subject = self.subject(db, a.get('subject'))
            cid = secrets.token_hex(8)
            db.execute('INSERT INTO connections(id,owner,subject,provider,institution,environment,endpoint,scopes,capabilities,status,token_ref,created,updated,note,contacts) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (cid, 'owner', subject['id'], provider, institution, env, endpoint,
                       json.dumps(scopes), json.dumps(caps), 'pending', None, self.now(), self.now(), note, json.dumps([x.strip() for x in contacts])))
            return self.public_connection(self.connection(db, cid))

    def set_token(self, connection_id, token_json):
        """Server-side only (bin/set-health-token.sh): stores the token file and marks the connection connected."""
        with self.db() as db:
            c = self.connection(db, connection_id)
            ref = 'tok_' + secrets.token_hex(8)
            path = self.tokens / (ref + '.json')
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
                f.write(json.dumps(token_json))
            if c.get('token_ref'):
                try:
                    (self.tokens / (c['token_ref'] + '.json')).unlink()
                except OSError:
                    pass
            db.execute("UPDATE connections SET token_ref=?,status='connected',updated=? WHERE id=?", (ref, self.now(), connection_id))
        return {'id': connection_id, 'status': 'connected'}

    def token(self, connection_id):
        """Adapters read the token here; it is never returned through call()."""
        with self.db() as db:
            c = self.connection(db, connection_id)
        if not c.get('token_ref'):
            return None
        try:
            return json.loads((self.tokens / (c['token_ref'] + '.json')).read_text())
        except (OSError, ValueError):
            return None

    def set_connection_status(self, connection_id, status, why=''):
        if status not in CONNECTION_STATUS:
            raise ValueError('status is one of: ' + ', '.join(CONNECTION_STATUS))
        with self.db() as db:
            c = self.connection(db, connection_id)
            db.execute('UPDATE connections SET status=?,updated=?,note=? WHERE id=?', (status, self.now(), clean(why or c.get('note') or '', 'note', 300, required=False), connection_id))
            if status in ('revoked', 'retired'):
                if c.get('token_ref'):
                    try:
                        (self.tokens / (c['token_ref'] + '.json')).unlink()
                    except OSError:
                        pass
                    db.execute('UPDATE connections SET token_ref=NULL WHERE id=?', (connection_id,))
                # Grants on a revoked connection stop with it; queued effects are cancelled, dispatching ones must be reconciled.
                for g in db.execute("SELECT id FROM grants WHERE connection=? AND status='active'", (connection_id,)).fetchall():
                    self._revoke_grant(db, g['id'], 'connection ' + status)
            out = self.public_connection(self.connection(db, connection_id))
        if status in ('revoked', 'retired'):
            out['deleted'] = self.forget_connection(connection_id)   # 0.25.0 (H4): disconnecting deletes what it synced
        return out

    # ---------- memory guard ----------
    def mentions_health(self, text):
        """0.24.3 (B02): True when a conversation turn names one of their health connections, grants, proposals or effects, or a
        health review. The broker then does not record it in conversation memory (unless /healthpolicy recall on)."""
        t = str(text or '')
        if not t or self.policy().get('recall') == 'on':
            return False
        if re.search(r'HEALTH GRANT|/health(?:grant|revoke|auth|confirm|connect|goal|pair)\b|\bhealth_[a-z_]+\b', t):
            return True
        ids = set(re.findall(r'\b[0-9a-f]{16}\b', t))
        if not ids:
            return False
        with self.db() as db:
            for table in ('connections', 'grants', 'grant_proposals', 'effects'):
                q = ','.join('?' * len(ids))
                if db.execute('SELECT 1 FROM %s WHERE id IN (%s) LIMIT 1' % (table, q), tuple(ids)).fetchone():
                    return True
        return False

    # ---------- grants ----------
    def _grant_shape(self, a):
        actions = a.get('actions')
        if not isinstance(actions, list) or not actions or len(actions) > 8 or any(x not in ACTIONS for x in actions):
            raise ValueError('actions is a non-empty list from: ' + ', '.join(ACTIONS))
        constraints = a.get('constraints') or {}
        if not isinstance(constraints, dict) or len(canonical(constraints)) > 4000:
            raise ValueError('constraints is an object (provider, visit_type, locations, time_windows, spend_cap_usd, cancellation_policy, prescriptions...).')
        disclosures = a.get('disclosures') or []
        if not isinstance(disclosures, list) or len(disclosures) > 12 or any(not isinstance(x, str) or len(x) > 160 for x in disclosures):
            raise ValueError('disclosures is a list of up to 12 short statements of what may be told to the other party.')
        if 'rx.refill' in actions and not constraints.get('prescriptions'):
            raise ValueError('A refill grant names the exact prescriptions it covers (constraints.prescriptions).')
        if any(x.startswith('appointment.') and x != 'appointment.search' for x in actions) and not (constraints.get('provider') or constraints.get('clinician') or constraints.get('specialty')):
            raise ValueError('A booking grant names the provider, clinician or specialty it covers.')
        spend = constraints.get('spend_cap_usd')
        if spend is not None and (type(spend) not in (int, float) or spend < 0 or spend > 5000):
            raise ValueError('spend_cap_usd is 0 to 5000.')
        valid_days = a.get('valid_days', 90)
        if type(valid_days) is not int or not 1 <= valid_days <= 365:
            raise ValueError('valid_days is 1 to 365.')
        no_secrets(canonical([constraints, disclosures]), 'grant')
        unknown = sorted(set(constraints) - set(CONSTRAINT_KEYS))
        if unknown:   # 0.24.3: a misspelled limit must never be silently ignored (it would widen the grant)
            raise ValueError('Unknown limit(s): %s. Limits are: %s.' % (', '.join(unknown), ', '.join(CONSTRAINT_KEYS)))
        return sorted(set(actions)), constraints, disclosures, valid_days

    def dry_run(self, actions, constraints, disclosures, unresolved, connection, valid_days=90):
        lines = ['HEALTH GRANT (dry run — nothing is authorized until you approve)',
                 'Subject: yourself', 'Connection: %s %s (%s)' % (connection['provider'], connection['institution'], connection['environment']),
                 'Actions allowed: ' + ', '.join(actions)]
        if constraints:
            lines.append('Limits: ' + '; '.join('%s = %s' % (k, json.dumps(v, ensure_ascii=False)) for k, v in sorted(constraints.items())))
        # 0.24.3 (B07): the review shows exactly what is stored. An empty list means nothing is listed for disclosure.
        lines.append('May be disclosed to the other party: ' + ('; '.join(disclosures) if disclosures else 'nothing (no disclosures listed)'))
        until = time.strftime('%b %d, %Y', time.localtime(self.now() + valid_days * 86400))
        lines.append('Valid for: %d day%s (until %s)' % (valid_days, '' if valid_days == 1 else 's', until))
        lines.append('Still unknown (will ask or stop): ' + ('; '.join(unresolved) if unresolved else 'nothing'))
        lines.append('Anything outside these limits stops and asks you. Revoke any time: /healthrevoke GRANT')
        return '\n'.join(lines)

    def propose_grant(self, a):
        actions, constraints, disclosures, valid_days = self._grant_shape(a)
        unresolved = a.get('unresolved') or []
        if not isinstance(unresolved, list) or len(unresolved) > 10 or any(not isinstance(x, str) or len(x) > 200 for x in unresolved):
            raise ValueError('unresolved is a list of up to 10 open questions.')
        title = clean(a.get('title'), 'title', 120)
        with self.db() as db:
            subject = self.subject(db, a.get('subject'))
            c = self.connection(db, clean(a.get('connection'), 'connection', 16), need_active=True)
            if c['subject'] != subject['id']:
                raise ValueError('That connection belongs to another subject.')
            pid = secrets.token_hex(8)
            body = {'subject': subject['id'], 'connection': c['id'], 'actions': actions, 'constraints': constraints, 'disclosures': disclosures, 'valid_days': valid_days, 'title': title}
            h = payload_hash(body)
            text = self.dry_run(actions, constraints, disclosures, unresolved, c, valid_days)
            db.execute('INSERT INTO grant_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid, h, subject['id'], c['id'], json.dumps(actions), json.dumps(constraints),
                       json.dumps(disclosures), valid_days, title, text, json.dumps(unresolved), self.now(), 'proposed', None))
        line = '/healthgrant %s %s' % (pid, h[:16])
        try:
            self.b.send_with_buttons('HEALTH GRANT REVIEW — ' + pid + '\n\n' + text + '\n\nApprove: send this line exactly (or tap to copy):\n' + line,
                                     [[{'text': '✅ Copy approval line', 'copy_text': line}]])
            delivered = True
        except Exception:
            delivered = False
        return {'proposal': pid, 'hash': h[:16], 'dry_run': text, 'review_delivered_to_owner': delivered,
                'next': 'Stop here. Nothing is authorized until the owner sends the /healthgrant line; you will be told when the owner does. Do not retry or re-propose.'}

    def approve_grant(self, a):
        pid = clean(a.get('proposal'), 'proposal', 16)
        with self.db() as db:
            p = db.execute('SELECT * FROM grant_proposals WHERE id=?', (pid,)).fetchone()
            if not p:
                raise ValueError('Unknown grant proposal.')
            if a.get('hash') != p['hash'][:16]:
                raise ValueError('That approval does not match this exact proposal.')
            if p['status'] != 'proposed':
                raise ValueError('This proposal was already %s.' % p['status'])
            if self.now() - p['created'] > 3600:
                db.execute("UPDATE grant_proposals SET status='expired' WHERE id=?", (pid,))
                raise ValueError('This proposal is older than an hour; ask for a fresh one.')
            self.connection(db, p['connection'], need_active=True)
            gid = secrets.token_hex(8)
            now = self.now()
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (gid, 1, p['subject'], p['connection'], p['actions'], p['constraints'], p['disclosures'],
                       p['title'], now, now + p['valid_days'] * 86400, 'active', p['hash'], now, now))
            db.execute('INSERT INTO grant_history VALUES(?,?,?,?,?,?,?,?,?)', (gid, 1, p['actions'], p['constraints'], p['disclosures'], now + p['valid_days'] * 86400, 'active', now, 'approved'))
            db.execute("UPDATE grant_proposals SET status='approved',grant_id=? WHERE id=?", (gid, pid))
            return self.public_grant(db.execute('SELECT * FROM grants WHERE id=?', (gid,)).fetchone())

    def public_grant(self, g):
        g = dict(g)
        return {'id': g['id'], 'revision': g['revision'], 'title': g['title'], 'subject': g['subject'], 'connection': g['connection'],
                'actions': json.loads(g['actions']), 'constraints': json.loads(g['constraints']), 'disclosures': json.loads(g['disclosures']),
                'valid_to': g['valid_to'], 'status': g['status']}

    def revise_grant(self, a):
        gid = clean(a.get('grant'), 'grant', 16)
        actions, constraints, disclosures, valid_days = self._grant_shape(a)
        with self.db() as db:
            g = db.execute('SELECT * FROM grants WHERE id=?', (gid,)).fetchone()
            if not g or g['status'] != 'active':
                raise ValueError('No active grant with that id.')
            now = self.now()
            rev = g['revision'] + 1
            db.execute('UPDATE grants SET revision=?,actions=?,constraints=?,disclosures=?,valid_to=?,updated=? WHERE id=?',
                       (rev, json.dumps(actions), json.dumps(constraints), json.dumps(disclosures), now + valid_days * 86400, now, gid))
            db.execute('INSERT INTO grant_history VALUES(?,?,?,?,?,?,?,?,?)', (gid, rev, json.dumps(actions), json.dumps(constraints), json.dumps(disclosures), now + valid_days * 86400, 'active', now, 'revised'))
            # Queued work bound to the superseded revision is cancelled; it must be re-authorized under the new one.
            n = self._cancel_queued(db, "grant_id=? AND grant_revision<?", (gid, rev), 'grant revised to r%d' % rev)
            return {**self.public_grant(db.execute('SELECT * FROM grants WHERE id=?', (gid,)).fetchone()), 'cancelled_queued': n}

    def _cancel_queued(self, db, where, params, why):
        rows = db.execute("SELECT id FROM effects WHERE state IN ('authorized','queued','awaiting_owner') AND " + where, params).fetchall()
        for r in rows:
            db.execute("UPDATE effects SET state='cancelled',settled_at=?,note=? WHERE id=?", (self.now(), why, r['id']))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (r['id'], self.now(), 'cancelled', 'grant', why))
        for r in db.execute("SELECT id FROM effects WHERE state='dispatching' AND " + where, params).fetchall():
            # Already sent to the provider: it cannot be cancelled here; it is reconciled by a read before anything else.
            db.execute("UPDATE effects SET state='uncertain',note=? WHERE id=?", ('authority changed while dispatching (' + why + '); read provider state', r['id']))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (r['id'], self.now(), 'uncertain', 'grant', why))
        return len(rows)

    def _revoke_grant(self, db, gid, why):
        g = db.execute('SELECT * FROM grants WHERE id=?', (gid,)).fetchone()
        if not g:
            raise ValueError('Unknown grant.')
        if g['status'] == 'revoked':
            return 0
        now = self.now()
        db.execute("UPDATE grants SET status='revoked',updated=? WHERE id=?", (now, gid))
        db.execute('INSERT INTO grant_history VALUES(?,?,?,?,?,?,?,?,?)', (gid, g['revision'], g['actions'], g['constraints'], g['disclosures'], g['valid_to'], 'revoked', now, why))
        return self._cancel_queued(db, 'grant_id=?', (gid,), 'grant revoked: ' + why)

    def revoke_grant(self, a):
        gid = clean(a.get('grant'), 'grant', 16)
        with self.db() as db:
            n = self._revoke_grant(db, gid, clean(a.get('why') or 'owner', 'why', 200, required=False))
            return {'grant': gid, 'status': 'revoked', 'cancelled_queued': n}

    def grant_for(self, db, gid, action, subject_id, connection_id, now=None):
        """The active grant that covers this action now, or a ValueError naming what is missing. Called at authorization,
        again at dispatch and again right before the consequential effect."""
        now = now or self.now()
        g = db.execute('SELECT * FROM grants WHERE id=?', (gid,)).fetchone()
        if not g:
            raise ValueError('Unknown grant.')
        if g['status'] != 'active':
            raise ValueError('Grant %s is %s; ask the owner for a new one.' % (gid, g['status']))
        if not g['valid_from'] <= now <= g['valid_to']:
            raise ValueError('Grant %s is outside its validity period; ask the owner to renew it.' % gid)
        if g['subject'] != subject_id or g['connection'] != connection_id:
            raise ValueError('Grant %s does not cover this subject/connection.' % gid)
        if action not in json.loads(g['actions']):
            raise ValueError('Grant %s does not allow %s; it allows %s. Ask the owner (health_grant_propose) rather than proceeding.' % (gid, action, ', '.join(json.loads(g['actions']))))
        return dict(g)

    @staticmethod
    def within_constraints(g, action, payload):
        """Deterministic constraint checks on the facts the payload states. Unknown facts are not assumed to comply."""
        c = json.loads(g['constraints']) if isinstance(g.get('constraints'), str) else (g.get('constraints') or {})
        p = payload or {}
        problems = []
        if action.startswith('appointment.'):
            want = (c.get('clinician') or c.get('provider') or '').lower()
            got = (p.get('clinician') or p.get('provider') or '').lower()
            if want and got and want not in got and got not in want:
                problems.append('clinician/provider %r is not the approved %r' % (p.get('clinician') or p.get('provider'), c.get('clinician') or c.get('provider')))
            if c.get('visit_type') and p.get('visit_type') and str(c['visit_type']).lower() != str(p['visit_type']).lower():
                problems.append('visit type %r is not the approved %r' % (p['visit_type'], c['visit_type']))
            if c.get('locations') and p.get('location') and not any(str(p['location']).lower().find(str(x).lower()) >= 0 or str(x).lower().find(str(p['location']).lower()) >= 0 for x in c['locations']):
                problems.append('location %r is not among the approved %s' % (p['location'], c['locations']))
            if c.get('time_windows') and p.get('date'):
                ok = any(str(w.get('from', '')) <= str(p['date'])[:10] <= str(w.get('to', '9999-12-31')) for w in c['time_windows'] if isinstance(w, dict))
                if not ok:
                    problems.append('date %s is outside the approved windows' % p['date'])
            if c.get('weekdays') and p.get('weekday') and str(p['weekday']).lower()[:3] not in [str(x).lower()[:3] for x in c['weekdays']]:
                problems.append('%s is not an approved day' % p['weekday'])
            if c.get('time_of_day') and p.get('time_of_day') and str(p['time_of_day']).lower() != str(c['time_of_day']).lower():
                problems.append('%s is not the approved time of day (%s)' % (p['time_of_day'], c['time_of_day']))
        if action == 'rx.refill':
            allowed = [str(x).lower() for x in (c.get('prescriptions') or [])]
            if str(p.get('prescription_id') or '').lower() not in allowed:
                problems.append('prescription %r is not one the grant names' % p.get('prescription_id'))
            if c.get('pharmacy') and p.get('pharmacy') and str(c['pharmacy']).lower() not in str(p['pharmacy']).lower():
                problems.append('pharmacy %r is not the approved %r' % (p['pharmacy'], c['pharmacy']))
            if c.get('pharmacy') and not p.get('pharmacy'):
                problems.append('the payload does not say which pharmacy/store')
            allowed_ful = c.get('fulfillment')
            if allowed_ful:
                allowed_ful = [allowed_ful] if isinstance(allowed_ful, str) else list(allowed_ful)
                if (p.get('fulfillment') or 'pickup') not in allowed_ful:
                    problems.append('%s is not an approved way to receive it (%s)' % (p.get('fulfillment') or 'pickup', ', '.join(allowed_ful)))
        if action in ('appointment.cancel', 'appointment.reschedule'):
            # Plan 02: cancellation/reschedule authority is explicit; fees are known before acting.
            if c.get('cancellation_policy') == 'no_fee_only':
                fee = p.get('fee_usd')
                if fee is None:
                    problems.append('the cancellation/change fee is unknown and the grant allows only fee-free changes')
                else:
                    try:
                        if float(fee) > 0:
                            problems.append('the change costs $%.2f and the grant allows only fee-free changes' % float(fee))
                    except (TypeError, ValueError):
                        problems.append('the fee is not a number')
        if action == 'appointment.reschedule' and c.get('reschedule_order') != 'book_then_cancel':
            problems.append('The owner has not approved how a reschedule is done (constraints.reschedule_order = "book_then_cancel": the new visit is booked first, '
                            'then the old one cancelled; for a few seconds both exist)')
        cap = c.get('spend_cap_usd')
        if cap is not None and p.get('cost_usd') is not None:
            try:
                if float(p['cost_usd']) > float(cap):
                    problems.append('cost $%.2f is over the $%.2f cap' % (float(p['cost_usd']), float(cap)))
            except (TypeError, ValueError):
                problems.append('cost is not a number')
        if cap is not None and p.get('cost_usd') is None and action in ('appointment.book', 'rx.refill'):
            problems.append('the cost is unknown and the grant has a spending cap')
        return problems

    # ---------- effects ----------
    def public_effect(self, e):
        e = dict(e)
        return {k: e.get(k) for k in ('id', 'request_id', 'subject', 'connection', 'grant_id', 'grant_revision', 'action', 'kind', 'target', 'step', 'state',
                                      'provider_state', 'created', 'attempted_at', 'provider_accepted_at', 'confirmed_at', 'settled_at', 'source', 'external_id',
                                      'note', 'duplicate_of')} | {'payload': json.loads(e['payload']) if e.get('payload') else None, 'receipt': bool(e.get('receipt_ref'))}

    def begin_effect(self, a):
        """Persist the operation and decide: authorized (may dispatch) or awaiting_owner (outside the grant / equivalent exists)."""
        rid = a.get('request_id')
        if not isinstance(rid, str) or not REQUEST_ID.match(rid):
            raise ValueError('Use a stable request_id of 8–100 letters, digits, _ : . -; reuse it on retries.')
        action = clean(a.get('action'), 'action', 40)
        if action not in ACTIONS or action in READ_ACTIONS:
            raise ValueError('Effects are the consequential actions: ' + ', '.join(x for x in ACTIONS if x not in READ_ACTIONS) + '. Reads need no effect record.')
        payload = a.get('payload')
        if not isinstance(payload, dict) or not payload or len(canonical(payload)) > 6000:
            raise ValueError('payload is the exact operation as an object (clinician, date, time, location, visit_type, cost_usd, prescription_id, pharmacy...).')
        no_secrets(canonical(payload), 'payload')
        kind = EFFECT_KINDS[action]
        h = payload_hash(payload)
        step = clean(a.get('step') or '', 'step', 120, required=False)
        with self.db() as db:
            old = db.execute('SELECT * FROM effects WHERE request_id=?', (rid,)).fetchone()
            if old:
                if old['payload_hash'] != h or old['action'] != action:
                    raise ValueError('request_id %s already belongs to a different operation; use a new request_id for new content.' % rid)
                return self.public_effect(old)
            subject = self.subject(db, a.get('subject'))
            c = self.connection(db, clean(a.get('connection'), 'connection', 16), need_active=True)
            if c['subject'] != subject['id']:
                raise ValueError('That connection belongs to another subject.')
            gid = clean(a.get('grant'), 'grant', 16)
            eid = secrets.token_hex(8)
            now = self.now()
            target = target_key(kind, payload, action)
            replaces = self.replaced_ids(db, action, payload)
            state, note, dup = 'authorized', '', None
            try:
                g = self.grant_for(db, gid, action, subject['id'], c['id'], now)
                problems = self.within_constraints(g, action, payload)
                rev = g['revision']
                if problems:
                    state, note = 'awaiting_owner', 'outside the grant: ' + '; '.join(problems)[:400]
            except ValueError as e:
                g = db.execute('SELECT * FROM grants WHERE id=?', (gid,)).fetchone()
                rev = g['revision'] if g else 0
                state, note = 'awaiting_owner', str(e)[:400]
            # Equivalence: the same thing already in flight or done, from any packet, browser, call or API.
            same = db.execute("SELECT id,state FROM effects WHERE subject=? AND kind=? AND target=? AND state IN ('authorized','queued','dispatching','confirmed','uncertain','awaiting_owner') "
                              "AND id!=? AND id NOT IN (%s) "
                              # a refill that was picked up / delivered, or confirmed over a week ago, is history: next month's refill is new
                              "AND NOT (kind='refill' AND state='confirmed' AND (settled_at<? OR provider_state LIKE 'picked_up%%' OR provider_state LIKE 'delivered%%')) "
                              "ORDER BY created DESC LIMIT 1" % ','.join('?' * len(replaces) or ["''"]),
                              (subject['id'], kind, target, eid, *replaces, now - 7 * 86400)).fetchone()
            if same:
                state, dup = 'awaiting_owner', same['id']
                note = ('an equivalent %s already exists (%s, %s); ' % (kind, same['id'], same['state']) + note).strip('; ')
            db.execute('INSERT INTO effects(id,request_id,payload_hash,subject,connection,grant_id,grant_revision,action,kind,target,step,state,payload,created,note,duplicate_of) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (eid, rid, h, subject['id'], c['id'], gid, rev, action, kind, target, step, state, json.dumps(payload), now, note, dup))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, now, state, 'broker', note))
            out = self.public_effect(db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone())
        if out['state'] == 'awaiting_owner':
            out['next'] = ('Stop. This operation is outside the grant or duplicates an existing one: ' + out['note'] +
                           '. Tell the owner in one line what you would do and why it needs them; do not dispatch it through any channel.')
        else:
            out['next'] = 'Authorized under grant r%d. Call health_effect_dispatch right before the provider operation, then health_effect_settle with the provider\'s answer.' % out['grant_revision']
        return out

    def dispatch_effect(self, a):
        """authorized → dispatching, with a lease; authority is rechecked here (and the adapter calls it again before the final press)."""
        eid = clean(a.get('effect'), 'effect', 16)
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('Unknown effect.')
            now = self.now()
            if e['state'] == 'dispatching' and (e['lease_until'] or 0) > now:
                return {**self.public_effect(e), 'next': 'Already dispatching; finish and settle it. Do not send it again.'}
            if e['state'] not in ('authorized', 'queued'):
                raise ValueError('Effect %s is %s; it cannot be dispatched%s.' % (eid, e['state'], ' (read provider state and reconcile it first)' if e['state'] == 'uncertain' else ''))
            g = self.grant_for(db, e['grant_id'], e['action'], e['subject'], e['connection'], now)
            stop = None
            if g['revision'] != e['grant_revision']:
                db.execute("UPDATE effects SET state='cancelled',settled_at=?,note=? WHERE id=?", (now, 'grant revised since authorization', eid))
                stop = 'The grant was revised since this operation was authorized; begin it again under the current revision.'
            problems = [] if stop else self.within_constraints(g, e['action'], json.loads(e['payload']))
            if problems:
                db.execute("UPDATE effects SET state='awaiting_owner',note=? WHERE id=?", ('outside the grant: ' + '; '.join(problems)[:400], eid))
                stop = 'Outside the grant now: ' + '; '.join(problems)
            if stop:
                db.commit()      # keep the state change: raising inside the block would roll it back
                raise ValueError(stop)
            # One write at a time per target: an equivalent effect that is dispatching or uncertain blocks this one.
            busy = db.execute("SELECT id,state FROM effects WHERE subject=? AND kind=? AND target=? AND id!=? AND (state='uncertain' OR (state='dispatching' AND lease_until>?))",
                              (e['subject'], e['kind'], e['target'], eid, now)).fetchone()
            if busy:
                raise ValueError('Effect %s for the same target is %s; reconcile it first.' % (busy['id'], busy['state']))
            db.execute("UPDATE effects SET state='dispatching',attempted_at=?,lease_until=? WHERE id=?", (now, now + LEASE_SECONDS, eid))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, now, 'dispatching', 'broker', ''))
            return {**self.public_effect(db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()),
                    'disclosures': json.loads(g['disclosures']), 'constraints': json.loads(g['constraints']),
                    'next': 'Perform the operation once through its adapter (browser task, call or API). Then health_effect_settle with exactly what the provider answered.'}

    def settle_effect(self, a):
        eid = clean(a.get('effect'), 'effect', 16)
        outcome = clean(a.get('outcome'), 'outcome', 20).lower()
        if outcome not in SETTLE:
            raise ValueError('outcome is confirmed, requested, waitlisted, held, failed, uncertain or cancelled.')
        source = clean(a.get('source'), 'source', 60)
        external_id = clean(a.get('external_id') or '', 'external_id', 120, required=False)
        provider_state = clean(a.get('provider_state') or '', 'provider_state', 160, required=False)
        detail = clean(a.get('detail') or '', 'detail', 600, required=False)
        no_secrets(external_id + ' ' + provider_state + ' ' + detail, 'settlement')
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('Unknown effect.')
            if e['state'] not in ('dispatching', 'uncertain', 'queued', 'authorized'):
                raise ValueError('Effect %s is already %s.' % (eid, e['state']))
            now = self.now()
            if outcome == 'confirmed' and PHONE_SOURCE.search(source):
                # Plan 02: a phone transcript reporting a booking is a REQUEST until the portal/site shows it or the owner confirms it
                # (/healthconfirm EFFECT). The receptionist's words are kept as the provider state.
                outcome = 'requested'
                detail = ('phone: ' + detail).strip()[:600]
            if outcome == 'confirmed':
                if not external_id and not detail:
                    raise ValueError('A confirmation needs the provider\'s confirmation/reference ID or the exact confirmation wording (detail).')
                db.execute("UPDATE effects SET state='confirmed',provider_accepted_at=COALESCE(provider_accepted_at,?),confirmed_at=?,settled_at=?,source=?,external_id=?,provider_state=?,note=?,lease_until=NULL WHERE id=?",
                           (now, now, now, source, external_id, provider_state, detail, eid))
                self.retire_replaced(db, eid)
            elif outcome in ('requested', 'waitlisted', 'held'):
                # Provider accepted a REQUEST; it is not a confirmed booking. The effect stays open as uncertain-until-read.
                db.execute("UPDATE effects SET state='uncertain',provider_accepted_at=?,source=?,external_id=?,provider_state=?,note=?,lease_until=NULL WHERE id=?",
                           (now, source, external_id, outcome + (': ' + provider_state if provider_state else ''), detail or ('provider accepted a ' + outcome + ' request; not confirmed'), eid))
            elif outcome == 'uncertain':
                db.execute("UPDATE effects SET state='uncertain',source=?,provider_state=?,note=?,lease_until=NULL WHERE id=?", (source, provider_state, detail or 'ambiguous provider response', eid))
            else:
                db.execute("UPDATE effects SET state=?,settled_at=?,source=?,provider_state=?,note=?,lease_until=NULL WHERE id=?", (outcome, now, source, provider_state, detail, eid))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, now, outcome, source, detail))
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            out = self.public_effect(e)
        if out['state'] == 'uncertain':
            out['next'] = ('Not confirmed. Before ANY new attempt, read the provider\'s state (portal/site/call) and call health_effect_reconcile with what it shows. '
                           'Tell the owner it is pending, not booked.')
        elif out['state'] == 'confirmed':
            out['next'] = 'Confirmed. Send the owner ONE neutral line (health_receipt gives them the private details).'
        return out

    def reconcile_effect(self, a):
        """After an ambiguous result: what a READ of provider state shows. Only this can move uncertain → confirmed/failed."""
        eid = clean(a.get('effect'), 'effect', 16)
        found = clean(a.get('found'), 'found', 20).lower()
        if found not in ('confirmed', 'absent', 'pending', 'unknown'):
            raise ValueError('found is confirmed (the provider shows it), absent (the provider shows nothing), pending or unknown.')
        source = clean(a.get('source'), 'source', 60)
        external_id = clean(a.get('external_id') or '', 'external_id', 120, required=False)
        detail = clean(a.get('detail') or '', 'detail', 600, required=False)
        no_secrets(external_id + ' ' + detail, 'reconciliation')
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('Unknown effect.')
            if e['state'] != 'uncertain':
                raise ValueError('Effect %s is %s, not uncertain.' % (eid, e['state']))
            now = self.now()
            if found == 'confirmed':
                db.execute("UPDATE effects SET state='confirmed',confirmed_at=?,settled_at=?,source=?,external_id=COALESCE(NULLIF(?,''),external_id),note=? WHERE id=?", (now, now, source, external_id, detail, eid))
                self.retire_replaced(db, eid)
            elif found == 'absent':
                db.execute("UPDATE effects SET state='failed',settled_at=?,source=?,note=? WHERE id=?", (now, source, 'provider shows no such operation: ' + detail, eid))
            else:
                db.execute("UPDATE effects SET source=?,note=? WHERE id=?", (source, found + ': ' + detail, eid))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, now, 'reconcile:' + found, source, detail))
            out = self.public_effect(db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone())
        if out['state'] == 'failed':
            out['next'] = 'The provider shows nothing. A new attempt needs a NEW request_id and begins again under the grant (health_effect_begin).'
        elif out['state'] == 'uncertain':
            out['next'] = 'Still uncertain; do not attempt it again. Tell the owner and check later (or ask them to look).'
        return out

    def public_effect_by_id(self, eid):
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('Unknown effect.')
            return self.public_effect(e)

    def hold_effect(self, eid, note):
        """An adapter found, before writing anything, that the operation needs the owner (an equivalent booking already exists at the
        provider, a fact changed): dispatching/authorized → awaiting_owner, nothing sent."""
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e or e['state'] not in ('authorized', 'queued', 'dispatching'):
                raise ValueError('Effect %s cannot be held now.' % eid)
            db.execute("UPDATE effects SET state='awaiting_owner',note=?,lease_until=NULL WHERE id=?", (clean(note, 'note', 400), eid))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, self.now(), 'awaiting_owner', 'adapter', note[:400]))

    def owner_confirm(self, a):
        """The owner saw the booking themselves (portal, email, the clinic told them): uncertain/requested → confirmed, source owner."""
        eid = clean(a.get('effect'), 'effect', 16)
        external_id = clean(a.get('external_id') or '', 'external_id', 120, required=False)
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e or e['state'] not in ('uncertain', 'dispatching'):
                raise ValueError('Only an uncertain or requested operation can be confirmed by you (this one is %s).' % (e['state'] if e else 'unknown'))
            now = self.now()
            db.execute("UPDATE effects SET state='confirmed',confirmed_at=?,settled_at=?,source='owner',external_id=COALESCE(NULLIF(?,''),external_id),lease_until=NULL,note=? WHERE id=?",
                       (now, now, external_id, 'confirmed by the owner', eid))
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, now, 'confirmed', 'owner', external_id))
            self.retire_replaced(db, eid)
            return self.public_effect(db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone())

    @staticmethod
    def replaced_ids(db, action, payload):
        """The booking effect(s) a cancel/reschedule refers to (of_effect, or the effect that created appointment_id)."""
        if action not in ('appointment.cancel', 'appointment.reschedule'):
            return []
        p = payload or {}
        ids = []
        if p.get('of_effect'):
            ids.append(str(p['of_effect']))
        if p.get('appointment_id'):
            ids += [r['id'] for r in db.execute("SELECT id FROM effects WHERE external_id=?", ('Appointment/' + str(p['appointment_id']),))]
        return ids

    def retire_replaced(self, db, eid):
        """A confirmed cancel/reschedule: the booking it replaced no longer blocks a new booking of that visit (equivalence)."""
        e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
        if not e or e['action'] not in ('appointment.cancel', 'appointment.reschedule'):
            return
        for rid in self.replaced_ids(db, e['action'], json.loads(e['payload'])):
            db.execute("UPDATE effects SET target=target||?,note=TRIM(COALESCE(note,'')||' '||?) WHERE id=? AND target NOT LIKE '%#replaced%'",
                       ('#replaced-by-' + eid, ('cancelled by ' if e['action'] == 'appointment.cancel' else 'moved by ') + eid, rid))

    def provider_update(self, eid, found, source, provider_state, detail=''):
        """A later provider status for an operation (pharmacy refill progress, webhook or poll). found: confirmed | failed | pending.
        uncertain/dispatching → confirmed/failed; a confirmed operation only gets its provider_state updated (a later 'failed' after a
        confirmation is kept as a conflict for the owner, never silently applied)."""
        if found not in ('confirmed', 'failed', 'pending'):
            raise ValueError('found is confirmed, failed or pending.')
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('Unknown effect.')
            now, ps = self.now(), clean(provider_state or '', 'provider_state', 160, required=False)
            conflict = False
            if e['state'] in ('uncertain', 'dispatching'):
                if found == 'confirmed':
                    db.execute("UPDATE effects SET state='confirmed',confirmed_at=?,settled_at=?,source=?,provider_state=?,note=?,lease_until=NULL WHERE id=?", (now, now, source, ps, detail[:600], eid))
                elif found == 'failed':
                    db.execute("UPDATE effects SET state='failed',settled_at=?,source=?,provider_state=?,note=?,lease_until=NULL WHERE id=?", (now, source, ps, detail[:600], eid))
                else:
                    db.execute("UPDATE effects SET source=?,provider_state=?,note=? WHERE id=?", (source, ps, detail[:600] or e['note'], eid))
            elif e['state'] == 'confirmed':
                if found == 'failed':
                    conflict = True
                    db.execute("UPDATE effects SET note=? WHERE id=?", (('CONFLICT: the provider later reported %s after a confirmation; check with the provider' % ps)[:600], eid))
                else:
                    db.execute("UPDATE effects SET provider_state=?,source=? WHERE id=?", (ps, source, eid))
            else:
                conflict = True
            db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (eid, now, 'provider:' + found + (':conflict' if conflict else ''), source, ps + ' ' + detail[:300]))
            out = self.public_effect(db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone())
        out['conflict'] = conflict
        return out

    def expire_leases(self):
        """A dispatching lease that ran out does not mean the operation failed: it becomes uncertain and must be read back."""
        with self.db() as db:
            now = self.now()
            rows = db.execute("SELECT id FROM effects WHERE state='dispatching' AND lease_until IS NOT NULL AND lease_until<?", (now,)).fetchall()
            for r in rows:
                db.execute("UPDATE effects SET state='uncertain',note='dispatch lease expired without a settlement; read provider state',lease_until=NULL WHERE id=?", (r['id'],))
                db.execute('INSERT INTO effect_events VALUES(?,?,?,?,?)', (r['id'], now, 'uncertain', 'lease', 'expired'))
            return len(rows)

    # ---------- receipts ----------
    def make_receipt(self, a):
        eid = clean(a.get('effect'), 'effect', 16)
        body = clean(a.get('body'), 'body', 4000)
        no_secrets(body, 'receipt')
        with self.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
            if not e:
                raise ValueError('Unknown effect.')
            token = secrets.token_urlsafe(24)
            rid = secrets.token_hex(8)
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,0)', (rid, eid, self.now(), body, hashlib.sha256(token.encode()).hexdigest(), self.now() + 7 * 86400))
            db.execute("UPDATE effects SET receipt_ref=? WHERE id=?", (rid, eid))
        return {'receipt': rid, 'token': token, 'expires_days': 7}

    def receipt_by_token(self, token, consume=True):
        h = hashlib.sha256(str(token or '').encode()).hexdigest()
        with self.db() as db:
            r = db.execute('SELECT * FROM receipts WHERE page_hash=?', (h,)).fetchone()
            if not r or r['expires'] < self.now():
                return None
            if consume:
                db.execute('UPDATE receipts SET used=used+1 WHERE id=?', (r['id'],))
            return {'id': r['id'], 'effect_id': r['effect_id'], 'body': r['body'], 'created': r['created']}

    # ---------- retention ----------
    def prune(self):
        days = int(self.policy().get('retention_days') or 365)
        cutoff = self.now() - days * 86400
        with self.db() as db:
            n = db.execute("DELETE FROM effects WHERE settled_at IS NOT NULL AND settled_at<? AND state IN ('confirmed','failed','cancelled')", (cutoff,)).rowcount
            db.execute('DELETE FROM effect_events WHERE effect_id NOT IN (SELECT id FROM effects)')
            db.execute('DELETE FROM receipts WHERE expires<?', (self.now(),))
            db.execute("DELETE FROM grant_proposals WHERE created<?", (self.now() - 7 * 86400,))
        return n

    # ---------- status ----------
    def status(self):
        with self.db() as db:
            subs = [dict(r) for r in db.execute('SELECT id,display_name,relation,status FROM subjects')]
            conns = [self.public_connection(dict(r)) for r in db.execute('SELECT * FROM connections ORDER BY created')]
            grants = [self.public_grant(r) for r in db.execute("SELECT * FROM grants WHERE status='active' ORDER BY created")]
            open_effects = [self.public_effect(r) for r in db.execute("SELECT * FROM effects WHERE state IN ('authorized','queued','dispatching','uncertain','awaiting_owner') ORDER BY created DESC LIMIT 20")]
        return {'subjects': subs, 'connections': conns, 'grants': grants, 'open_effects': open_effects, 'policy': self.policy(),
                'rules': ('Grants are approved by the owner only (/healthgrant). Every booking/refill is one effect: begin → dispatch → settle; an '
                          'ambiguous result is uncertain and is READ back before any new attempt. A provider "request accepted" is not a confirmation. '
                          'Health details go in the private receipt, never in Telegram text or memory.')}

    # ---------- dispatch ----------
    portal = None   # health_portal.Portal, attached by the broker (0.22.0)
    modules = None  # 0.23.0: prefix -> module with call(action, args, privileged): scheduling, nutrition, pharmacy, sync

    # ---------- 0.25.0: read permission (H2) ----------
    def read_gate(self, category):
        """Passes when the owner allowed this kind of read (once, or until a date); otherwise asks them once and refuses for now."""
        t = self.now()
        with self.db() as db:
            r = db.execute('SELECT * FROM read_scopes WHERE category=?', (category,)).fetchone()
            if r and r['once']:
                db.execute('UPDATE read_scopes SET once=0,updated=? WHERE category=?', (t, category))
                return
            if r and (r['until'] or 0) > t:
                return
            pending = db.execute("SELECT id FROM read_requests WHERE category=? AND status='pending' AND created>?", (category, t - 1800)).fetchone()
            rid = pending['id'] if pending else secrets.token_hex(4)
            if not pending:
                db.execute("INSERT INTO read_requests VALUES(?,?,?,'pending')", (rid, category, t))
        if not pending:
            try:
                self.b.telegram('PROCURATOR asks to read your %s (health).\nAllow once: /healthread %s once\nAllow for 30 days: /healthread %s 30\n'
                                'Ignore this message to decline.' % (READ_CATEGORIES[category], rid, rid))
            except Exception:
                pass
        raise ValueError('The owner has not allowed reading their %s yet. A request was sent to them (/healthread); wait for their answer and do not '
                         'ask again in a loop. Nothing was read.' % READ_CATEGORIES[category])

    def read_allow(self, a):
        """Owner only: /healthread ID once|DAYS · /healthread CATEGORY DAYS|off · /healthread (status)."""
        t = self.now()
        with self.db() as db:
            if a.get('request'):
                rq = db.execute('SELECT * FROM read_requests WHERE id=?', (clean(a['request'], 'request', 8),)).fetchone()
                if not rq:
                    raise ValueError('No such read request.')
                category = rq['category']
                db.execute("UPDATE read_requests SET status='answered' WHERE id=?", (rq['id'],))
            else:
                category = clean(a.get('category') or '', 'category', 20, required=False)
            if category:
                if category not in READ_CATEGORIES:
                    raise ValueError('category is one of: ' + ', '.join(READ_CATEGORIES))
                how = str(a.get('allow') or '').lower()
                if how == 'off':
                    db.execute('DELETE FROM read_scopes WHERE category=?', (category,))
                elif how == 'once':
                    db.execute('INSERT INTO read_scopes VALUES(?,0,1,?) ON CONFLICT(category) DO UPDATE SET once=1,updated=excluded.updated', (category, t))
                else:
                    days = int(a.get('days') or 0)
                    if not 1 <= days <= 365:
                        raise ValueError('Allow once, for 1-365 days, or off.')
                    db.execute('INSERT INTO read_scopes VALUES(?,?,0,?) ON CONFLICT(category) DO UPDATE SET until=excluded.until,once=0,updated=excluded.updated',
                               (category, t + days * 86400, t))
            rows = {r['category']: r for r in db.execute('SELECT * FROM read_scopes')}
        return {'scopes': {c: ('once' if rows.get(c) and rows[c]['once'] else ('until ' + time.strftime('%Y-%m-%d', time.gmtime(rows[c]['until'])) if rows.get(c) and (rows[c]['until'] or 0) > t else 'ask'))
                           for c in READ_CATEGORIES}, 'category': category or None}

    # ---------- 0.25.0: visit packets (F1) and the health admin digest (F2) ----------
    DEFAULT_BRING = ('Photo ID', 'Insurance card', 'List of current medications (your own list)', 'Referral or order, if the visit needs one')

    def appointments(self, db, days_ahead=None):
        today = time.strftime('%Y-%m-%d', time.localtime(self.now()))
        out = []
        for e in db.execute("SELECT * FROM effects WHERE kind='appointment' AND action!='appointment.cancel' AND state IN ('confirmed','awaiting_owner','dispatching','queued','authorized') ORDER BY created"):
            try:
                p = json.loads(e['payload'] or '{}')
            except ValueError:
                p = {}
            d = str(p.get('date') or '')
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', d) or d < today:
                continue
            if days_ahead is not None and d > time.strftime('%Y-%m-%d', time.localtime(self.now() + days_ahead * 86400)):
                continue
            out.append((e, p))
        return sorted(out, key=lambda x: (x[1].get('date'), x[1].get('time') or ''))

    def visit_packet(self, eid=None):
        """Owner only (dashboard Health tab): what to know and bring for each upcoming appointment. No clinical interpretation."""
        with self.db() as db:
            rows = self.appointments(db)
            notes = {r['effect_id']: r for r in db.execute('SELECT * FROM visit_notes')}
            conns = {c['id']: c for c in db.execute('SELECT id,institution,endpoint FROM connections')}
        packets = []
        for e, p in rows:
            if eid and e['id'] != eid:
                continue
            n = notes.get(e['id'])
            c = conns.get(e['connection']) or {}
            packets.append({'effect': e['id'], 'state': e['state'], 'date': p.get('date'), 'time': p.get('time'), 'tz': p.get('tz'),
                            'clinician': p.get('clinician'), 'location': p.get('location'), 'visit_type': p.get('visit_type'),
                            'institution': c['institution'] if c else None, 'telehealth': bool(re.search(r'(?i)tele|video|virtual', str(p.get('visit_type') or p.get('location') or ''))),
                            'bring': (json.loads(n['bring']) if n and n['bring'] else []) or list(self.DEFAULT_BRING),
                            'questions': json.loads(n['questions']) if n and n['questions'] else [],
                            'confirmation': e['external_id'], 'note': 'Logistics only: your own questions and documents. PROCURATOR does not interpret results or symptoms.'})
        return {'visits': packets}

    def visit_note(self, a):
        eid = clean(a.get('effect'), 'effect', 16)
        def lines(v, name):
            items = v if isinstance(v, list) else [x for x in str(v or '').splitlines()]
            items = [clean(x, name, 200) for x in items if str(x).strip()][:12]
            no_secrets(canonical(items), name)
            return items
        questions, bring = lines(a.get('questions'), 'question'), lines(a.get('bring'), 'item')
        with self.db() as db:
            if not db.execute("SELECT 1 FROM effects WHERE id=? AND kind='appointment'", (eid,)).fetchone():
                raise ValueError('No such appointment.')
            db.execute('INSERT INTO visit_notes VALUES(?,?,?,?) ON CONFLICT(effect_id) DO UPDATE SET questions=excluded.questions,bring=excluded.bring,updated=excluded.updated',
                       (eid, json.dumps(questions), json.dumps(bring), self.now()))
        return self.visit_packet(eid)

    def digest(self):
        """Counts only (safe for Telegram and for the model): what in the health module needs their attention."""
        t = self.now()
        with self.db() as db:
            upcoming = len(self.appointments(db, days_ahead=7))
            waiting = db.execute("SELECT COUNT(*) FROM effects WHERE state IN ('awaiting_owner','uncertain')").fetchone()[0]
            refills = db.execute("SELECT COUNT(*) FROM effects WHERE kind='refill' AND state IN ('queued','dispatching','authorized','awaiting_owner')").fetchone()[0]
            proposals = db.execute("SELECT COUNT(*) FROM grant_proposals WHERE status='proposed' AND created>?", (t - 3600,)).fetchone()[0]
            reads = db.execute("SELECT COUNT(*) FROM read_requests WHERE status='pending' AND created>?", (t - 1800,)).fetchone()[0]
        total = upcoming + waiting + refills + proposals + reads
        return {'appointments_next_7_days': upcoming, 'waiting_on_you': waiting + proposals + reads, 'refills_in_progress': refills, 'total': total,
                'line': ('Health: %d item%s need%s you — open the dashboard Health tab.' % (total, '' if total == 1 else 's', 's' if total == 1 else '')) if total else 'Health: nothing needs you.'}

    # ---------- 0.25.0: disconnecting deletes (H4) ----------
    def forget_connection(self, connection_id):
        """Deletes what was synced or recorded for a revoked/retired connection (portal links, refill records); effects and
        receipts stay as the record of what was done. Returns counts by kind, nothing else."""
        deleted = {}
        with self.db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, label in (('portal_links', 'portal link'), ('portal_auth', 'pending sign-in'), ('portal_incidents', 'portal incident'),
                                 ('rx_refills', 'refill record'), ('rx_events', 'pharmacy event'), ('rx_webhooks', 'pharmacy webhook')):
                if table in tables:
                    n = db.execute('DELETE FROM %s WHERE connection=?' % table, (connection_id,)).rowcount
                    if n:
                        deleted[label] = n
        return deleted

    def attach(self, prefix, module):
        self.modules = dict(self.modules or {})
        self.modules[prefix] = module
        return module

    def call(self, action, args, privileged=False):
        a = args if isinstance(args, dict) else {}
        self.expire_leases()
        if action == 'health_adult_confirm':
            if not privileged:
                raise ValueError('Only the owner confirms this themselves (/healthadult).')
            return self.set_policy('adult_confirmed', 'yes')
        if action not in ADULT_FREE and self.policy().get('adult_confirmed') != 'yes':
            raise ValueError('The health module is off until the owner confirms once that the owner is 18 or older: the owner sends /healthadult. Nothing was done.')
        if action == 'health_digest':
            return self.digest()
        if action in ('health_visit_packet', 'health_visit_note'):
            if not privileged:
                raise ValueError('Visit packets are on their private dashboard (Health tab).')
            return self.visit_packet(a.get('effect')) if action == 'health_visit_packet' else self.visit_note(a)
        if action == 'health_read_allow':
            if not privileged:
                raise ValueError('Only the owner allows health reads (/healthread).')
            return self.read_allow(a)
        if not privileged and action in READ_GATED:
            self.read_gate(READ_GATED[action])
        if action.startswith('health_portal_'):
            if not self.portal:
                raise ValueError('The patient-portal client is not configured on this server.')
            return self.portal.call(action, a, privileged)
        for prefix, module in sorted((self.modules or {}).items(), key=lambda x: -len(x[0])):
            if action.startswith(prefix):
                return module.call(action, a, privileged)
        if action == 'health_effect_owner_confirm':
            if not privileged:
                raise ValueError('Only the owner confirms an operation themselves (/healthconfirm EFFECT).')
            return self.owner_confirm(a)
        if action == 'health_status':
            return self.status()
        if action == 'health_policy':
            if 'set' in a:
                if not privileged:
                    raise ValueError('Only the owner changes the health policy (/healthpolicy).')
                s = a['set'] if isinstance(a['set'], dict) else {}
                out = self.policy()
                for k, v in s.items():
                    out = self.set_policy(k, v)
                return out
            return self.policy()
        if action == 'health_connection_add':
            if not privileged:
                raise ValueError('Only the owner adds a health connection (/healthconnect).')
            return self.add_connection(a)
        if action == 'health_connection_status':
            if not privileged:
                raise ValueError('Only the owner changes a connection.')
            return self.set_connection_status(clean(a.get('connection'), 'connection', 16), clean(a.get('status'), 'status', 20), a.get('why') or '')
        if action == 'health_grant_propose':
            return self.propose_grant(a)
        if action == 'health_grant_approve':
            if not privileged:
                raise ValueError('Only the owner approves a health grant (/healthgrant ID HASH).')
            return self.approve_grant(a)
        if action == 'health_grant_revise':
            if not privileged:
                raise ValueError('Only the owner revises a grant.')
            return self.revise_grant(a)
        if action == 'health_grant_revoke':
            if not privileged:
                raise ValueError('Only the owner revokes a grant (/healthrevoke GRANT).')
            return self.revoke_grant(a)
        if action == 'health_effect_begin':
            return self.begin_effect(a)
        if action == 'health_effect_dispatch':
            return self.dispatch_effect(a)
        if action == 'health_effect_settle':
            return self.settle_effect(a)
        if action == 'health_effect_reconcile':
            return self.reconcile_effect(a)
        if action == 'health_effect_get':
            with self.db() as db:
                e = db.execute('SELECT * FROM effects WHERE id=?', (clean(a.get('effect'), 'effect', 16),)).fetchone()
                if not e:
                    raise ValueError('Unknown effect.')
                events = [dict(r) for r in db.execute('SELECT at,state,source,detail FROM effect_events WHERE effect_id=? ORDER BY at', (e['id'],))]
                return {**self.public_effect(e), 'events': events}
        if action == 'health_receipt':
            return self.make_receipt(a)
        if action == 'health_receipt_read':
            if not privileged:
                raise ValueError('Receipts are read by the owner\'s private page only.')
            return self.receipt_by_token(a.get('token'), consume=bool(a.get('consume', True))) or {'error': 'no such receipt or expired'}
        if action == 'health_prune':
            if not privileged:
                raise ValueError('Owner only.')
            return {'pruned': self.prune()}
        raise ValueError('Unknown health action.')
