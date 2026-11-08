"""Synthetic pharmacy API for the mock EHR server (web 0.23.0; build-plans/health/04-pharmacy-integration.md).

A CVS/Walgreens-shaped stand-in (no real vendor contract is implied): one pharmacy chain, `corner` — "Cornerstone Pharmacy
(synthetic)" — with the same SMART-style sign-in as the mock portals (account owner / corner-test → patient c1), and a JSON API at
/corner/rx/v1:

  GET  /.well-known/smart-configuration     (issuer = the rx base)
  GET  /patient                              the signed-in account's patient id and name
  GET  /prescriptions                        the signed-in patient's prescriptions (rx id, rx number, store, medication, refills, expiry)
  GET  /prescriptions/{id}                   one prescription (ETag = version)
  GET  /prescriptions/{id}/eligibility       eligible | too_soon (earliest date) | renewal_required | expired | on_hold, copay if known
  POST /refills                              Idempotency-Key required; same key + same body → the same refill; same key + other body → 422
  GET  /refills?idempotency_key=K | ?prescription_id=ID ;  GET /refills/{id}
  GET  /stores/{id}                          pickup / delivery capability
  POST /webhooks {"url"}                     registers a callback; answers the signing secret once
Status: received → processing → ready → picked_up | delivered; or rejected / out_of_stock / renewal_requested / cancelled.
Webhooks: POST {event_id, refill_id, prescription_id, patient, status, status_text, version, occurred_at} with
  X-Rx-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>.
Admin (token): /_admin/corner/advance {"refill","status","text"?,"deliver"?} → the signed event (and delivery if a webhook is set);
  /_admin/corner/prescription {"id", field: value}; faults: rate_limit, outage, wrong_patient, refill_commit_then_504, error_500.
"""
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.request

STATUS_TEXT = {'received': 'Refill request received', 'processing': 'We are filling your prescription', 'ready': 'Ready for pickup',
               'picked_up': 'Picked up', 'delivered': 'Delivered', 'rejected': 'Refill request could not be completed',
               'out_of_stock': 'Out of stock at this store', 'renewal_requested': 'We asked your prescriber for a new prescription', 'cancelled': 'Cancelled'}
OPEN = ('received', 'processing', 'ready')


def institution(today):
    def rx(rid, num, pid, store, name, strength, form, refills, last_days_ago, supply, expires, status='active', copay=None):
        return {'id': rid, 'rx_number': num, 'patient': pid, 'store_id': store, 'medication': {'name': name, 'strength': strength, 'form': form},
                'prescriber': 'Dr. Wei Chen (synthetic)', 'refills_remaining': refills, 'last_fill_date': (today - dt.timedelta(days=last_days_ago)).isoformat(),
                'days_supply': supply, 'expires_on': expires, 'status': status, 'copay_usd': copay, 'version': 1, 'synthetic': True}
    far = (today + dt.timedelta(days=200)).isoformat()
    return {
        'name': 'Cornerstone Pharmacy (synthetic)', 'policy': 'pharmacy', 'kind': 'pharmacy',
        'users': {'owner': ('corner-test', 'c1'), 'maria': ('corner-test', 'c2')},
        'patients': {'c1': {'id': 'c1', 'name': 'Alex Testpatient'}, 'c2': {'id': 'c2', 'name': 'Maria Synthetic'}},
        'practitioners': {}, 'locations': {}, 'rules': [], 'established': set(), 'meds': {}, 'allergies': {}, 'observations': {}, 'appointments': [],
        'stores': {'st-100': {'id': 'st-100', 'name': 'Cornerstone Midtown (synthetic)', 'address': '100 Example Ave, Atlanta GA', 'capabilities': ['pickup']},
                   'st-200': {'id': 'st-200', 'name': 'Cornerstone Decatur (synthetic)', 'address': '200 Sample St, Decatur GA', 'capabilities': ['pickup', 'delivery']}},
        'rx': {r['id']: r for r in [
            rx('rx-1001', 'RX7001', 'c1', 'st-100', 'Atorvastatin', '20 mg', 'tablet', 3, 27, 30, far, copay=0.0),
            rx('rx-1002', 'RX7002', 'c1', 'st-100', 'Atorvastatin', '40 mg', 'tablet', 2, 8, 30, far, copay=0.0),      # same name, other strength; too soon
            rx('rx-1003', 'RX7003', 'c1', 'st-100', 'Lisinopril', '10 mg', 'tablet', 0, 85, 90, far),                  # no refills left
            rx('rx-1004', 'RX7004', 'c1', 'st-100', 'Amoxicillin', '500 mg', 'capsule', 1, 200, 10, (today - dt.timedelta(days=20)).isoformat(), status='expired'),
            rx('rx-1005', 'RX7005', 'c1', 'st-200', 'Atorvastatin', '20 mg', 'tablet', 4, 29, 30, far, copay=4.5),     # same drug, other store
            rx('rx-1006', 'RX7006', 'c1', 'st-200', 'Cetirizine', '10 mg', 'tablet', 2, 40, 30, far),                  # copay unknown
            rx('rx-2001', 'RX8001', 'c2', 'st-100', 'Metformin', '500 mg', 'tablet', 5, 40, 30, far, copay=0.0),
        ]},
        'refills': {}, 'idem': {}, 'webhooks': {},
    }


class Rx:
    def __init__(self, ehr, secret=None):
        self.ehr = ehr
        self.n = 5000
        self.clock = time.time      # tests set this to their own clock for signature timestamps

    def base(self, inst):
        return '%s/%s/rx/v1' % (self.ehr.public, inst)

    def smart_config(self, inst):
        e = self.ehr
        return e.json(200, {'issuer': self.base(inst), 'authorization_endpoint': '%s/%s/oauth2/authorize' % (e.public, inst), 'token_endpoint': '%s/%s/oauth2/token' % (e.public, inst),
                            'grant_types_supported': ['authorization_code', 'refresh_token'], 'code_challenge_methods_supported': ['S256'],
                            'token_endpoint_auth_methods_supported': ['none'], 'response_types_supported': ['code'],
                            'scopes_supported': ['launch/patient', 'openid', 'offline_access', 'rx/prescriptions.read', 'rx/refills.write'], 'capabilities': ['launch-standalone', 'client-public']})

    def err(self, status, code, text, **extra):
        return self.ehr.json(status, {'error': code, 'message': text, **extra})

    def eligibility(self, d, r):
        today = self.ehr.today
        if r['status'] == 'on_hold':
            return {'eligible': False, 'reason': 'on_hold', 'message': 'This prescription is on hold at the pharmacy.'}
        if r['status'] == 'expired' or r['expires_on'] < today.isoformat():
            return {'eligible': False, 'reason': 'expired', 'message': 'This prescription has expired; your prescriber must send a new one.'}
        if r['refills_remaining'] <= 0:
            return {'eligible': False, 'reason': 'renewal_required', 'message': 'No refills remain; a renewal from your prescriber is needed.'}
        earliest = dt.date.fromisoformat(r['last_fill_date']) + dt.timedelta(days=int(r['days_supply'] * 0.75))
        if earliest > today:
            return {'eligible': False, 'reason': 'too_soon', 'earliest_date': earliest.isoformat(), 'message': 'Too soon to refill.'}
        if any(f['prescription_id'] == r['id'] and f['status'] in OPEN for f in d['refills'].values()):
            return {'eligible': False, 'reason': 'in_progress', 'message': 'A refill is already in progress.'}
        return {'eligible': True, 'reason': 'ok', 'copay_usd': r['copay_usd'], 'fulfillment': d['stores'][r['store_id']]['capabilities']}

    def handle(self, inst, method, rest, q, headers, body):
        e, d = self.ehr, self.ehr.inst[inst]
        if rest == '/v1/.well-known/smart-configuration':
            return self.smart_config(inst)
        auth = headers.get('Authorization') or ''
        tok = e.tokens.get(auth[7:]) if auth.startswith('Bearer ') else None
        if not tok or tok['inst'] != inst or tok['expires'] < time.time():
            return 401, {'WWW-Authenticate': 'Bearer error="invalid_token"', 'Content-Type': 'application/json'}, b'{"error":"invalid_token"}'
        if e.faults[inst].get('outage'):
            return self.err(503, 'unavailable', 'Pharmacy systems are down for maintenance (synthetic)')
        if e.fault(inst, 'rate_limit'):
            return 429, {'Retry-After': '1', 'Content-Type': 'application/json'}, b'{"error":"rate_limited"}'
        if e.fault(inst, 'error_500'):
            return self.err(500, 'server_error', 'synthetic internal error')
        pid, scope = tok['patient'], tok['scope'].split()
        parts = [x for x in rest.strip('/').split('/') if x][1:]       # drop v1
        one = lambda k: (q.get(k) or [''])[0]
        write = method == 'POST'
        if ('rx/refills.write' if write and parts[:1] == ['refills'] else 'rx/prescriptions.read') not in scope:
            return self.err(403, 'insufficient_scope', 'not permitted by the granted scopes')
        with e.lock:
            mine = lambda r: r['patient'] == pid or e.faults[inst].get('wrong_patient')
            if method == 'GET' and parts == ['patient']:
                return e.json(200, {'id': pid, 'name': d['patients'][pid]['name'], 'synthetic': True})
            if method == 'GET' and parts == ['prescriptions']:
                items = [r for r in d['rx'].values() if mine(r)]
                if one('status'):
                    items = [r for r in items if r['status'] == one('status')]
                return e.json(200, {'patient': pid, 'prescriptions': items})
            if method == 'GET' and len(parts) >= 2 and parts[0] == 'prescriptions':
                r = d['rx'].get(parts[1])
                if not r or not mine(r):
                    return self.err(404, 'not_found', 'no such prescription')
                if len(parts) == 2:
                    return e.json(200, r, {'ETag': 'W/"%d"' % r['version']})
                if parts[2:] == ['eligibility']:
                    return e.json(200, {'prescription_id': r['id'], 'version': r['version'], **self.eligibility(d, r)})
            if method == 'GET' and parts == ['refills']:
                items = [f for f in d['refills'].values() if f['patient'] == pid]
                if one('idempotency_key'):
                    items = [f for f in items if f['idempotency_key'] == one('idempotency_key')]
                if one('prescription_id'):
                    items = [f for f in items if f['prescription_id'] == one('prescription_id')]
                return e.json(200, {'refills': items})
            if method == 'GET' and len(parts) == 2 and parts[0] == 'refills':
                f = d['refills'].get(parts[1])
                if not f or f['patient'] != pid:
                    return self.err(404, 'not_found', 'no such refill')
                return e.json(200, f)
            if method == 'GET' and len(parts) == 2 and parts[0] == 'stores':
                s = d['stores'].get(parts[1])
                return e.json(200, s) if s else self.err(404, 'not_found', 'no such store')
            if method == 'POST' and parts == ['webhooks']:
                data = json.loads(body or b'{}')
                url = str(data.get('url') or '')
                if not url.startswith(('https://', 'http://127.0.0.1')):
                    return self.err(400, 'bad_url', 'webhook url must be https')
                wid, sec = 'wh-' + secrets.token_hex(4), secrets.token_urlsafe(32)
                d['webhooks'][pid] = {'id': wid, 'url': url, 'secret': sec}
                return e.json(201, {'id': wid, 'url': url, 'secret': sec})
            if method == 'POST' and parts == ['refills']:
                return self.refill(inst, d, pid, headers, body)
        return self.err(405, 'not_supported', 'operation not supported')

    def refill(self, inst, d, pid, headers, body):
        e = self.ehr
        key = next((v for k, v in headers.items() if k.lower() == 'idempotency-key'), '')
        if not re.fullmatch(r'[A-Za-z0-9_:.-]{8,100}', key):
            return self.err(400, 'idempotency_key_required', 'send an Idempotency-Key header')
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            return self.err(400, 'bad_json', 'unreadable body')
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        prev = d['idem'].get((pid, key))
        if prev:
            if prev['digest'] != digest:
                return self.err(422, 'idempotency_key_reused', 'this Idempotency-Key was used for a different request')
            return e.json(200, d['refills'][prev['refill']])
        r = d['rx'].get(str(data.get('prescription_id')))
        if not r or r['patient'] != pid:
            return self.err(404, 'not_found', 'no such prescription')
        if data.get('store_id') != r['store_id']:
            return self.err(422, 'transfer_required', 'this prescription is filled at %s; moving it is a transfer' % r['store_id'])
        ful = data.get('fulfillment') or 'pickup'
        if ful not in d['stores'][r['store_id']]['capabilities']:
            return self.err(422, 'fulfillment_unsupported', '%s is not offered at this store' % ful)
        el = self.eligibility(d, r)
        if not el['eligible']:
            if el['reason'] == 'in_progress':
                cur = next(f for f in d['refills'].values() if f['prescription_id'] == r['id'] and f['status'] in OPEN)
                return self.err(409, 'refill_in_progress', el['message'], refill_id=cur['id'])
            return self.err(422, el['reason'], el['message'], **{k: v for k, v in el.items() if k == 'earliest_date'})
        self.n += 1
        fid = 'rf-%d' % self.n
        f = {'id': fid, 'prescription_id': r['id'], 'rx_number': r['rx_number'], 'patient': pid, 'store_id': r['store_id'], 'fulfillment': ful,
             'status': 'received', 'status_text': STATUS_TEXT['received'], 'version': 1, 'idempotency_key': key, 'copay_usd': r['copay_usd'],
             'created': dt.datetime.now(dt.timezone.utc).isoformat(), 'synthetic': True}
        d['refills'][fid] = f
        d['idem'][(pid, key)] = {'digest': digest, 'refill': fid}
        r['refills_remaining'] -= 1
        r['version'] += 1
        if e.fault(inst, 'refill_commit_then_504'):
            return self.err(504, 'timeout', 'Gateway timeout (synthetic; the refill WAS recorded)')
        return e.json(202, f)

    # ---------- admin ----------
    def sign(self, secret, raw, t=None):
        t = int(t or self.clock())
        return 't=%d,v1=%s' % (t, hmac.new(secret.encode(), b'%d.' % t + raw, hashlib.sha256).hexdigest())

    def advance(self, inst, data):
        e, d = self.ehr, self.ehr.inst[inst]
        f = d['refills'].get(data.get('refill'))
        if not f:
            return e.json(404, {'error': 'no such refill'})
        st = data.get('status')
        if st not in STATUS_TEXT:
            return e.json(400, {'error': 'status is one of ' + ', '.join(STATUS_TEXT)})
        f['status'], f['status_text'], f['version'] = st, data.get('text') or STATUS_TEXT[st], f['version'] + 1
        if st in ('rejected', 'out_of_stock', 'cancelled'):
            d['rx'][f['prescription_id']]['refills_remaining'] += 1
        ev = {'event_id': 'ev-' + secrets.token_hex(6), 'type': 'refill.status', 'refill_id': f['id'], 'prescription_id': f['prescription_id'], 'patient': f['patient'],
              'status': f['status'], 'status_text': f['status_text'], 'version': f['version'], 'occurred_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        raw = json.dumps(ev, separators=(',', ':')).encode()
        hook = d['webhooks'].get(f['patient'])
        out = {'event': ev, 'body': raw.decode()}
        if hook:
            out['signature'] = self.sign(hook['secret'], raw)
            if data.get('deliver', True):
                def go():
                    try:
                        urllib.request.urlopen(urllib.request.Request(hook['url'], data=raw, headers={'Content-Type': 'application/json', 'X-Rx-Signature': out['signature']},
                                                                      method='POST'), timeout=10).read()
                    except Exception:
                        pass
                threading.Thread(target=go, daemon=True).start()
        return e.json(200, out)

    def set_rx(self, inst, data):
        e, d = self.ehr, self.ehr.inst[inst]
        r = d['rx'].get(data.get('id'))
        if not r:
            return e.json(404, {'error': 'no such prescription'})
        for k in ('refills_remaining', 'last_fill_date', 'status', 'store_id', 'copay_usd', 'expires_on'):
            if k in data:
                r[k] = data[k]
        r['version'] += 1
        return e.json(200, r)
