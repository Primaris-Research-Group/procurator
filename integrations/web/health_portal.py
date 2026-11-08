"""Patient-portal API client (web 0.22.0, consumer health module; build-plans/health/03-patient-portal-apis.md).

Connects ONE patient's account at ONE institution through the vendor's SMART-on-FHIR standalone patient launch, reads
selected records with provenance, and books appointments through the shared effect contract (health.py). Tested against
mock_ehr.py; the same code targets an Epic/Oracle sandbox once registered.

Rules (enforced here, not in prompts):
  * Endpoints come from the connection's reviewed FHIR base and its SMART configuration; authorize/token endpoints must be on the
    same host as that base, the issuer must equal it, and S256 PKCE must be offered. Plain http is accepted only for loopback
    in a `fake` connection (the mock).
  * The OAuth `state` is single-use and expires in 10 minutes; the PKCE verifier never leaves the server.
  * The token response must carry a patient context; that patient is bound to the connection. Re-authorizing as a different
    patient (account switch) is refused and the existing binding kept. Every resource read is checked against the bound
    patient BEFORE it is returned; one mismatch drops the whole read.
  * Pagination follows only `next` links under the same FHIR base; anything else stops with `foreign_url`.
  * Distinct states: reauth_required (invalid_grant / revoked consent), refresh_uncertain (refresh response lost),
    insufficient_scope, rate_limited, outage, server_error, identity_mismatch, missing_patient_context, foreign_url.
  * Tokens live in health.py's token store (server-side files); nothing here returns them.
  * Booking: health_effect_dispatch (grant recheck) → re-read the Slot (still free, same time) → POST Appointment with
    `If-None-Exist: identifier=<request id>` (provider idempotency) → settle: booked = confirmed, pending/proposed = requested,
    4xx = failed, timeout/5xx/lost response = uncertain → immediately READ by identifier (reconcile) before anything else.
"""
import base64
import datetime as dt
import hashlib
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from health import Health, clean

IDENT_SYSTEM = 'urn:procurator:request'
DEFAULT_SCOPES = ('launch/patient openid fhirUser offline_access patient/Patient.rs patient/Appointment.rs patient/Appointment.c patient/Appointment.u '
                  'patient/MedicationRequest.rs patient/AllergyIntolerance.rs patient/Observation.rs user/Practitioner.rs user/Location.rs user/Schedule.rs user/Slot.rs')
CLIENT_ID = 'procurator-health'
READS = {'appointments': 'Appointment', 'medications': 'MedicationRequest', 'allergies': 'AllergyIntolerance', 'observations': 'Observation', 'slots': 'Slot'}
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
PHARMACY = ('cvs', 'walgreens', 'mock_pharmacy')          # 0.23.0: pharmacy accounts use the same sign-in; their API is health_pharmacy.py
PHARMACY_SCOPES = 'launch/patient openid offline_access rx/prescriptions.read rx/refills.write'


class PortalError(ValueError):
    def __init__(self, kind, detail=''):
        self.kind, self.detail = kind, detail
        super().__init__('%s: %s' % (kind, detail) if detail else kind)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def origin(url):
    p = urllib.parse.urlsplit(url or '')
    return (p.scheme, (p.hostname or '').lower(), p.port)


class Portal:
    def __init__(self, health, redirect_uri, client_id=CLIENT_ID, now=time.time, sleep=time.sleep, timeout=20):
        self.h, self.redirect_uri, self.client_id, self.now, self.sleep, self.timeout = health, redirect_uri, client_id, now, sleep, timeout
        self.locks = {}
        self.opener = urllib.request.build_opener(NoRedirect)
        with self.h.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS portal_auth(state_hash TEXT PRIMARY KEY,connection TEXT,verifier TEXT,redirect TEXT,token_endpoint TEXT,created REAL,expires REAL,used REAL);
                CREATE TABLE IF NOT EXISTS portal_links(connection TEXT PRIMARY KEY,iss TEXT,patient_id TEXT,patient_name TEXT,scopes TEXT,linked_at REAL,last_ok REAL,last_error TEXT);
                CREATE TABLE IF NOT EXISTS portal_incidents(at REAL,connection TEXT,kind TEXT,detail TEXT);''')

    # ---------- plumbing ----------
    def lock(self, cid):
        return self.locks.setdefault(cid, threading.Lock())

    def conn(self, cid, need_active=False):
        with self.h.db() as db:
            c = self.h.connection(db, cid, need_active=need_active)
        if c['provider'] not in ('epic', 'oracle', 'mock_ehr') + PHARMACY:
            raise PortalError('not_a_portal', 'connection %s is %s, not a patient-portal or pharmacy API' % (cid, c['provider']))
        base = (c['endpoint'] or '').rstrip('/')
        sch, host, _ = origin(base)
        if not base or not (sch == 'https' or (sch == 'http' and host in LOOPBACK and c['environment'] == 'fake')):
            raise PortalError('bad_endpoint', 'the connection needs an https FHIR base address (http only for the loopback mock)')
        c['base'] = base
        return c

    def incident(self, cid, kind, detail=''):
        with self.h.db() as db:
            db.execute('INSERT INTO portal_incidents VALUES(?,?,?,?)', (self.now(), cid, kind, str(detail)[:300]))
            db.execute('UPDATE portal_links SET last_error=? WHERE connection=?', (kind, cid))

    def http(self, method, url, headers=None, data=None, form=None):
        """(status, headers, body bytes). Network failure raises PortalError('network')."""
        body = urllib.parse.urlencode(form).encode() if form is not None else (json.dumps(data).encode() if data is not None else None)
        h = {'Accept': 'application/fhir+json, application/json'}
        if form is not None:
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        elif data is not None:
            h['Content-Type'] = 'application/fhir+json'
        h.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b''
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            raise PortalError('network', type(e).__name__)

    # ---------- discovery + authorization ----------
    def discover(self, c):
        status, _, body = self.http('GET', c['base'] + '/.well-known/smart-configuration')
        if status != 200:
            raise PortalError('discovery_failed', 'HTTP %s' % status)
        cfg = json.loads(body or b'{}')
        if cfg.get('issuer') and cfg['issuer'].rstrip('/') != c['base']:
            raise PortalError('issuer_mismatch', 'issuer %s is not the connection\'s FHIR base' % cfg['issuer'])
        for k in ('authorization_endpoint', 'token_endpoint'):
            if origin(cfg.get(k)) != origin(c['base']):
                raise PortalError('foreign_endpoint', '%s is on another host' % k)
        if 'S256' not in (cfg.get('code_challenge_methods_supported') or []):
            raise PortalError('no_pkce', 'the server does not offer S256 PKCE')
        return cfg

    def begin(self, cid, scopes=DEFAULT_SCOPES):
        c = self.conn(cid)
        if c['provider'] in PHARMACY and scopes == DEFAULT_SCOPES:
            scopes = PHARMACY_SCOPES
        cfg = self.discover(c)
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        with self.h.db() as db:
            db.execute('DELETE FROM portal_auth WHERE expires<?', (self.now() - 86400,))
            db.execute('INSERT INTO portal_auth VALUES(?,?,?,?,?,?,?,NULL)', (hashlib.sha256(state.encode()).hexdigest(), cid, verifier, self.redirect_uri,
                       cfg['token_endpoint'], self.now(), self.now() + 600))
        q = {'response_type': 'code', 'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'scope': scopes, 'state': state, 'aud': c['base'],
             'code_challenge': b64url(hashlib.sha256(verifier.encode()).digest()), 'code_challenge_method': 'S256'}
        return {'connection': cid, 'authorize_url': cfg['authorization_endpoint'] + '?' + urllib.parse.urlencode(q), 'expires_minutes': 10}

    def complete(self, state, code=None, error=None):
        h = hashlib.sha256(str(state or '').encode()).hexdigest()
        with self.h.db() as db:
            p = db.execute('SELECT * FROM portal_auth WHERE state_hash=?', (h,)).fetchone()
            if not p:
                raise PortalError('bad_state', 'unknown sign-in attempt')
            if p['used']:
                raise PortalError('replayed_callback', 'this sign-in link was already used')
            db.execute('UPDATE portal_auth SET used=? WHERE state_hash=?', (self.now(), h))     # single use, before anything else
        cid = p['connection']
        if p['expires'] < self.now():
            raise PortalError('expired_state', 'the sign-in took longer than 10 minutes; start again')
        if error:
            self.incident(cid, 'authorization_denied', error)
            raise PortalError('authorization_denied', str(error)[:80])
        c = self.conn(cid)
        status, _, body = self.http('POST', p['token_endpoint'], form={'grant_type': 'authorization_code', 'code': code or '', 'redirect_uri': p['redirect'],
                                                                      'client_id': self.client_id, 'code_verifier': p['verifier']})
        tok = json.loads(body or b'{}') if body else {}
        if status != 200 or not tok.get('access_token'):
            raise PortalError('token_exchange_failed', tok.get('error') or 'HTTP %s' % status)
        if str(tok.get('token_type', '')).lower() != 'bearer':
            raise PortalError('token_exchange_failed', 'unexpected token type')
        pid = tok.get('patient')
        if not pid:
            self.incident(cid, 'missing_patient_context')
            raise PortalError('missing_patient_context', 'the server did not say which patient this sign-in is for; nothing was saved')
        with self.h.db() as db:
            link = db.execute('SELECT * FROM portal_links WHERE connection=?', (cid,)).fetchone()
        if link and link['patient_id'] != pid:
            self.incident(cid, 'account_switch', 'signed in as a different patient')
            raise PortalError('account_switch', 'this sign-in is for a different patient than the one connected; the existing connection was kept')
        if c['provider'] in PHARMACY:
            st, _, pb = self.http('GET', c['base'] + '/patient', headers={'Authorization': 'Bearer ' + tok['access_token']})
            patient = json.loads(pb or b'{}') if st == 200 else {}
            if patient.get('id') != pid:
                raise PortalError('identity_mismatch', 'the pharmacy account does not match the sign-in')
            name = str(patient.get('name') or '').strip()[:120]
        else:
            st, _, pb = self.http('GET', '%s/Patient/%s' % (c['base'], urllib.parse.quote(pid)), headers={'Authorization': 'Bearer ' + tok['access_token']})
            patient = json.loads(pb or b'{}') if st == 200 else {}
            if patient.get('resourceType') != 'Patient' or patient.get('id') != pid:
                raise PortalError('identity_mismatch', 'the patient record does not match the sign-in')
            name = ((patient.get('name') or [{}])[0].get('text') or '').strip()[:120]
        self.h.set_token(cid, {'access_token': tok['access_token'], 'refresh_token': tok.get('refresh_token'), 'expires_at': self.now() + int(tok.get('expires_in') or 3600),
                               'scope': tok.get('scope', ''), 'patient': pid})
        granted = (tok.get('scope') or '').split()
        if c['provider'] in PHARMACY:
            caps = {'rx.status': 'supported' if 'rx/prescriptions.read' in granted else 'unsupported', 'rx.refill': 'supported' if 'rx/refills.write' in granted else 'unsupported',
                    'rx.transfer': 'unsupported', 'rx.renewal': 'unsupported'}
        else:
            caps = {'portal.read': 'supported', 'appointment.search': 'supported' if any('Slot' in s for s in granted) else 'unsupported',
                    'appointment.book': 'supported' if any(re.search(r'Appointment\.(write|[cruds]*c)', s) for s in granted) else 'unsupported',
                    'appointment.cancel': 'supported' if any(re.search(r'Appointment\.(write|[cruds]*u)', s) for s in granted) else 'unsupported',
                    'rx.refill': 'unsupported'}
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO portal_links VALUES(?,?,?,?,?,?,?,NULL)', (cid, c['base'], pid, name, tok.get('scope', ''), self.now(), self.now()))
            db.execute('UPDATE connections SET capabilities=?,updated=? WHERE id=?', (json.dumps(caps), self.now(), cid))
        return {'connection': cid, 'status': 'connected', 'patient_display': name, 'scopes_granted': granted, 'capabilities': caps}

    def link(self, cid):
        with self.h.db() as db:
            r = db.execute('SELECT * FROM portal_links WHERE connection=?', (cid,)).fetchone()
        if not r:
            raise PortalError('not_linked', 'this connection has not been authorized yet (/healthauth)')
        return dict(r)

    # ---------- tokens ----------
    def access_token(self, c, force=False):
        with self.lock(c['id']):
            tok = self.h.token(c['id'])
            if not tok:
                raise PortalError('reauth_required', 'no stored authorization')
            if not force and tok.get('expires_at', 0) - 60 > self.now():
                return tok['access_token']
            if not tok.get('refresh_token'):
                self.h.set_connection_status(c['id'], 'reauth_required', 'access expired and no refresh token')
                raise PortalError('reauth_required', 'access expired')
            cfg = self.discover(c)
            try:
                status, _, body = self.http('POST', cfg['token_endpoint'], form={'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'], 'client_id': self.client_id})
            except PortalError:
                # The server may have rotated the refresh token and the answer was lost: the stored one is probably spent now.
                self.incident(c['id'], 'refresh_uncertain')
                raise PortalError('refresh_uncertain', 'the refresh answer was lost; if the next try fails, the owner must re-authorize (/healthauth)')
            new = json.loads(body or b'{}') if body else {}
            if status != 200 or not new.get('access_token'):
                self.h.set_connection_status(c['id'], 'reauth_required', 'refresh refused: %s' % (new.get('error') or status))
                raise PortalError('reauth_required', 'the portal refused the stored authorization (revoked or expired); the owner must re-authorize (/healthauth)')
            if new.get('patient') and new['patient'] != tok.get('patient'):
                self.incident(c['id'], 'identity_mismatch', 'refresh returned another patient')
                raise PortalError('identity_mismatch', 'refresh returned another patient')
            self.h.set_token(c['id'], {**tok, 'access_token': new['access_token'], 'refresh_token': new.get('refresh_token') or tok['refresh_token'],
                                       'expires_at': self.now() + int(new.get('expires_in') or 3600), 'scope': new.get('scope', tok.get('scope', ''))})
            return new['access_token']

    # ---------- FHIR ----------
    def url_for(self, c, path_or_url, params=None):
        if path_or_url.startswith(('http://', 'https://')):
            if not (path_or_url == c['base'] or path_or_url.startswith(c['base'] + '/')) or origin(path_or_url) != origin(c['base']):
                raise PortalError('foreign_url', 'a link pointed outside %s; stopped' % c['base'])
            return path_or_url
        u = c['base'] + '/' + path_or_url.lstrip('/')
        return u + ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')

    def request(self, c, method, path_or_url, params=None, data=None, headers=None, read=True):
        url = self.url_for(c, path_or_url, params)
        tries = 0
        while True:
            tries += 1
            h = {'Authorization': 'Bearer ' + self.access_token(c), **(headers or {})}
            status, rh, body = self.http(method, url, headers=h, data=data)
            if status == 401 and tries == 1:
                self.access_token(c, force=True)
                continue
            if status == 401:
                self.h.set_connection_status(c['id'], 'reauth_required', 'access refused')
                raise PortalError('reauth_required', 'the portal no longer accepts this authorization (consent removed or expired)')
            if status == 429 and read and tries <= 3:
                wait = min(5.0, float(rh.get('Retry-After') or 1))
                self.sleep(wait)
                continue
            if status == 429:
                raise PortalError('rate_limited', 'the portal is rate-limiting requests; try later')
            if status == 403:
                raise PortalError('insufficient_scope', self.diagnostic(body) or 'not permitted by the granted scopes')
            if status == 503:
                raise PortalError('outage', self.diagnostic(body) or 'portal unavailable')
            if status >= 500 and read and tries <= 2:
                self.sleep(1)
                continue
            if status >= 500:
                raise PortalError('server_error', 'HTTP %s %s' % (status, self.diagnostic(body)))
            with self.h.db() as db:
                db.execute('UPDATE portal_links SET last_ok=? WHERE connection=?', (self.now(), c['id']))
            return status, rh, (json.loads(body) if body else {})

    @staticmethod
    def diagnostic(body):
        try:
            o = json.loads(body or b'{}')
            return '; '.join(str(i.get('diagnostics') or '') for i in o.get('issue', []) if i.get('diagnostics'))[:200]
        except ValueError:
            return ''

    def search(self, c, rtype, params, max_pages=5):
        """All matching resources (and _include'd ones) with completeness."""
        url, params = rtype, dict(params)
        found, included, pages, total = [], [], 0, None
        while url and pages < max_pages:
            status, _, b = self.request(c, 'GET', url, params)
            if status != 200 or b.get('resourceType') != 'Bundle':
                raise PortalError('bad_response', 'expected a Bundle (HTTP %s)' % status)
            pages += 1
            total = b.get('total', total)
            for e in b.get('entry') or []:
                r = e.get('resource') or {}
                (included if (e.get('search') or {}).get('mode') == 'include' else found).append(r)
            nxt = next((l.get('url') for l in b.get('link') or [] if l.get('relation') == 'next'), None)
            url, params = (nxt, None) if nxt else (None, None)
        complete = url is None and (total is None or len(found) >= total)
        return found, included, complete

    def check_subject(self, c, resources):
        """Every Patient reference in every resource must be the bound patient; otherwise nothing is returned."""
        pid = self.link(c['id'])['patient_id']
        for r in resources:
            refs = []
            for k in ('subject', 'patient'):
                if isinstance(r.get(k), dict):
                    refs.append(r[k].get('reference', ''))
            for p in r.get('participant') or []:
                refs.append((p.get('actor') or {}).get('reference', ''))
            if r.get('resourceType') == 'Patient':
                refs.append('Patient/' + str(r.get('id')))
            bad = [x for x in refs if x.startswith('Patient/') and x != 'Patient/' + pid]
            if bad or (r.get('resourceType') in ('Observation', 'MedicationRequest', 'AllergyIntolerance', 'Appointment') and 'Patient/' + pid not in refs):
                self.incident(c['id'], 'identity_mismatch', '%s/%s' % (r.get('resourceType'), r.get('id')))
                raise PortalError('identity_mismatch', 'the portal returned a record for another patient; nothing from this read was used')

    def provenance(self, c, r):
        meta = r.get('meta') or {}
        return {'institution': c['institution'], 'environment': c['environment'], 'resource': '%s/%s' % (r.get('resourceType'), r.get('id')),
                'version': meta.get('versionId'), 'last_updated': meta.get('lastUpdated'), 'retrieved_at': dt.datetime.fromtimestamp(self.now(), dt.timezone.utc).isoformat(timespec='seconds')}

    # ---------- normalized reads ----------
    def read(self, c, what, args):
        if what not in READS:
            raise PortalError('bad_request', 'what is one of: ' + ', '.join(READS))
        pid = self.link(c['id'])['patient_id']
        if what == 'slots':
            return self.slots(c, args)
        params = {'patient': pid, '_count': 20}
        if what == 'appointments':
            if args.get('from'):
                params['date'] = 'ge' + args['from']
            if args.get('status'):
                params['status'] = args['status']
        if what == 'observations' and args.get('code'):
            params['code'] = 'http://loinc.org|' + args['code']
        found, _, complete = self.search(c, READS[what], params)
        self.check_subject(c, found)
        items = [getattr(self, 'norm_' + what)(r) | {'source': self.provenance(c, r)} for r in found]
        return {'what': what, 'items': items, 'complete': complete, 'count': len(items),
                'note': None if complete else 'INCOMPLETE: the portal reported more results than it returned; say so instead of treating this as the full list.'}

    @staticmethod
    def actor(r, kind):
        return next(((p.get('actor') or {}) for p in r.get('participant') or [] if (p.get('actor') or {}).get('reference', '').startswith(kind + '/')), {})

    def norm_appointments(self, r):
        return {'id': r['id'], 'status': r.get('status'), 'start': r.get('start'), 'end': r.get('end'), 'service': ((r.get('serviceType') or [{}])[0]).get('text'),
                'clinician': self.actor(r, 'Practitioner').get('display'), 'location': self.actor(r, 'Location').get('display'),
                'identifiers': ['%s|%s' % (i.get('system'), i.get('value')) for i in r.get('identifier') or []]}

    @staticmethod
    def norm_medications(r):
        dr = r.get('dispenseRequest') or {}
        return {'id': r['id'], 'medication': (r.get('medicationCodeableConcept') or {}).get('text'), 'status': r.get('status'),
                'instructions': ' '.join(x.get('text', '') for x in r.get('dosageInstruction') or []), 'refills_allowed': dr.get('numberOfRepeatsAllowed'),
                'quantity': dr.get('quantity'), 'authored': r.get('authoredOn')}

    @staticmethod
    def norm_allergies(r):
        return {'id': r['id'], 'substance': (r.get('code') or {}).get('text'), 'criticality': r.get('criticality'),
                'reactions': [m.get('text') for x in r.get('reaction') or [] for m in x.get('manifestation') or []]}

    @staticmethod
    def norm_observations(r):
        q = r.get('valueQuantity') or {}
        return {'id': r['id'], 'what': (r.get('code') or {}).get('text'), 'loinc': ((r.get('code') or {}).get('coding') or [{}])[0].get('code'),
                'value': q.get('value'), 'unit': q.get('code') or q.get('unit'), 'when': r.get('effectiveDateTime'), 'status': r.get('status')}

    def slots(self, c, args):
        params = {'status': 'free', '_include': 'Slot:schedule', '_count': 50}
        if args.get('practitioner'):
            params['schedule.actor'] = 'Practitioner/' + clean(args['practitioner'], 'practitioner', 64)
        if args.get('service_type'):
            params['service-type'] = clean(args['service_type'], 'service_type', 40)
        start = []
        if args.get('from'):
            start.append('ge' + args['from'])
        if args.get('to'):
            start.append('lt' + args['to'])
        if start:
            params['start'] = start
        found, included, complete = self.search(c, 'Slot', params)
        sched = {s['id']: s for s in included if s.get('resourceType') == 'Schedule'}
        items = []
        observed = dt.datetime.fromtimestamp(self.now(), dt.timezone.utc).isoformat(timespec='seconds')
        for r in found:
            s = sched.get(((r.get('schedule') or {}).get('reference') or '')[9:], {})
            prac = next((a for a in s.get('actor', []) if a.get('reference', '').startswith('Practitioner/')), {})
            loc = next((a for a in s.get('actor', []) if a.get('reference', '').startswith('Location/')), {})
            start = dt.datetime.fromisoformat(r['start'])
            items.append({'slot_id': r['id'], 'start': r['start'], 'end': r.get('end'), 'date': r['start'][:10], 'time': r['start'][11:16], 'weekday': start.strftime('%A'),
                          'time_of_day': 'morning' if start.hour < 12 else 'afternoon' if start.hour < 17 else 'evening',
                          'visit_type': ((r.get('serviceType') or [{}])[0]).get('text'), 'clinician': prac.get('display'), 'practitioner_id': prac.get('reference', '')[13:],
                          'location': loc.get('display'), 'observed_at': observed, 'source': self.provenance(c, r)})
        return {'what': 'slots', 'items': items, 'complete': complete, 'count': len(items),
                'note': ('Slots are a snapshot at observed_at; booking re-checks the slot first. ' + ('' if complete else 'INCOMPLETE list.')).strip()}

    # ---------- booking through effects ----------
    def book(self, eid):
        e = self.effect_row(eid)
        if e['action'] != 'appointment.book':
            raise PortalError('bad_request', 'this effect is not a booking')
        payload = json.loads(e['payload'])
        slot_id = payload.get('slot_id')
        if not slot_id:
            raise PortalError('bad_request', 'the effect payload needs slot_id (from health_portal_read what=slots)')
        c = self.conn(e['connection'], need_active=True)
        pid = self.link(c['id'])['patient_id']
        if e['state'] in ('authorized', 'queued'):
            self.h.dispatch_effect({'effect': eid})        # grant + limits rechecked; lease taken
        elif e['state'] != 'dispatching':
            raise PortalError('bad_state', 'effect is %s' % e['state'])
        # Revalidate the slot immediately before the write.
        try:
            st, _, slot = self.request(c, 'GET', 'Slot/' + urllib.parse.quote(slot_id))
        except PortalError as err:
            self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': 'could not re-check the slot before booking: ' + err.kind})
            raise
        if st != 200 or slot.get('status') != 'free':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'provider_state': 'slot ' + str(slot.get('status') or st),
                                        'detail': 'the slot is no longer free; nothing was booked'})
            return {'effect': out, 'booked': False, 'why': 'slot no longer available'}
        if payload.get('date') and slot.get('start', '')[:10] != payload['date'] or payload.get('time') and slot.get('start', '')[11:16] != payload['time']:
            out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': 'the slot time changed (%s) from what was authorized' % slot.get('start')})
            return {'effect': out, 'booked': False, 'why': 'slot time changed'}
        # An existing upcoming appointment with the same clinician (made outside PROCURATOR) is an equivalent booking.
        try:
            existing, _, _ = self.search(c, 'Appointment', {'patient': pid, 'status': 'booked,pending,proposed', 'date': 'ge' + dt.date.fromtimestamp(self.now()).isoformat()})
            self.check_subject(c, existing)
        except PortalError as err:
            if err.kind == 'identity_mismatch':
                self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': 'identity check failed; nothing was booked'})
                raise
            existing = []
        sched_actor = None
        try:
            _, _, sch = self.request(c, 'GET', (slot.get('schedule') or {}).get('reference', ''))
            sched_actor = next((a.get('reference') for a in sch.get('actor', []) if a.get('reference', '').startswith('Practitioner/')), None)
            loc_ref = next((a.get('reference') for a in sch.get('actor', []) if a.get('reference', '').startswith('Location/')), None)
        except PortalError:
            loc_ref = None
        same = [a for a in existing if sched_actor and self.actor(a, 'Practitioner').get('reference') == sched_actor
                and not any(i.get('system') == IDENT_SYSTEM and i.get('value') == e['request_id'] for i in a.get('identifier') or [])]
        if same:
            self.h.hold_effect(eid, 'the portal already shows an upcoming %s appointment with this clinician (%s, %s); not booking another' % (
                same[0].get('status'), same[0].get('start'), same[0].get('id')))
            return {'effect': self.h.public_effect_by_id(eid), 'booked': False, 'why': 'an equivalent appointment already exists in the portal'}
        body = self.appointment_body(pid, slot_id, e['request_id'], sched_actor, loc_ref, payload)
        # Final authority check right before the consequential write.
        with self.h.db() as db:
            g = self.h.grant_for(db, e['grant_id'], 'appointment.book', e['subject'], e['connection'])
        if g['revision'] != e['grant_revision']:
            self.h.settle_effect({'effect': eid, 'outcome': 'cancelled', 'source': 'broker', 'detail': 'grant revised before the write'})
            return {'effect': self.h.public_effect_by_id(eid), 'booked': False, 'why': 'grant changed'}
        try:
            st, _, appt = self.request(c, 'POST', 'Appointment', data=body, headers={'If-None-Exist': 'identifier=%s|%s' % (IDENT_SYSTEM, e['request_id'])}, read=False)
        except PortalError as err:
            if err.kind in ('network', 'server_error', 'outage', 'rate_limited'):
                self.h.settle_effect({'effect': eid, 'outcome': 'uncertain', 'source': 'portal api', 'detail': 'booking request ended without a clear answer (%s)' % err.kind})
                return {'effect': self.reconcile(eid)['effect'], 'booked': None, 'why': 'ambiguous response; read back from the portal'}
            self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': err.kind + ': ' + err.detail})
            raise
        if st in (200, 201) and appt.get('resourceType') == 'Appointment':
            self.check_subject(c, [appt])
            return self.settle_from(eid, appt)
        out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'provider_state': 'HTTP %s' % st,
                                    'detail': (self.diagnostic(json.dumps(appt).encode()) or 'refused')[:300]})
        return {'effect': out, 'booked': False, 'why': out.get('note')}

    @staticmethod
    def appointment_body(pid, slot_id, ident, sched_actor, loc_ref, payload):
        body = {'resourceType': 'Appointment', 'status': 'proposed', 'slot': [{'reference': 'Slot/' + slot_id}],
                'identifier': [{'system': IDENT_SYSTEM, 'value': ident}],
                'participant': [{'actor': {'reference': 'Patient/' + pid}, 'status': 'accepted'}] +
                               ([{'actor': {'reference': sched_actor}, 'status': 'needs-action'}] if sched_actor else []) +
                               ([{'actor': {'reference': loc_ref}, 'status': 'needs-action'}] if loc_ref else [])}
        if payload.get('reason'):
            body['comment'] = clean(str(payload['reason']), 'reason', 300)
        return body

    def effect_row(self, eid):
        with self.h.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (eid,)).fetchone()
        if not e:
            raise PortalError('bad_request', 'unknown effect')
        return dict(e)

    def start(self, e, action):
        if e['action'] != action:
            raise PortalError('bad_request', 'this effect is not %s' % action)
        if e['state'] in ('authorized', 'queued'):
            self.h.dispatch_effect({'effect': e['id']})
        elif e['state'] != 'dispatching':
            raise PortalError('bad_state', 'effect is %s' % e['state'])

    def target_appointment(self, c, pid, payload):
        """The existing appointment a cancel/reschedule is about: payload.appointment_id, or the one an earlier booking effect created."""
        aid = payload.get('appointment_id')
        if not aid and payload.get('of_effect'):
            prev = self.effect_row(clean(payload['of_effect'], 'of_effect', 16))
            aid = (prev.get('external_id') or '').replace('Appointment/', '')
        if not aid or not re.fullmatch(r'[A-Za-z0-9.-]{1,64}', str(aid)):
            raise PortalError('bad_request', 'the payload needs appointment_id (from health_portal_read appointments) or of_effect (the booking effect)')
        st, rh, a = self.request(c, 'GET', 'Appointment/' + aid)
        if st != 200 or a.get('resourceType') != 'Appointment':
            raise PortalError('not_found', 'Appointment/%s could not be read (HTTP %s)' % (aid, st))
        self.check_subject(c, [a])
        return a, rh.get('ETag') or rh.get('Etag')

    def cancel_write(self, c, appt, etag):
        """PUT status=cancelled with If-Match. Returns ('done'|'refused'|'ambiguous', appointment or detail)."""
        body = dict(appt)
        body['status'] = 'cancelled'
        try:
            st, _, out = self.request(c, 'PUT', 'Appointment/' + appt['id'], data=body, headers={'If-Match': etag} if etag else None, read=False)
        except PortalError as err:
            if err.kind in ('network', 'server_error', 'outage', 'rate_limited'):
                return 'ambiguous', err.kind
            raise
        if st == 200 and out.get('status') == 'cancelled':
            return 'done', out
        if st == 412:
            _, _, now = self.request(c, 'GET', 'Appointment/' + appt['id'])
            return ('done', now) if now.get('status') == 'cancelled' else ('refused', 'the appointment changed at the portal since it was read')
        return 'refused', self.diagnostic(json.dumps(out).encode()) or 'HTTP %s' % st

    def cancel(self, eid):
        e = self.effect_row(eid)
        payload = json.loads(e['payload'])
        c = self.conn(e['connection'], need_active=True)
        pid = self.link(c['id'])['patient_id']
        self.start(e, 'appointment.cancel')
        try:
            appt, etag = self.target_appointment(c, pid, payload)
        except PortalError as err:
            self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': 'could not read the appointment to cancel: ' + err.kind})
            raise
        if appt.get('status') == 'cancelled':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'confirmed', 'source': 'portal api', 'external_id': 'Appointment/' + appt['id'], 'provider_state': 'already cancelled'})
            return {'effect': out, 'cancelled': True, 'note': 'it was already cancelled at the portal'}
        if appt.get('status') not in ('booked', 'pending', 'proposed', 'waitlist'):
            out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': 'the appointment is %s and cannot be cancelled' % appt.get('status')})
            return {'effect': out, 'cancelled': False}
        with self.h.db() as db:
            self.h.grant_for(db, e['grant_id'], 'appointment.cancel', e['subject'], e['connection'])
        result, detail = self.cancel_write(c, appt, etag)
        if result == 'done':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'confirmed', 'source': 'portal api', 'external_id': 'Appointment/' + appt['id'], 'provider_state': 'cancelled'})
            return {'effect': out, 'cancelled': True, 'appointment': self.norm_appointments(detail), 'next': 'Remove it from their calendar: health_calendar_plan for this effect.'}
        if result == 'ambiguous':
            self.h.settle_effect({'effect': eid, 'outcome': 'uncertain', 'source': 'portal api', 'detail': 'cancel request ended without a clear answer (%s)' % detail})
            return {'effect': self.reconcile(eid)['effect'], 'cancelled': None}
        out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': str(detail)[:300]})
        return {'effect': out, 'cancelled': False}

    def reschedule(self, eid):
        """Book the new slot first, then cancel the old appointment — the order the owner approved in the grant. If the cancel fails, both
        appointments are named for them; nothing is retried blindly."""
        e = self.effect_row(eid)
        payload = json.loads(e['payload'])
        c = self.conn(e['connection'], need_active=True)
        pid = self.link(c['id'])['patient_id']
        if not payload.get('slot_id'):
            raise PortalError('bad_request', 'the payload needs slot_id of the new time')
        self.start(e, 'appointment.reschedule')
        old, etag = self.target_appointment(c, pid, payload)
        st, _, slot = self.request(c, 'GET', 'Slot/' + urllib.parse.quote(payload['slot_id']))
        if st != 200 or slot.get('status') != 'free':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'detail': 'the new slot is no longer free; the old appointment was kept'})
            return {'effect': out, 'rescheduled': False}
        _, _, sch = self.request(c, 'GET', (slot.get('schedule') or {}).get('reference', ''))
        prac = next((a.get('reference') for a in sch.get('actor', []) if a.get('reference', '').startswith('Practitioner/')), None)
        loc = next((a.get('reference') for a in sch.get('actor', []) if a.get('reference', '').startswith('Location/')), None)
        with self.h.db() as db:
            self.h.grant_for(db, e['grant_id'], 'appointment.reschedule', e['subject'], e['connection'])
        try:
            st, _, new = self.request(c, 'POST', 'Appointment', data=self.appointment_body(pid, payload['slot_id'], e['request_id'], prac, loc, payload),
                                      headers={'If-None-Exist': 'identifier=%s|%s' % (IDENT_SYSTEM, e['request_id'])}, read=False)
        except PortalError as err:
            if err.kind in ('network', 'server_error', 'outage', 'rate_limited'):
                self.h.settle_effect({'effect': eid, 'outcome': 'uncertain', 'source': 'portal api',
                                      'detail': 'the new booking ended without a clear answer; the old appointment %s was NOT cancelled' % old['id']})
                return {'effect': self.reconcile(eid)['effect'], 'rescheduled': None,
                        'next': 'Once the new booking is confirmed, cancel the old one with a separate appointment.cancel effect.'}
            raise
        if st not in (200, 201) or new.get('resourceType') != 'Appointment':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api',
                                        'detail': 'the new time was refused (%s); the old appointment was kept' % (self.diagnostic(json.dumps(new).encode()) or st)})
            return {'effect': out, 'rescheduled': False}
        self.check_subject(c, [new])
        if new.get('status') != 'booked':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'requested', 'source': 'portal api', 'external_id': 'Appointment/' + new['id'], 'provider_state': new.get('status'),
                                        'detail': 'the new time is only requested; the old appointment %s was kept until it is confirmed' % old['id']})
            return {'effect': out, 'rescheduled': False, 'requested': True}
        result, detail = self.cancel_write(c, old, etag)
        if result == 'done':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'confirmed', 'source': 'portal api', 'external_id': 'Appointment/' + new['id'],
                                        'provider_state': 'rescheduled from Appointment/%s' % old['id']})
            return {'effect': out, 'rescheduled': True, 'new': self.norm_appointments(new), 'old_cancelled': old['id']}
        # The new one is booked but the old one could not be cancelled: both exist — the risk the owner approved; the owner decides.
        self.h.settle_effect({'effect': eid, 'outcome': 'confirmed', 'source': 'portal api', 'external_id': 'Appointment/' + new['id'],
                              'provider_state': 'new booked; OLD NOT CANCELLED (Appointment/%s)' % old['id'],
                              'detail': 'cancel of the old appointment %s %s' % (old['id'], 'was ambiguous' if result == 'ambiguous' else 'was refused: ' + str(detail))})
        return {'effect': self.h.public_effect_by_id(eid), 'rescheduled': 'partial', 'double_booked': [new['id'], old['id']],
                'next': 'Tell the owner both appointments exist now (new %s, old %s). Cancel the old one only with a new appointment.cancel effect after checking it is still booked.' % (new['id'], old['id'])}

    def settle_from(self, eid, appt):
        s = appt.get('status')
        summary = self.norm_appointments(appt)
        if s == 'booked':
            out = self.h.settle_effect({'effect': eid, 'outcome': 'confirmed', 'source': 'portal api', 'external_id': 'Appointment/' + appt['id'], 'provider_state': s})
            return {'effect': out, 'booked': True, 'appointment': summary}
        if s in ('pending', 'proposed', 'waitlist'):
            out = self.h.settle_effect({'effect': eid, 'outcome': 'requested' if s != 'waitlist' else 'waitlisted', 'source': 'portal api',
                                        'external_id': 'Appointment/' + appt['id'], 'provider_state': s, 'detail': 'the clinic received a request; it is not confirmed yet'})
            return {'effect': out, 'booked': False, 'requested': True, 'appointment': summary,
                    'next': 'Tell the owner it is REQUESTED, not booked. Check again later with health_portal_reconcile.'}
        out = self.h.settle_effect({'effect': eid, 'outcome': 'failed', 'source': 'portal api', 'provider_state': str(s), 'detail': 'the portal returned status ' + str(s)})
        return {'effect': out, 'booked': False}

    def reconcile(self, eid):
        """Read the provider's state for an uncertain booking (by our identifier). Only a successful, complete read decides."""
        e = self.effect_row(eid)
        c = self.conn(e['connection'])
        pid = self.link(c['id'])['patient_id']
        if e['action'] == 'appointment.cancel':
            try:
                appt, _ = self.target_appointment(c, pid, json.loads(e['payload']))
            except PortalError as err:
                return {'effect': self.h.public_effect_by_id(eid), 'reconciled': False, 'why': 'could not read the portal (%s); still uncertain' % err.kind}
            found = 'confirmed' if appt.get('status') == 'cancelled' else 'absent'
            out = self.h.reconcile_effect({'effect': eid, 'found': found, 'source': 'portal read', 'external_id': 'Appointment/' + appt['id'],
                                           'detail': 'status ' + str(appt.get('status'))})
            return {'effect': out, 'reconciled': True, 'appointment': self.norm_appointments(appt)}
        try:
            found, _, complete = self.search(c, 'Appointment', {'patient': pid, 'identifier': '%s|%s' % (IDENT_SYSTEM, e['request_id'])})
            self.check_subject(c, found)
        except PortalError as err:
            return {'effect': self.h.public_effect_by_id(eid), 'reconciled': False, 'why': 'could not read the portal (%s); still uncertain — do not book again' % err.kind}
        if found:
            a = found[0]
            if a.get('status') == 'booked':
                out = self.h.reconcile_effect({'effect': eid, 'found': 'confirmed', 'source': 'portal read by identifier', 'external_id': 'Appointment/' + a['id']})
            elif a.get('status') in ('pending', 'proposed', 'waitlist'):
                out = self.h.reconcile_effect({'effect': eid, 'found': 'pending', 'source': 'portal read by identifier', 'detail': 'Appointment/%s is %s' % (a['id'], a.get('status'))})
            else:
                out = self.h.reconcile_effect({'effect': eid, 'found': 'absent', 'source': 'portal read by identifier', 'detail': 'Appointment/%s is %s' % (a['id'], a.get('status'))})
            return {'effect': out, 'reconciled': out['state'] != 'uncertain', 'appointment': self.norm_appointments(a)}
        if not complete:
            return {'effect': self.h.public_effect_by_id(eid), 'reconciled': False, 'why': 'the portal\'s answer was incomplete; still uncertain'}
        out = self.h.reconcile_effect({'effect': eid, 'found': 'absent', 'source': 'portal read by identifier', 'detail': 'no appointment carries this request id'})
        return {'effect': out, 'reconciled': True}

    # ---------- broker actions ----------
    def status(self, cid):
        c = self.conn(cid)
        try:
            ln = self.link(cid)
        except PortalError:
            ln = None
        return {'connection': cid, 'institution': c['institution'], 'environment': c['environment'], 'status': c['status'], 'capabilities': json.loads(c['capabilities'] or '{}'),
                'linked': bool(ln), 'patient_display': ln['patient_name'] if ln else None, 'last_ok': ln['last_ok'] if ln else None, 'last_error': ln['last_error'] if ln else None}

    def call(self, action, a, privileged=False):
        try:
            if action == 'health_portal_authorize':
                if not privileged:
                    raise PortalError('owner_only', 'only the owner starts a portal sign-in (/healthauth)')
                return self.begin(clean(a.get('connection'), 'connection', 16))
            if action == 'health_portal_callback':
                if not privileged:
                    raise PortalError('owner_only', 'callbacks come from the sign-in page only')
                return self.complete(a.get('state'), a.get('code'), a.get('error'))
            if action == 'health_portal_status':
                return self.status(clean(a.get('connection'), 'connection', 16))
            if action == 'health_portal_read':
                cid = clean(a.get('connection'), 'connection', 16)
                what = clean(a.get('what'), 'what', 20)
                c = self.conn(cid, need_active=True)
                need = 'appointment.search' if what == 'slots' else 'portal.read'
                with self.h.db() as db:
                    self.h.grant_for(db, clean(a.get('grant'), 'grant', 16), need, c['subject'], cid)
                return self.read(c, what, {k: a.get(k) for k in ('from', 'to', 'practitioner', 'service_type', 'status', 'code') if a.get(k)})
            if action == 'health_portal_book':
                return self.book(clean(a.get('effect'), 'effect', 16))
            if action == 'health_portal_reconcile':
                return self.reconcile(clean(a.get('effect'), 'effect', 16))
            if action == 'health_portal_cancel':
                return self.cancel(clean(a.get('effect'), 'effect', 16))
            if action == 'health_portal_reschedule':
                return self.reschedule(clean(a.get('effect'), 'effect', 16))
        except PortalError as e:
            return {'error': e.kind, 'detail': e.detail, 'next': NEXT.get(e.kind, 'Tell the owner in one line what happened; do not retry a booking.')}
        raise ValueError('Unknown portal action.')


NEXT = {
    'reauth_required': 'The portal needs the owner to sign in again: tell them to send /healthauth CONNECTION. Do nothing else on this connection.',
    'refresh_uncertain': 'Try the same read once more later; if it says reauth_required, ask the owner for /healthauth.',
    'insufficient_scope': 'The portal did not grant this. Tell the owner which permission is missing; do not work around it.',
    'rate_limited': 'The portal is limiting requests. Wait and try the read later; never retry a booking (it is reconciled instead).',
    'outage': 'The portal is down. Tell the owner and try later.',
    'identity_mismatch': 'STOP. The portal returned another patient\'s data; nothing was used. Tell the owner; do not continue on this connection.',
    'foreign_url': 'The portal pointed outside its own address; the read stopped there. Use the partial result only if you say it is partial.',
    'not_linked': 'The owner has not authorized this portal yet: ask them to send /healthauth CONNECTION.',
}
