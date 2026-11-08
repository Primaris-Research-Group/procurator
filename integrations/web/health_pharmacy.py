"""Pharmacy client (web 0.23.0; build-plans/health/04-pharmacy-integration.md). Tested against the synthetic pharmacy in mock_rx.py;
CVS / Walgreens adapters reuse it once developer access is confirmed (their published docs do not establish PROCURATOR's access).

Sign-in is the same OAuth flow as the patient portals (/healthauth CONNECTION; health_portal.py, pharmacy scopes); this file speaks the
pharmacy API. Rules enforced here:
  * Exact matching. A prescription is identified by the pharmacy's id AND its rx number AND its store AND the signed-in patient. A
    similar drug name never counts: "Atorvastatin 20 mg at Midtown" and "Atorvastatin 20 mg at Decatur" are different prescriptions.
  * Reads (health_pharmacy_list / _eligibility / _refill_status) need an active grant with rx.status. Every record is checked against
    the bound patient before anything is returned; one mismatch drops the whole answer (identity_mismatch).
  * A refill is one rx.refill effect (health_effect_begin with payload prescription_id, rx_number, pharmacy = store id, fulfillment,
    cost_usd from the eligibility answer). health_pharmacy_refill rechecks the grant, re-reads the prescription and its eligibility,
    then POSTs with Idempotency-Key = the effect's request id. Outcomes:
        accepted ("received")              → REQUESTED (effect stays open; never shown as done)
        ready / picked_up / delivered      → confirmed, with the pharmacy's wording
        rejected / out_of_stock / renewal_requested / cancelled → failed, with the reason
        not eligible (too soon, no refills, expired) → failed before anything is sent; renewal is a prescriber action, not a refill
        already in progress                → awaiting_owner (an equivalent request exists outside this effect)
        timeout / 5xx / lost answer        → uncertain, then read back by idempotency key at once — never re-sent
  * Status updates: signed webhooks (HMAC-SHA256 over "t.body", 5-minute window, event ids kept for replay protection, versions applied
    in order — an older version never overwrites a newer one) and bounded polling. A failed status read never changes the state.
  * Telegram gets one neutral line; medication names stay in tool results and the private receipt.
"""
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse

from health import clean
from health_portal import PHARMACY, PortalError

ACCEPTED = ('received', 'processing')
DONE = ('ready', 'picked_up', 'delivered')
FAILED = ('rejected', 'out_of_stock', 'renewal_requested', 'cancelled')
WEBHOOK_WINDOW = 300
POLL_MIN_SECONDS = 600
NEXT = {
    'reauth_required': 'The pharmacy needs the owner to sign in again: /healthauth CONNECTION.',
    'identity_mismatch': 'STOP. The pharmacy returned another person\'s record; nothing was used. Tell the owner.',
    'rate_limited': 'The pharmacy is limiting requests; check again later (never re-send a refill).',
    'outage': 'The pharmacy is down; tell the owner and check later.',
    'not_linked': 'The owner has not signed in to this pharmacy yet: /healthauth CONNECTION.',
    'mismatch': 'The prescription the pharmacy shows does not match exactly (number, store or patient). Do not substitute another one; ask the owner.',
}


class Pharmacy:
    def __init__(self, health, portal, callback_base=None, now=None):
        self.h, self.p = health, portal
        self.callback_base = (callback_base or '').rstrip('/')
        self.now = now or health.now
        with self.h.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rx_refills(effect_id TEXT PRIMARY KEY,connection TEXT,refill_id TEXT,prescription_id TEXT,status TEXT,status_text TEXT,
                    version INTEGER,updated REAL,source TEXT,last_poll REAL);
                CREATE TABLE IF NOT EXISTS rx_events(event_id TEXT PRIMARY KEY,connection TEXT,refill_id TEXT,version INTEGER,status TEXT,at REAL,outcome TEXT);
                CREATE TABLE IF NOT EXISTS rx_webhooks(connection TEXT PRIMARY KEY,webhook_id TEXT,url TEXT,registered REAL);''')

    # ---------- plumbing ----------
    def conn(self, cid, need_active=True):
        c = self.p.conn(clean(cid, 'connection', 16), need_active=need_active)
        if c['provider'] not in PHARMACY:
            raise PortalError('not_a_pharmacy', 'connection %s is %s, not a pharmacy' % (c['id'], c['provider']))
        return c

    def patient(self, c):
        return self.p.link(c['id'])['patient_id']

    def need(self, c, grant, action='rx.status'):
        with self.h.db() as db:
            return self.h.grant_for(db, clean(grant, 'grant', 16), action, c['subject'], c['id'])

    def get(self, c, path, params=None):
        st, rh, b = self.p.request(c, 'GET', path, params)
        return st, rh, b

    def mine(self, c, records):
        pid = self.patient(c)
        for r in records:
            if not isinstance(r, dict) or r.get('patient') != pid:
                self.p.incident(c['id'], 'identity_mismatch', 'pharmacy record for another patient')
                raise PortalError('identity_mismatch', 'the pharmacy returned a record for another person; nothing was used')
        return records

    @staticmethod
    def norm(r, store=None):
        m = r.get('medication') or {}
        return {'prescription_id': r.get('id'), 'rx_number': r.get('rx_number'), 'store_id': r.get('store_id'), 'store': (store or {}).get('name'),
                'medication': ' '.join(x for x in (m.get('name'), m.get('strength'), m.get('form')) if x), 'refills_remaining': r.get('refills_remaining'),
                'last_fill_date': r.get('last_fill_date'), 'expires_on': r.get('expires_on'), 'status': r.get('status'), 'version': r.get('version'),
                'prescriber': r.get('prescriber')}

    def store(self, c, sid, cache):
        if sid not in cache:
            try:
                st, _, s = self.get(c, 'stores/' + urllib.parse.quote(str(sid)))
                cache[sid] = s if st == 200 else {}
            except PortalError:
                cache[sid] = {}
        return cache[sid]

    # ---------- reads ----------
    def list(self, a):
        c = self.conn(a.get('connection'))
        self.need(c, a.get('grant'))
        st, _, b = self.get(c, 'prescriptions')
        if st != 200 or not isinstance(b.get('prescriptions'), list):
            raise PortalError('bad_response', 'no prescription list (HTTP %s)' % st)
        self.mine(c, b['prescriptions'])
        stores = {}
        items = [self.norm(r, self.store(c, r.get('store_id'), stores)) for r in b['prescriptions']]
        return {'connection': c['id'], 'prescriptions': items,
                'note': 'Identify a prescription by prescription_id + rx_number + store_id. Same-name medicines with another strength, form or store are different prescriptions.'}

    def exact(self, c, prescription_id, rx_number, store_id):
        """The live record, only if every identifier matches exactly."""
        pid = clean(str(prescription_id or ''), 'prescription_id', 64)
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', pid):
            raise PortalError('bad_request', 'prescription_id is the pharmacy\'s id from health_pharmacy_list')
        st, rh, r = self.get(c, 'prescriptions/' + urllib.parse.quote(pid))
        if st == 404:
            raise PortalError('mismatch', 'the pharmacy has no prescription %s for this account' % pid)
        if st != 200:
            raise PortalError('bad_response', 'HTTP %s' % st)
        self.mine(c, [r])
        problems = []
        if rx_number is not None and str(r.get('rx_number')) != str(rx_number):
            problems.append('rx number is %s, not %s' % (r.get('rx_number'), rx_number))
        if store_id is not None and str(r.get('store_id')) != str(store_id):
            problems.append('store is %s, not %s' % (r.get('store_id'), store_id))
        if problems:
            raise PortalError('mismatch', '; '.join(problems))
        return r

    def eligibility(self, a):
        c = self.conn(a.get('connection'))
        self.need(c, a.get('grant'))
        r = self.exact(c, a.get('prescription_id'), a.get('rx_number'), a.get('store_id'))
        st, _, el = self.get(c, 'prescriptions/%s/eligibility' % urllib.parse.quote(r['id']))
        if st != 200 or 'eligible' not in el:
            raise PortalError('bad_response', 'no eligibility answer (HTTP %s)' % st)
        out = {'prescription': self.norm(r), 'eligible': bool(el['eligible']), 'reason': el.get('reason'), 'pharmacy_says': el.get('message'),
               'earliest_date': el.get('earliest_date'), 'cost_usd': el.get('copay_usd'), 'fulfillment': el.get('fulfillment')}
        if el.get('reason') in ('renewal_required', 'expired'):
            out['next'] = 'A refill is not possible: it needs a RENEWAL from the prescriber, which is a different request. Tell the owner; do not request a refill.'
        elif el.get('reason') == 'too_soon':
            out['next'] = 'Too soon; the pharmacy allows it from %s.' % el.get('earliest_date')
        elif el['eligible'] and el.get('copay_usd') is None:
            out['next'] = 'Eligible, but the cost is unknown; a grant with a spending cap stops on an unknown cost. Ask the owner.'
        elif el['eligible']:
            out['next'] = ('Eligible. Begin an rx.refill effect with payload {prescription_id, rx_number, pharmacy: store_id, fulfillment, cost_usd: %s}, '
                           'then health_pharmacy_refill.' % el.get('copay_usd'))
        return out

    # ---------- refill ----------
    def row(self, eid):
        with self.h.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (clean(eid, 'effect', 16),)).fetchone()
            r = db.execute('SELECT * FROM rx_refills WHERE effect_id=?', (eid,)).fetchone()
        if not e:
            raise PortalError('bad_request', 'unknown effect')
        return dict(e), (dict(r) if r else None)

    def record(self, eid, cid, f, source):
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO rx_refills VALUES(?,?,?,?,?,?,?,?,?,COALESCE((SELECT last_poll FROM rx_refills WHERE effect_id=?),NULL))',
                       (eid, cid, f.get('id'), f.get('prescription_id'), f.get('status'), str(f.get('status_text') or '')[:120], int(f.get('version') or 0), self.now(), source, eid))

    def apply(self, e, f, source):
        """Map a pharmacy refill status onto the effect. Returns the effect after the update."""
        st, text = f.get('status'), str(f.get('status_text') or f.get('status'))[:120]
        ps = '%s: %s' % (st, text)
        if e['state'] in ('dispatching',) and st in ACCEPTED:
            return self.h.settle_effect({'effect': e['id'], 'outcome': 'requested', 'source': source, 'external_id': 'refill/' + str(f.get('id')),
                                         'provider_state': ps, 'detail': 'the pharmacy received the request; it is not ready yet'})
        if st in DONE:
            return self.h.provider_update(e['id'], 'confirmed', source, ps, 'pharmacy reports: ' + text)
        if st in FAILED:
            return self.h.provider_update(e['id'], 'failed', source, ps, 'pharmacy reports: ' + text + (' — a renewal needs the prescriber' if st == 'renewal_requested' else ''))
        return self.h.provider_update(e['id'], 'pending', source, ps, 'pharmacy reports: ' + text)

    def refill(self, eid):
        e, _ = self.row(eid)
        if e['action'] != 'rx.refill':
            raise PortalError('bad_request', 'this effect is not rx.refill')
        payload = json.loads(e['payload'])
        c = self.conn(e['connection'])
        if e['state'] in ('authorized', 'queued'):
            self.h.dispatch_effect({'effect': e['id']})
        elif e['state'] != 'dispatching':
            raise PortalError('bad_state', 'effect is %s' % e['state'])
        try:
            r = self.exact(c, payload.get('prescription_id'), payload.get('rx_number'), payload.get('pharmacy'))
            st, _, el = self.get(c, 'prescriptions/%s/eligibility' % urllib.parse.quote(r['id']))
        except PortalError as err:
            if err.kind in ('mismatch', 'identity_mismatch'):
                self.h.settle_effect({'effect': e['id'], 'outcome': 'failed', 'source': 'pharmacy api', 'detail': 'not sent: ' + err.detail})
            else:
                self.h.settle_effect({'effect': e['id'], 'outcome': 'cancelled', 'source': 'pharmacy api', 'detail': 'not sent: could not read the prescription (%s)' % err.kind})
            raise
        if st == 200 and not el.get('eligible'):
            if el.get('reason') == 'in_progress':
                self.h.hold_effect(e['id'], 'the pharmacy already has a refill in progress for this prescription; nothing was sent')
                return {'effect': self.h.public_effect_by_id(e['id']), 'requested': False, 'next': 'Tell the owner a refill is already in progress at the pharmacy.'}
            out = self.h.settle_effect({'effect': e['id'], 'outcome': 'failed', 'source': 'pharmacy api', 'provider_state': 'not eligible: ' + str(el.get('reason')),
                                        'detail': 'not sent: ' + str(el.get('message') or el.get('reason')) + (' (from %s)' % el['earliest_date'] if el.get('earliest_date') else '')})
            return {'effect': out, 'requested': False, 'reason': el.get('reason')}
        if payload.get('cost_usd') is not None and el.get('copay_usd') is not None and float(el['copay_usd']) > float(payload['cost_usd']):
            out = self.h.settle_effect({'effect': e['id'], 'outcome': 'failed', 'source': 'pharmacy api',
                                        'detail': 'not sent: the cost is now $%.2f, more than the $%.2f this request was approved for' % (float(el['copay_usd']), float(payload['cost_usd']))})
            return {'effect': out, 'requested': False}
        self.need(c, e['grant_id'], 'rx.refill')       # last authority check before the write
        self.ensure_webhook(c)
        body = {'prescription_id': r['id'], 'store_id': r['store_id'], 'fulfillment': payload.get('fulfillment') or 'pickup'}
        try:
            st, _, f = self.p.request(c, 'POST', 'refills', data=body, headers={'Idempotency-Key': e['request_id']}, read=False)
        except PortalError as err:
            if err.kind in ('network', 'server_error', 'outage', 'rate_limited'):
                self.h.settle_effect({'effect': e['id'], 'outcome': 'uncertain', 'source': 'pharmacy api', 'detail': 'the refill request ended without a clear answer (%s)' % err.kind})
                return self.reconcile(e['id']) | {'requested': None}
            raise
        if st in (200, 201, 202) and f.get('id'):
            self.mine(c, [f])
            self.record(e['id'], c['id'], f, 'pharmacy api')
            out = self.apply(self.h.public_effect_by_id(e['id']), f, 'pharmacy api')
            return {'effect': out, 'requested': True, 'refill': {'id': f['id'], 'status': f['status'], 'status_text': f.get('status_text')},
                    'next': 'Tell the owner it is REQUESTED (not ready). The pharmacy reports progress; check with health_pharmacy_refill_status.'}
        if st == 409 and f.get('error') == 'refill_in_progress':
            self.h.hold_effect(e['id'], 'the pharmacy already has a refill in progress (%s); nothing new was sent' % f.get('refill_id'))
            return {'effect': self.h.public_effect_by_id(e['id']), 'requested': False}
        if st == 504:
            self.h.settle_effect({'effect': e['id'], 'outcome': 'uncertain', 'source': 'pharmacy api', 'detail': 'gateway timeout; the pharmacy may have recorded it'})
            return self.reconcile(e['id']) | {'requested': None}
        out = self.h.settle_effect({'effect': e['id'], 'outcome': 'failed', 'source': 'pharmacy api', 'provider_state': 'HTTP %s %s' % (st, f.get('error') or ''),
                                    'detail': str(f.get('message') or f.get('error') or 'refused')[:300]})
        return {'effect': out, 'requested': False, 'reason': f.get('error')}

    def find_refill(self, c, e):
        st, _, b = self.get(c, 'refills', {'idempotency_key': e['request_id']})
        if st != 200 or not isinstance(b.get('refills'), list):
            raise PortalError('bad_response', 'HTTP %s' % st)
        return self.mine(c, b['refills'])

    def reconcile(self, eid):
        """An uncertain refill: look it up by the idempotency key. Only a successful read decides."""
        e, _ = self.row(eid)
        c = self.conn(e['connection'], need_active=False)
        try:
            found = self.find_refill(c, e)
        except PortalError as err:
            return {'effect': self.h.public_effect_by_id(eid), 'reconciled': False, 'why': 'could not read the pharmacy (%s); still uncertain — do not request again' % err.kind}
        if not found:
            out = self.h.reconcile_effect({'effect': eid, 'found': 'absent', 'source': 'pharmacy read by idempotency key', 'detail': 'the pharmacy has no request with this key'})
            return {'effect': out, 'reconciled': True}
        f = found[0]
        self.record(eid, c['id'], f, 'pharmacy read')
        if f['status'] in ACCEPTED:
            out = self.h.provider_update(eid, 'pending', 'pharmacy read by idempotency key', '%s: %s' % (f['status'], f.get('status_text')), 'the pharmacy has the request; not ready yet')
            with self.h.db() as db:
                db.execute("UPDATE effects SET external_id=?,provider_accepted_at=COALESCE(provider_accepted_at,?) WHERE id=?", ('refill/' + f['id'], self.now(), eid))
            out = self.h.public_effect_by_id(eid)
        else:
            out = self.apply(self.h.public_effect_by_id(eid), f, 'pharmacy read by idempotency key')
        return {'effect': out, 'reconciled': True, 'refill': {'id': f['id'], 'status': f['status'], 'status_text': f.get('status_text')}}

    def refill_status(self, a):
        """Bounded polling: at most one read per refill every 10 minutes unless forced by the owner."""
        e, r = self.row(a.get('effect'))
        c = self.conn(e['connection'], need_active=False)
        self.need(c, e['grant_id'], 'rx.status')
        if not r:
            if e['state'] == 'uncertain':
                return self.reconcile(e['id'])
            return {'effect': self.h.public_effect_by_id(e['id']), 'refill': None}
        if r['last_poll'] and self.now() - r['last_poll'] < POLL_MIN_SECONDS and not a.get('force'):
            return {'effect': self.h.public_effect_by_id(e['id']), 'refill': {'id': r['refill_id'], 'status': r['status'], 'status_text': r['status_text']},
                    'note': 'checked less than 10 minutes ago; this is the last known status'}
        with self.h.db() as db:
            db.execute('UPDATE rx_refills SET last_poll=? WHERE effect_id=?', (self.now(), e['id']))
        try:
            st, _, f = self.get(c, 'refills/' + urllib.parse.quote(r['refill_id']))
            if st != 200:
                raise PortalError('bad_response', 'HTTP %s' % st)
            self.mine(c, [f])
        except PortalError as err:
            # A failed read never changes the state.
            return {'effect': self.h.public_effect_by_id(e['id']), 'refill': {'id': r['refill_id'], 'status': r['status']}, 'read_failed': err.kind,
                    'next': NEXT.get(err.kind, 'Could not read the pharmacy; the last known status stands.')}
        return self.take(e, r, f, 'pharmacy poll')

    def take(self, e, r, f, source):
        if int(f.get('version') or 0) <= int(r['version'] or 0) and f.get('status') == r['status']:
            return {'effect': self.h.public_effect_by_id(e['id']), 'refill': {'id': f['id'], 'status': f['status'], 'status_text': f.get('status_text')}, 'changed': False}
        if int(f.get('version') or 0) < int(r['version'] or 0):
            return {'effect': self.h.public_effect_by_id(e['id']), 'refill': {'id': r['refill_id'], 'status': r['status']}, 'changed': False, 'stale': True}
        self.record(e['id'], e['connection'], f, source)
        out = self.apply(self.h.public_effect_by_id(e['id']), f, source)
        return {'effect': out, 'refill': {'id': f['id'], 'status': f['status'], 'status_text': f.get('status_text')}, 'changed': True}

    # ---------- webhooks ----------
    def secret_path(self, cid):
        return self.h.tokens / ('rxhook_%s.json' % cid)

    def ensure_webhook(self, c):
        """Register the pharmacy callback once per connection (best effort; polling still works without it)."""
        if not self.callback_base:
            return None
        with self.h.db() as db:
            if db.execute('SELECT 1 FROM rx_webhooks WHERE connection=?', (c['id'],)).fetchone():
                return True
        url = '%s/%s' % (self.callback_base, c['id'])
        try:
            st, _, b = self.p.request(c, 'POST', 'webhooks', data={'url': url}, read=False)
        except PortalError:
            return False
        if st not in (200, 201) or not b.get('secret'):
            return False
        path = self.secret_path(c['id'])
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as fh:
            fh.write(json.dumps({'secret': b['secret'], 'id': b.get('id')}))
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO rx_webhooks VALUES(?,?,?,?)', (c['id'], b.get('id'), url, self.now()))
        return True

    def webhook(self, cid, headers, raw):
        """A pharmacy callback (dashboard /health/rx-callback/<connection>). Returns (http status, result)."""
        if not re.fullmatch(r'[a-f0-9]{16}', str(cid or '')):
            return 404, {'error': 'unknown'}
        try:
            secret = json.loads(self.secret_path(cid).read_text())['secret']
        except (OSError, ValueError, KeyError):
            return 404, {'error': 'unknown'}
        sig = next((v for k, v in headers.items() if k.lower() == 'x-rx-signature'), '')
        m = re.fullmatch(r't=(\d{1,12}),v1=([a-f0-9]{64})', sig or '')
        if not m:
            return 401, {'error': 'bad_signature'}
        t = int(m.group(1))
        want = hmac.new(secret.encode(), b'%d.' % t + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, m.group(2)):
            return 401, {'error': 'bad_signature'}
        if abs(self.now() - t) > WEBHOOK_WINDOW:
            return 401, {'error': 'stale_signature'}
        try:
            ev = json.loads(raw)
        except ValueError:
            return 400, {'error': 'bad_json'}
        eid_ = str(ev.get('event_id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{4,80}', eid_):
            return 400, {'error': 'bad_event'}
        with self.h.db() as db:
            if db.execute('SELECT 1 FROM rx_events WHERE event_id=?', (eid_,)).fetchone():
                return 200, {'duplicate': True}
            r = db.execute('SELECT * FROM rx_refills WHERE connection=? AND refill_id=?', (cid, str(ev.get('refill_id')))).fetchone()
        outcome = 'unmatched'
        result = {'ok': True}
        try:
            c = self.conn(cid, need_active=False)
            pid = self.patient(c)
        except PortalError:
            c, pid = None, None
        if c and ev.get('patient') != pid:
            outcome = 'wrong_patient'
            self.p.incident(cid, 'identity_mismatch', 'webhook for another patient')
        elif r:
            e, _ = self.row(r['effect_id'])
            f = {'id': ev.get('refill_id'), 'prescription_id': ev.get('prescription_id'), 'status': ev.get('status'), 'status_text': ev.get('status_text'), 'version': ev.get('version')}
            if f['prescription_id'] != r['prescription_id']:
                outcome = 'prescription_mismatch'
            else:
                out = self.take(e, dict(r), f, 'pharmacy webhook')
                outcome = 'stale' if out.get('stale') else ('applied' if out.get('changed') else 'same')
                result.update(changed=out.get('changed', False), effect=e['id'], state=out['effect']['state'])
                if out.get('changed') and ev.get('status') in DONE + FAILED:
                    result['notify'] = 'A pharmacy request has an update. Details: /healthstatus (private).'
        with self.h.db() as db:
            db.execute('INSERT INTO rx_events VALUES(?,?,?,?,?,?,?)', (eid_, cid, str(ev.get('refill_id')), int(ev.get('version') or 0), str(ev.get('status'))[:30], self.now(), outcome))
        result['outcome'] = outcome
        return 200, result

    # ---------- dispatch ----------
    def call(self, action, a, privileged=False):
        try:
            if action == 'health_pharmacy_list':
                return self.list(a)
            if action == 'health_pharmacy_eligibility':
                return self.eligibility(a)
            if action == 'health_pharmacy_refill':
                return self.refill(clean(a.get('effect'), 'effect', 16))
            if action == 'health_pharmacy_refill_status':
                return self.refill_status({'effect': a.get('effect'), 'force': bool(a.get('force')) and privileged})
            if action == 'health_pharmacy_reconcile':
                return self.reconcile(clean(a.get('effect'), 'effect', 16))
            if action == 'health_pharmacy_webhook':
                if not privileged:
                    raise PortalError('owner_only', 'pharmacy callbacks come from the dashboard only')
                st, out = self.webhook(a.get('connection'), a.get('headers') or {}, (a.get('body') or '').encode())
                return out | {'http_status': st}
        except PortalError as e:
            return {'error': e.kind, 'detail': e.detail, 'next': NEXT.get(e.kind, 'Tell the owner in one line what happened; do not re-send a refill.')}
        raise ValueError('Unknown pharmacy action.')
