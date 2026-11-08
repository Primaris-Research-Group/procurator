#!/usr/bin/env python3
"""Mock EHR with a patient-portal API (web 0.22.0, consumer health module; build-plans/health/03-patient-portal-apis.md).

A small, deterministic stand-in for an Epic/Oracle-style patient API so the health module can be tested end to end without
production access. Standard library only. ALL DATA IS SYNTHETIC; no real patient, clinician or institution is represented.

What it speaks
  * SMART App Launch (standalone patient launch): /<inst>/fhir/.well-known/smart-configuration, /<inst>/oauth2/authorize
    (consent page; public client; exact redirect match; PKCE S256 required; state echoed), /<inst>/oauth2/token
    (authorization_code with single-use codes — a replayed code is refused and revokes what it issued; refresh_token with
    rotation), /<inst>/oauth2/revoke.
  * FHIR R4 (JSON) subset under /<inst>/fhir: metadata, Patient, Practitioner, Location, Schedule, Slot (_include=Slot:schedule),
    Appointment (search / read / create with conditional create `If-None-Exist` / update to cancelled with If-Match),
    MedicationRequest, AllergyIntolerance, Observation. Bundles page with absolute `next` links. SMART v1/v2 scopes enforced.
  * Two institutions whose patient IDs overlap on purpose (both have Patient/p1, different people):
      riverside  — books immediately (Appointment.status = booked)
      northgate  — takes REQUESTS (Appointment.status = pending) that staff confirm later (admin `confirm`)
  * Fault injection (admin API, loopback + admin token): rate_limit N, error_500 N, outage, hostile_next, partial, wrong_patient,
    book_commit_then_504, book_commit_then_drop, slot_race, refresh_lost, no_patient_context, issuer_mismatch, foreign_endpoints.

Run it
  python3 mock_ehr.py serve [--port 3351] [--public http://127.0.0.1:3351] [--today 2026-11-09] [--redirect URL ...] [--admin-token-file PATH]
  On the droplet it runs as procurator-mock-ehr.service; the dashboard forwards /assistant/dashboard/mock-ehr/<inst>/(fhir|oauth2)/… to it.
  Faults and staff confirmation from the Mac: bash bin/mock-ehr.sh fault riverside '{"outage":true}' | clear riverside | confirm northgate ap-1001
  Test accounts:  riverside  owner / riverside-test (Patient/p1), maria / riverside-test (Patient/p2)
                  northgate  jordan  / northgate-test (Patient/p1)
                  corner     owner / corner-test (pharmacy patient c1), maria / corner-test (c2) — synthetic pharmacy, see mock_rx.py
"""
import base64
import datetime as dt
import hashlib
import html
import http.server
import json
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse

import mock_rx

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('America/New_York')
except Exception:  # pragma: no cover
    TZ = dt.timezone(dt.timedelta(hours=-5))

CLIENT_ID = 'procurator-health'
DEFAULT_REDIRECTS = ('https://assistant.example.com/assistant/dashboard/health/callback',)
SYNTHETIC = {'system': 'urn:procurator:mock-ehr', 'code': 'synthetic', 'display': 'Synthetic test data — not a real person'}
FAULTS = ('rate_limit', 'error_500', 'outage', 'hostile_next', 'partial', 'wrong_patient', 'book_commit_then_504', 'book_commit_then_drop',
          'slot_race', 'refresh_lost', 'no_patient_context', 'issuer_mismatch', 'foreign_endpoints', 'cancel_commit_then_504',
          'refill_commit_then_504')


class Drop(Exception):
    """Close the connection without an answer (a lost response)."""


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def institutions():
    def pat(pid, given, family, birth, gender):
        return {'resourceType': 'Patient', 'id': pid, 'meta': {'tag': [SYNTHETIC]}, 'name': [{'given': [given], 'family': family, 'text': given + ' ' + family}],
                'birthDate': birth, 'gender': gender}

    def prac(pid, given, family, specialty):
        return {'resourceType': 'Practitioner', 'id': pid, 'meta': {'tag': [SYNTHETIC]}, 'name': [{'prefix': ['Dr.'], 'given': [given], 'family': family, 'text': 'Dr. %s %s' % (given, family)}],
                'qualification': [{'code': {'text': specialty}}]}

    def loc(lid, name, city):
        return {'resourceType': 'Location', 'id': lid, 'meta': {'tag': [SYNTHETIC]}, 'name': name, 'address': {'city': city, 'state': 'GA'}}

    riverside = {
        'name': 'Riverside Health (synthetic)', 'policy': 'booked',
        'users': {'owner': ('riverside-test', 'p1'), 'maria': ('riverside-test', 'p2')},
        'patients': {'p1': pat('p1', 'Alex', 'Testpatient', '1988-04-12', 'male'), 'p2': pat('p2', 'Maria', 'Synthetic', '1975-09-30', 'female')},
        'practitioners': {'pr-rivera': prac('pr-rivera', 'Elena', 'Rivera', 'Dermatology'), 'pr-chen': prac('pr-chen', 'Wei', 'Chen', 'Internal Medicine')},
        'locations': {'loc-midtown': loc('loc-midtown', 'Riverside Midtown Clinic', 'Atlanta'), 'loc-buckhead': loc('loc-buckhead', 'Riverside Buckhead Clinic', 'Atlanta')},
        # (practitioner, location, weekday, start times, service type)
        'rules': [('pr-rivera', 'loc-midtown', 1, ['13:00', '13:30', '14:00', '14:30', '15:00', '15:30', '16:00', '16:30'], 'follow-up'),
                  ('pr-rivera', 'loc-buckhead', 3, ['13:00', '13:30'], 'new-patient'),
                  ('pr-rivera', 'loc-buckhead', 3, ['14:00', '14:30', '15:00'], 'follow-up'),
                  ('pr-chen', 'loc-midtown', 0, ['09:00', '09:30', '10:00', '10:30', '11:00', '11:30'], 'follow-up'),
                  ('pr-chen', 'loc-midtown', 2, ['09:00'], 'new-patient')],
        'established': {('p1', 'pr-rivera'), ('p1', 'pr-chen'), ('p2', 'pr-chen')},
        'meds': {'p1': [('mr-1', 'Atorvastatin 20 mg oral tablet', 'Take 1 tablet by mouth once daily', 'active', 3, 30),
                        ('mr-2', 'Atorvastatin 40 mg oral tablet', 'Take 1 tablet by mouth once daily', 'stopped', 0, 30),
                        ('mr-3', 'Cetirizine 10 mg oral tablet', 'Take 1 tablet by mouth daily as needed for allergies', 'active', 1, 30)],
                 'p2': [('mr-21', 'Lisinopril 10 mg oral tablet', 'Take 1 tablet by mouth once daily', 'active', 5, 90)]},
        'allergies': {'p1': [('al-1', 'Penicillin', 'high', 'Hives')], 'p2': []},
        'observations': {'p1': [('ob-1', '29463-7', 'Body weight', 82.5, 'kg', '2026-10-01'), ('ob-2', '29463-7', 'Body weight', 181.0, '[lb_av]', '2026-10-28'),
                                ('ob-3', '8302-2', 'Body height', 178.0, 'cm', '2026-10-01')],
                         'p2': [('ob-21', '29463-7', 'Body weight', 64.0, 'kg', '2026-09-15')]},
        'appointments': [('ap-100', 'p1', 'pr-rivera', 'loc-midtown', '2026-10-14T14:00:00', 'fulfilled', 'follow-up'),
                         ('ap-101', 'p2', 'pr-chen', 'loc-midtown', '2026-11-16T09:30:00', 'booked', 'follow-up')],
    }
    northgate = {
        'name': 'Northgate Family Medicine (synthetic)', 'policy': 'pending',
        'users': {'jordan': ('northgate-test', 'p1')},
        'patients': {'p1': pat('p1', 'Jordan', 'Example', '1990-01-20', 'other')},
        'practitioners': {'pr-okafor': prac('pr-okafor', 'Ada', 'Okafor', 'Family Medicine')},
        'locations': {'loc-north': loc('loc-north', 'Northgate Clinic', 'Marietta')},
        'rules': [('pr-okafor', 'loc-north', 0, ['10:00', '10:30', '11:00'], 'follow-up'), ('pr-okafor', 'loc-north', 4, ['10:00', '10:30'], 'follow-up')],
        'established': {('p1', 'pr-okafor')},
        'meds': {'p1': [('mr-n1', 'Metformin 500 mg oral tablet', 'Take 1 tablet by mouth twice daily', 'active', 2, 60)]},
        'allergies': {'p1': []},
        'observations': {'p1': [('ob-n1', '29463-7', 'Body weight', 70.2, 'kg', '2026-10-20')]},
        'appointments': [],
    }
    return {'riverside': riverside, 'northgate': northgate}


class EHR:
    def __init__(self, today='2026-11-09', public='http://127.0.0.1:3351', admin_token=None, redirects=DEFAULT_REDIRECTS, days=42, token_seconds=3600):
        self.lock = threading.RLock()
        self.today = dt.date.fromisoformat(today)
        self.public = public.rstrip('/')
        self.admin_token = admin_token or secrets.token_urlsafe(18)
        self.redirects = set(redirects)
        self.token_seconds = token_seconds
        self.inst = institutions()
        self.inst['corner'] = mock_rx.institution(self.today)          # 0.23.0: synthetic pharmacy (mock_rx.py)
        self.rx = mock_rx.Rx(self)
        self.codes, self.tokens, self.refresh = {}, {}, {}
        self.faults = {k: {} for k in self.inst}
        self.counter = 1000
        self.log = []
        for name, d in self.inst.items():
            d['slots'], d['schedules'], d['appts'] = {}, {}, {}
            self._make_slots(name, d, days)
            for aid, pid, pr, lo, start, status, svc in d['appointments']:
                s = dt.datetime.fromisoformat(start).replace(tzinfo=TZ)
                d['appts'][aid] = self._appt(name, aid, pid, pr, lo, s, s + dt.timedelta(minutes=30), status, svc, None)

    # ---------- data ----------
    def base(self, inst):
        if self.inst[inst].get('kind') == 'pharmacy':
            return self.rx.base(inst)
        return '%s/%s/fhir' % (self.public, inst)

    def _make_slots(self, name, d, days):
        for pr, lo, wd, times, svc in d['rules']:
            sid = 'sch-%s-%s' % (pr[3:], lo[4:])
            d['schedules'][sid] = {'resourceType': 'Schedule', 'id': sid, 'actor': [{'reference': 'Practitioner/' + pr, 'display': d['practitioners'][pr]['name'][0]['text']},
                                                                                  {'reference': 'Location/' + lo, 'display': d['locations'][lo]['name']}], 'meta': {'tag': [SYNTHETIC]}}
            for off in range(1, days + 1):
                day = self.today + dt.timedelta(days=off)
                if day.weekday() != wd:
                    continue
                for t in times:
                    start = dt.datetime.combine(day, dt.time.fromisoformat(t)).replace(tzinfo=TZ)
                    slid = 'sl-%s-%s-%s' % (pr[3:], day.strftime('%Y%m%d'), t.replace(':', ''))
                    busy = int(hashlib.sha1(slid.encode()).hexdigest(), 16) % 5 == 0
                    d['slots'][slid] = {'resourceType': 'Slot', 'id': slid, 'meta': {'versionId': '1', 'tag': [SYNTHETIC]}, 'schedule': {'reference': 'Schedule/' + sid},
                                        'serviceType': [{'text': svc}], 'status': 'busy' if busy else 'free',
                                        'start': start.isoformat(), 'end': (start + dt.timedelta(minutes=30)).isoformat()}

    def _appt(self, inst, aid, pid, pr, lo, start, end, status, svc, ident):
        d = self.inst[inst]
        a = {'resourceType': 'Appointment', 'id': aid, 'meta': {'versionId': '1', 'lastUpdated': dt.datetime.now(dt.timezone.utc).isoformat(), 'tag': [SYNTHETIC]},
             'status': status, 'serviceType': [{'text': svc}], 'start': start.isoformat(), 'end': end.isoformat(),
             'participant': [{'actor': {'reference': 'Patient/' + pid, 'display': d['patients'][pid]['name'][0]['text']}, 'status': 'accepted'},
                             {'actor': {'reference': 'Practitioner/' + pr, 'display': d['practitioners'][pr]['name'][0]['text']}, 'status': 'accepted'},
                             {'actor': {'reference': 'Location/' + lo, 'display': d['locations'][lo]['name']}, 'status': 'accepted'}]}
        if ident:
            a['identifier'] = [ident]
        return a

    def _meds(self, inst, pid):
        return [{'resourceType': 'MedicationRequest', 'id': mid, 'meta': {'versionId': '1', 'tag': [SYNTHETIC]}, 'status': st, 'intent': 'order',
                 'subject': {'reference': 'Patient/' + pid}, 'medicationCodeableConcept': {'text': text}, 'authoredOn': '2026-06-01',
                 'dosageInstruction': [{'text': sig}], 'dispenseRequest': {'numberOfRepeatsAllowed': refills, 'quantity': {'value': qty, 'unit': 'tablet'}}}
                for mid, text, sig, st, refills, qty in self.inst[inst]['meds'].get(pid, [])]

    def _allergies(self, inst, pid):
        return [{'resourceType': 'AllergyIntolerance', 'id': aid, 'meta': {'versionId': '1', 'tag': [SYNTHETIC]}, 'patient': {'reference': 'Patient/' + pid},
                 'code': {'text': sub}, 'criticality': crit, 'reaction': [{'manifestation': [{'text': rx}]}]} for aid, sub, crit, rx in self.inst[inst]['allergies'].get(pid, [])]

    def _obs(self, inst, pid):
        return [{'resourceType': 'Observation', 'id': oid, 'meta': {'versionId': '1', 'tag': [SYNTHETIC]}, 'status': 'final', 'subject': {'reference': 'Patient/' + pid},
                 'code': {'coding': [{'system': 'http://loinc.org', 'code': loinc}], 'text': text},
                 'valueQuantity': {'value': val, 'unit': unit, 'system': 'http://unitsofmeasure.org', 'code': unit}, 'effectiveDateTime': when}
                for oid, loinc, text, val, unit, when in self.inst[inst]['observations'].get(pid, [])]

    # ---------- faults ----------
    def fault(self, inst, name):
        """Consume a fault if set. Counted faults decrement; flags stay until cleared."""
        f = self.faults[inst]
        v = f.get(name)
        if not v:
            return False
        if isinstance(v, int) and not isinstance(v, bool):
            f[name] = v - 1
        return True

    # ---------- HTTP entry ----------
    def handle(self, method, path, query, headers, body):
        """Returns (status, headers, body). Raises Drop to close without answering."""
        p = urllib.parse.urlsplit(path).path
        q = urllib.parse.parse_qs(query, keep_blank_values=True)
        with self.lock:
            self.log.append((method, p))
        if p.startswith('/_admin/'):
            return self.admin(method, p, headers, body)
        m = re.fullmatch(r'/([a-z]+)/(fhir|oauth2|rx)(/.*)?', p)
        if not m or m.group(1) not in self.inst:
            return self.json(404, {'error': 'not found'})
        inst, area, rest = m.group(1), m.group(2), m.group(3) or '/'
        if area == 'oauth2':
            return self.oauth(inst, method, rest, q, headers, body)
        pharmacy = self.inst[inst].get('kind') == 'pharmacy'
        if (area == 'rx') != pharmacy:
            return self.json(404, {'error': 'not found'})
        if area == 'rx':
            return self.rx.handle(inst, method, rest, q, headers, body)
        if rest == '/.well-known/smart-configuration':
            return self.smart_config(inst)
        if rest == '/metadata':
            return self.json(200, {'resourceType': 'CapabilityStatement', 'status': 'active', 'kind': 'instance', 'fhirVersion': '4.0.1', 'format': ['json'],
                                   'software': {'name': 'PROCURATOR mock EHR (synthetic)'}, 'rest': [{'mode': 'server', 'security': {'service': [{'text': 'SMART-on-FHIR'}]}}]})
        return self.fhir(inst, method, rest, q, headers, body)

    @staticmethod
    def json(status, obj, extra=None):
        h = {'Content-Type': 'application/fhir+json' if isinstance(obj, dict) and 'resourceType' in obj else 'application/json'}
        h.update(extra or {})
        return status, h, json.dumps(obj).encode()

    def outcome(self, status, text, code='processing'):
        return self.json(status, {'resourceType': 'OperationOutcome', 'issue': [{'severity': 'error', 'code': code, 'diagnostics': text}]})

    # ---------- admin ----------
    def admin(self, method, p, headers, body):
        if headers.get('X-Admin-Token') != self.admin_token:
            return self.json(403, {'error': 'admin token required'})
        m = re.fullmatch(r'/_admin/([a-z]+)/(fault|clear|revoke_consent|confirm|appointments|slot|advance|prescription|refills)', p)
        if not m or m.group(1) not in self.inst:
            return self.json(404, {'error': 'unknown admin path'})
        inst, op = m.groups()
        data = json.loads(body or b'{}')
        with self.lock:
            if op == 'fault':
                for k, v in data.items():
                    if k not in FAULTS:
                        return self.json(400, {'error': 'unknown fault ' + k})
                    self.faults[inst][k] = v
                return self.json(200, {'faults': self.faults[inst]})
            if op == 'clear':
                self.faults[inst] = {}
                return self.json(200, {'faults': {}})
            if op == 'revoke_consent':
                pid = data.get('patient')
                for store in (self.tokens, self.refresh):
                    for t in [t for t, v in store.items() if v['inst'] == inst and v['patient'] == pid]:
                        del store[t]
                return self.json(200, {'revoked': pid})
            if op == 'advance':
                return self.rx.advance(inst, data)
            if op == 'prescription':
                return self.rx.set_rx(inst, data)
            if op == 'refills':
                return self.json(200, {'refills': list(self.inst[inst].get('refills', {}).values())})
            if op == 'confirm':
                a = self.inst[inst]['appts'].get(data.get('appointment'))
                if not a:
                    return self.json(404, {'error': 'no such appointment'})
                a['status'] = 'booked'
                a['meta']['versionId'] = str(int(a['meta']['versionId']) + 1)
                return self.json(200, a)
            if op == 'slot':
                s = self.inst[inst]['slots'].get(data.get('slot'))
                if not s:
                    return self.json(404, {'error': 'no such slot'})
                s['status'] = data.get('status', s['status'])
                return self.json(200, s)
            return self.json(200, {'appointments': list(self.inst[inst]['appts'].values())})

    # ---------- SMART ----------
    def smart_config(self, inst):
        base = self.base(inst)
        auth_host = 'https://login.evil.example' if self.faults[inst].get('foreign_endpoints') else self.public
        return self.json(200, {'issuer': (base + '-other') if self.faults[inst].get('issuer_mismatch') else base,
                               'authorization_endpoint': '%s/%s/oauth2/authorize' % (auth_host, inst), 'token_endpoint': '%s/%s/oauth2/token' % (auth_host, inst),
                               'revocation_endpoint': '%s/%s/oauth2/revoke' % (self.public, inst),
                               'grant_types_supported': ['authorization_code', 'refresh_token'], 'code_challenge_methods_supported': ['S256'],
                               'token_endpoint_auth_methods_supported': ['none'], 'response_types_supported': ['code'],
                               'scopes_supported': ['launch/patient', 'openid', 'fhirUser', 'offline_access', 'patient/*.rs', 'patient/Appointment.c', 'patient/Appointment.u'],
                               'capabilities': ['launch-standalone', 'client-public', 'context-standalone-patient', 'permission-patient', 'permission-offline', 'permission-v1', 'permission-v2']})

    def oauth(self, inst, method, rest, q, headers, body):
        form = urllib.parse.parse_qs((body or b'').decode(), keep_blank_values=True) if method == 'POST' else {}
        one = lambda src, k: (src.get(k) or [''])[0]
        if rest == '/authorize' and method == 'GET':
            client, redirect = one(q, 'client_id'), one(q, 'redirect_uri')
            if client != CLIENT_ID or redirect not in self.redirects:
                return 400, {'Content-Type': 'text/html'}, b'<h1>Unknown client or unregistered redirect</h1>'   # never redirect to an unknown URI
            if one(q, 'response_type') != 'code' or one(q, 'code_challenge_method') != 'S256' or not one(q, 'code_challenge') or one(q, 'aud') != self.base(inst):
                return self.redirect_error(redirect, one(q, 'state'), 'invalid_request')
            hidden = ''.join('<input type="hidden" name="%s" value="%s">' % (k, html.escape(one(q, k))) for k in ('client_id', 'redirect_uri', 'scope', 'state', 'code_challenge', 'aud'))
            page = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>%s — sign in</title>'
                    '<h2>%s</h2><p><b>Synthetic test portal.</b> No real patient data.</p><p>PROCURATOR asks to: %s</p>'
                    '<form method="post" action="%s/%s/oauth2/authorize/approve">%s<label>Username <input name="username" autocomplete="username"></label><br>'
                    '<label>Password <input name="password" type="password" autocomplete="current-password"></label><br><button name="decision" value="allow">Allow</button> '
                    '<button name="decision" value="deny">Deny</button></form>') % (self.inst[inst]['name'], self.inst[inst]['name'], html.escape(one(q, 'scope')),
                                                                     urllib.parse.urlsplit(self.public).path, inst, hidden)
            return 200, {'Content-Type': 'text/html; charset=utf-8'}, page.encode()
        if rest == '/authorize/approve' and method == 'POST':
            redirect = one(form, 'redirect_uri')
            if one(form, 'client_id') != CLIENT_ID or redirect not in self.redirects:
                return 400, {'Content-Type': 'text/html'}, b'<h1>Unknown client</h1>'
            if one(form, 'decision') != 'allow':
                return self.redirect_error(redirect, one(form, 'state'), 'access_denied')
            user = self.inst[inst]['users'].get(one(form, 'username'))
            if not user or user[0] != one(form, 'password'):
                return 401, {'Content-Type': 'text/html'}, b'<h1>Wrong username or password</h1>'
            code = secrets.token_urlsafe(24)
            with self.lock:
                self.codes[code] = {'inst': inst, 'patient': user[1], 'scope': one(form, 'scope'), 'redirect': redirect, 'challenge': one(form, 'code_challenge'),
                                    'expires': time.time() + 300, 'used': False, 'issued': []}
            return 302, {'Location': redirect + ('&' if '?' in redirect else '?') + urllib.parse.urlencode({'code': code, 'state': one(form, 'state')})}, b''
        if rest == '/token' and method == 'POST':
            grant = one(form, 'grant_type')
            with self.lock:
                if grant == 'authorization_code':
                    c = self.codes.get(one(form, 'code'))
                    if not c or c['inst'] != inst or c['expires'] < time.time():
                        return self.json(400, {'error': 'invalid_grant'})
                    if c['used']:
                        for t in c['issued']:      # RFC 6749 §4.1.2: a replayed code revokes what it issued
                            self.tokens.pop(t, None)
                            self.refresh.pop(t, None)
                        return self.json(400, {'error': 'invalid_grant', 'error_description': 'code already used'})
                    if one(form, 'client_id') != CLIENT_ID or one(form, 'redirect_uri') != c['redirect']:
                        return self.json(400, {'error': 'invalid_grant', 'error_description': 'redirect_uri mismatch'})
                    if b64url(hashlib.sha256(one(form, 'code_verifier').encode()).digest()) != c['challenge']:
                        return self.json(400, {'error': 'invalid_grant', 'error_description': 'PKCE verification failed'})
                    c['used'] = True
                    return self.issue(inst, c['patient'], c['scope'], c)
                if grant == 'refresh_token':
                    r = self.refresh.pop(one(form, 'refresh_token'), None)
                    if not r or r['inst'] != inst:
                        return self.json(400, {'error': 'invalid_grant'})
                    out = self.issue(inst, r['patient'], r['scope'], None)
                    if self.fault(inst, 'refresh_lost'):
                        raise Drop()      # rotated on the server, but the client never hears it
                    return out
            return self.json(400, {'error': 'unsupported_grant_type'})
        if rest == '/revoke' and method == 'POST':
            t = one(form, 'token')
            with self.lock:
                for store in (self.tokens, self.refresh):
                    v = store.pop(t, None)
                    if v:
                        for other in [k for k, x in self.tokens.items() if x.get('family') == v.get('family')]:
                            self.tokens.pop(other, None)
                        for other in [k for k, x in self.refresh.items() if x.get('family') == v.get('family')]:
                            self.refresh.pop(other, None)
            return 200, {'Content-Type': 'application/json'}, b'{}'
        return self.json(404, {'error': 'not found'})

    def redirect_error(self, redirect, state, error):
        return 302, {'Location': redirect + ('&' if '?' in redirect else '?') + urllib.parse.urlencode({'error': error, 'state': state})}, b''

    def issue(self, inst, patient, scope, code):
        family = code['issued'][0] if code and code['issued'] else secrets.token_hex(6)
        at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.tokens[at] = {'inst': inst, 'patient': patient, 'scope': scope, 'expires': time.time() + self.token_seconds, 'family': family}
        body = {'access_token': at, 'token_type': 'Bearer', 'expires_in': self.token_seconds, 'scope': scope}
        if 'offline_access' in scope.split():
            self.refresh[rt] = {'inst': inst, 'patient': patient, 'scope': scope, 'family': family}
            body['refresh_token'] = rt
        if code is not None:
            code['issued'] += [at, rt]
        if not self.fault(inst, 'no_patient_context'):
            body['patient'] = patient
        return self.json(200, body)

    # ---------- FHIR ----------
    @staticmethod
    def allowed(scope, rtype, op):
        """SMART v1 (.read/.write) and v2 (.cruds) patient/ and user/ scopes."""
        for s in scope.split():
            m = re.fullmatch(r'(?:patient|user)/(\*|[A-Za-z]+)\.(read|write|[cruds]+)', s)
            if not m or m.group(1) not in ('*', rtype):
                continue
            perm = m.group(2)
            if perm == 'read' and op in 'rs' or perm == 'write' and op in 'cud' or perm not in ('read', 'write') and op in perm:
                return True
        return False

    def fhir(self, inst, method, rest, q, headers, body):
        auth = headers.get('Authorization') or ''
        tok = self.tokens.get(auth[7:]) if auth.startswith('Bearer ') else None
        if not tok or tok['inst'] != inst or tok['expires'] < time.time():
            return 401, {'WWW-Authenticate': 'Bearer error="invalid_token"', 'Content-Type': 'application/json'}, b'{"error":"invalid_token"}'
        if self.faults[inst].get('outage'):
            return self.outcome(503, 'Scheduled maintenance (synthetic outage)', 'transient')
        if self.fault(inst, 'rate_limit'):
            return 429, {'Retry-After': '1', 'Content-Type': 'application/json'}, b'{"error":"rate limited"}'
        if self.fault(inst, 'error_500'):
            return self.outcome(500, 'Internal error (synthetic)', 'exception')
        pid, scope, d = tok['patient'], tok['scope'], self.inst[inst]
        parts = [x for x in rest.strip('/').split('/') if x]
        rtype = parts[0] if parts else ''
        one = lambda k: (q.get(k) or [''])[0]
        need = {'GET': 's' if len(parts) == 1 else 'r', 'POST': 'c', 'PUT': 'u'}.get(method, 'x')
        if not self.allowed(scope, rtype, need):
            return self.outcome(403, 'insufficient_scope for %s.%s' % (rtype, need), 'forbidden')
        with self.lock:
            if method == 'GET' and len(parts) == 2:
                res = {'Patient': d['patients'], 'Practitioner': d['practitioners'], 'Location': d['locations'], 'Schedule': d['schedules'],
                       'Slot': d['slots'], 'Appointment': d['appts']}.get(rtype, {}).get(parts[1])
                if rtype == 'MedicationRequest':
                    res = next((m for m in self._meds(inst, pid) if m['id'] == parts[1]), None)
                if not res:
                    return self.outcome(404, 'not found', 'not-found')
                if rtype == 'Patient' and parts[1] != pid or rtype == 'Appointment' and not any(p['actor']['reference'] == 'Patient/' + pid for p in res['participant']):
                    return self.outcome(403, 'not in your patient compartment', 'forbidden')
                return self.json(200, res, {'ETag': 'W/"%s"' % res.get('meta', {}).get('versionId', '1')})
            if method == 'GET' and len(parts) == 1:
                return self.search(inst, rtype, q, pid)
            if method == 'POST' and parts == ['Appointment']:
                return self.book(inst, pid, headers, body)
            if method == 'PUT' and len(parts) == 2 and rtype == 'Appointment':
                return self.update_appt(inst, pid, parts[1], headers, body)
        return self.outcome(405, 'operation not supported', 'not-supported')

    def search(self, inst, rtype, q, pid):
        d = self.inst[inst]
        one = lambda k: (q.get(k) or [''])[0]
        include = []
        if rtype == 'Patient':
            items = [d['patients'][pid]]
        elif rtype == 'Practitioner':
            items = list(d['practitioners'].values())
        elif rtype == 'Location':
            items = list(d['locations'].values())
        elif rtype == 'Slot':
            items = sorted(d['slots'].values(), key=lambda s: s['start'])
            actor = one('schedule.actor')
            if actor:
                items = [s for s in items if any(a['reference'] == actor for a in d['schedules'][s['schedule']['reference'][9:]]['actor'])]
            if one('status'):
                items = [s for s in items if s['status'] == one('status')]
            if one('service-type'):
                items = [s for s in items if s['serviceType'][0]['text'] == one('service-type')]
            for v in q.get('start', []):
                if v.startswith('ge'):
                    items = [s for s in items if s['start'][:10] >= v[2:12]]
                elif v.startswith('lt'):
                    items = [s for s in items if s['start'][:10] < v[2:12]]
            if one('_include') == 'Slot:schedule':
                include = list({s['schedule']['reference'][9:]: d['schedules'][s['schedule']['reference'][9:]] for s in items}.values())
        elif rtype == 'Appointment':
            want = one('patient').replace('Patient/', '')
            if want and want != pid:
                return self.outcome(403, 'not in your patient compartment', 'forbidden')
            items = sorted((a for a in d['appts'].values() if any(p['actor']['reference'] == 'Patient/' + pid for p in a['participant'])), key=lambda a: a['start'])
            if self.fault(inst, 'wrong_patient'):
                other = next((x for x in d['patients'] if x != pid), None) or 'p999'
                items = items + [self._appt(inst, 'ap-leak', other if other in d['patients'] else pid, next(iter(d['practitioners'])), next(iter(d['locations'])),
                                            dt.datetime(2026, 12, 1, 9, tzinfo=TZ), dt.datetime(2026, 12, 1, 9, 30, tzinfo=TZ), 'booked', 'follow-up', None)]
                if other not in d['patients']:
                    items[-1]['participant'][0]['actor']['reference'] = 'Patient/p999'
            if one('identifier'):
                sysv = one('identifier')
                items = [a for a in items if any('%s|%s' % (i.get('system'), i.get('value')) == sysv for i in a.get('identifier', []))]
            if one('status'):
                items = [a for a in items if a['status'] in one('status').split(',')]
            for v in q.get('date', []):
                if v.startswith('ge'):
                    items = [a for a in items if a['start'][:10] >= v[2:12]]
                elif v.startswith('lt'):
                    items = [a for a in items if a['start'][:10] < v[2:12]]
        elif rtype in ('MedicationRequest', 'AllergyIntolerance', 'Observation'):
            want = one('patient').replace('Patient/', '') or one('subject').replace('Patient/', '')
            if want and want != pid:
                return self.outcome(403, 'not in your patient compartment', 'forbidden')
            items = {'MedicationRequest': self._meds, 'AllergyIntolerance': self._allergies, 'Observation': self._obs}[rtype](inst, pid)
            if rtype == 'Observation' and one('code'):
                items = [o for o in items if o['code']['coding'][0]['code'] == one('code').split('|')[-1]]
            if self.fault(inst, 'wrong_patient') and items:
                items = [json.loads(json.dumps(items[0]))] + items
                items[0]['id'] = items[0]['id'] + '-leak'
                items[0]['subject' if 'subject' in items[0] else 'patient'] = {'reference': 'Patient/p999'}
        else:
            return self.outcome(400, 'search not supported for ' + rtype, 'not-supported')
        return self.bundle(inst, rtype, q, items, include)

    def bundle(self, inst, rtype, q, items, include):
        count = max(1, min(50, int((q.get('_count') or ['10'])[0] or 10)))
        offset = int((q.get('_offset') or ['0'])[0] or 0)
        page = items[offset:offset + count]
        b = {'resourceType': 'Bundle', 'type': 'searchset', 'total': len(items), 'link': [],
             'entry': [{'fullUrl': '%s/%s/%s' % (self.base(inst), r['resourceType'], r['id']), 'resource': r, 'search': {'mode': 'match'}} for r in page] +
                      [{'fullUrl': '%s/Schedule/%s' % (self.base(inst), r['id']), 'resource': r, 'search': {'mode': 'include'}} for r in include]}
        if offset + count < len(items):
            nq = {k: v for k, v in q.items() if k != '_offset'}
            nq['_offset'] = [str(offset + count)]
            host = 'http://evil.example' if self.fault(inst, 'hostile_next') else self.public
            b['link'].append({'relation': 'next', 'url': '%s/%s/fhir/%s?%s' % (host, inst, rtype, urllib.parse.urlencode(nq, doseq=True))})
        if self.fault(inst, 'partial'):
            b['total'] = len(items) + 5
            b['link'] = []
        return self.json(200, b)

    def book(self, inst, pid, headers, body):
        d = self.inst[inst]
        try:
            a = json.loads(body or b'{}')
        except ValueError:
            return self.outcome(400, 'invalid JSON', 'invalid')
        if a.get('resourceType') != 'Appointment':
            return self.outcome(400, 'expected an Appointment', 'invalid')
        refs = [p.get('actor', {}).get('reference', '') for p in a.get('participant', [])]
        if 'Patient/' + pid not in refs:
            return self.outcome(403, 'you can only book for yourself', 'forbidden')
        if any(r.startswith('Patient/') and r != 'Patient/' + pid for r in refs):
            return self.outcome(403, 'another patient is named', 'forbidden')
        cond = headers.get('If-None-Exist') or ''
        ident = None
        if cond.startswith('identifier='):
            sysv = urllib.parse.unquote(cond[len('identifier='):])
            for x in d['appts'].values():
                if any('%s|%s' % (i.get('system'), i.get('value')) == sysv for i in x.get('identifier', [])):
                    return self.json(200, x, {'Location': '%s/Appointment/%s' % (self.base(inst), x['id'])})   # conditional create: already exists
            s, _, v = sysv.partition('|')
            ident = {'system': s, 'value': v}
        slots = [s.get('reference', '')[5:] for s in a.get('slot', [])]
        if len(slots) != 1 or slots[0] not in d['slots']:
            return self.outcome(422, 'exactly one existing slot is required', 'required')
        slot = d['slots'][slots[0]]
        if self.fault(inst, 'slot_race'):
            slot['status'] = 'busy'
        if slot['status'] != 'free':
            return self.outcome(409, 'This time is no longer available.', 'conflict')
        sch = d['schedules'][slot['schedule']['reference'][9:]]
        pr = sch['actor'][0]['reference'][13:]
        lo = sch['actor'][1]['reference'][9:]
        svc = slot['serviceType'][0]['text']
        if svc == 'follow-up' and (pid, pr) not in d['established']:
            return self.outcome(422, 'Follow-up visits are for established patients of this clinician; choose a new-patient visit.', 'business-rule')
        self.counter += 1
        aid = 'ap-%d' % self.counter
        appt = self._appt(inst, aid, pid, pr, lo, dt.datetime.fromisoformat(slot['start']), dt.datetime.fromisoformat(slot['end']),
                          'booked' if d['policy'] == 'booked' else 'pending', svc, ident)
        if a.get('comment'):
            appt['comment'] = str(a['comment'])[:500]
        d['appts'][aid] = appt
        slot['status'] = 'busy' if d['policy'] == 'booked' else 'busy-tentative'
        slot['meta']['versionId'] = str(int(slot['meta']['versionId']) + 1)
        if self.fault(inst, 'book_commit_then_504'):
            return self.outcome(504, 'Gateway timeout (synthetic; the appointment WAS created)', 'timeout')
        if self.fault(inst, 'book_commit_then_drop'):
            raise Drop()
        return self.json(201, appt, {'Location': '%s/Appointment/%s' % (self.base(inst), aid), 'ETag': 'W/"1"'})

    def update_appt(self, inst, pid, aid, headers, body):
        d = self.inst[inst]
        cur = d['appts'].get(aid)
        if not cur:
            return self.outcome(404, 'not found', 'not-found')
        if not any(p['actor']['reference'] == 'Patient/' + pid for p in cur['participant']):
            return self.outcome(403, 'not in your patient compartment', 'forbidden')
        if headers.get('If-Match') and headers['If-Match'] != 'W/"%s"' % cur['meta']['versionId']:
            return self.outcome(412, 'version conflict', 'conflict')
        new = json.loads(body or b'{}')
        if new.get('status') != 'cancelled' or cur['status'] in ('cancelled', 'fulfilled'):
            return self.outcome(422, 'patients may only cancel an upcoming appointment', 'business-rule')
        cur['status'] = 'cancelled'
        cur['meta']['versionId'] = str(int(cur['meta']['versionId']) + 1)
        for s in d['slots'].values():
            if s['start'] == cur['start'] and s['status'].startswith('busy'):
                sch = d['schedules'][s['schedule']['reference'][9:]]
                if any(a['reference'] in [p['actor']['reference'] for p in cur['participant']] for a in sch['actor']):
                    s['status'] = 'free'
        if self.fault(inst, 'cancel_commit_then_504'):
            return self.outcome(504, 'Gateway timeout (synthetic; the cancellation WAS saved)', 'timeout')
        return self.json(200, cur, {'ETag': 'W/"%s"' % cur['meta']['versionId']})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(ehr, host='127.0.0.1', port=0):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'mock-ehr'
        sys_version = ''

        def log_message(self, *a):
            pass

        def go(self, method):
            path, _, query = self.path.partition('?')
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            try:
                status, headers, out = ehr.handle(method, path, query, dict(self.headers), body)
            except Drop:
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(out)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self.go('GET')

        def do_POST(self):
            self.go('POST')

        def do_PUT(self):
            self.go('PUT')

    srv = Server((host, port), Handler)
    return srv


def serve_in_thread(ehr, host='127.0.0.1'):
    srv = make_server(ehr, host, 0)
    ehr.public = 'http://%s:%d' % (host, srv.server_address[1])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def main(argv):
    if not argv or argv[0] != 'serve':
        print(__doc__)
        return 2
    args = {'--port': '3351', '--public': None, '--today': dt.date.today().isoformat(), '--admin-token': None, '--admin-token-file': None}
    redirects = []
    i = 1
    while i < len(argv):
        if argv[i] == '--redirect':
            redirects.append(argv[i + 1])
        elif argv[i] in args:
            args[argv[i]] = argv[i + 1]
        i += 2
    port = int(args['--port'])
    if args['--admin-token-file'] and not args['--admin-token']:
        import os
        path = args['--admin-token-file']
        try:
            args['--admin-token'] = open(path).read().strip()
        except OSError:
            args['--admin-token'] = secrets.token_urlsafe(24)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, args['--admin-token'].encode())
            os.close(fd)
    ehr = EHR(today=args['--today'], public=args['--public'] or 'http://127.0.0.1:%d' % port, admin_token=args['--admin-token'],
              redirects=tuple(redirects) or DEFAULT_REDIRECTS)
    srv = make_server(ehr, '127.0.0.1', port)
    print('Mock EHR (synthetic) on 127.0.0.1:%d · public %s · FHIR bases: %s · admin token %s…' % (
        port, ehr.public, ', '.join(ehr.base(k) for k in ehr.inst), ehr.admin_token[:6]), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
