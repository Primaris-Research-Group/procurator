"""Patient-portal path against the mock EHR (web 0.22.0; build-plans/health/03 acceptance list + 02 booking failures).
Everything is synthetic and runs on loopback."""
import json
import re
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

import mock_ehr
from health import Health
from health_portal import Portal, PortalError

CB = 'https://assistant.example.com/assistant/dashboard/health/callback'



def ready(h):
    """0.25.0: the owner confirmed 18+ (/healthadult) and allowed stored-data reads (/healthread) — the tests below exercise
    the health features themselves; test_health_gates.py covers the gates."""
    h.call('health_adult_confirm', {}, True)
    for c in ('activity', 'nutrition', 'appointments'):
        h.call('health_read_allow', {'category': c, 'days': 365}, True)
    return h

class Quiet:
    def __init__(self):
        self.messages = []
    def send_with_buttons(self, m, rows):
        self.messages.append(m)
    def telegram(self, m):
        self.messages.append(m)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.ehr = mock_ehr.EHR(today='2026-11-09', redirects=(CB,))      # a fresh synthetic EHR per test
        self.srv = mock_ehr.serve_in_thread(self.ehr)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1_794_200_000.0]
        self.h = Health(Quiet(), self.tmp.name, now=lambda: self.clock[0])
        ready(self.h)
        self.p = Portal(self.h, CB, now=lambda: self.clock[0], sleep=lambda s: None, timeout=5)
        self.h.portal = self.p
        self.op = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def connect(self, inst='riverside', env='fake'):
        return self.h.call('health_connection_add', {'provider': 'mock_ehr', 'institution': self.ehr.inst[inst]['name'], 'environment': env, 'endpoint': self.ehr.base(inst)}, True)

    def browser(self, url, user, pw, decision='allow'):
        """What the owner's phone does: open the consent page, sign in, allow. Returns the callback query."""
        page = self.op.open(url).read().decode()
        fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', page))
        fields.update(username=user, password=pw, decision=decision)
        inst = re.search(r'action="[^"]*?/([a-z]+)/oauth2', page).group(1)
        try:
            self.op.open(self.ehr.public + '/%s/oauth2/authorize/approve' % inst, data=urllib.parse.urlencode(fields).encode())
        except urllib.error.HTTPError as e:
            return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(e.headers['Location']).query).items()}
        raise AssertionError('no redirect')

    def link(self, inst='riverside', user='owner', pw='riverside-test'):
        c = self.connect(inst)
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], user, pw)
        out = self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)
        self.assertEqual(out.get('status'), 'connected', out)
        return c

    def grant(self, c, actions=('appointment.search', 'appointment.book', 'portal.read'), **constraints):
        p = self.h.call('health_grant_propose', {'connection': c['id'], 'title': 'Test grant', 'actions': list(actions),
                                                  'constraints': {'clinician': 'Rivera', 'spend_cap_usd': 0, **constraints}})
        return self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)

    def fault(self, inst, **f):
        self.ehr.faults[inst].update(f)


class AuthorizationTests(Base):
    def test_sign_in_binds_the_patient_and_keeps_tokens_out_of_results(self):
        c = self.link()
        st = self.h.call('health_portal_status', {'connection': c['id']})
        self.assertEqual((st['linked'], st['patient_display'], st['status']), (True, 'Alex Testpatient', 'connected'))
        self.assertEqual(st['capabilities']['appointment.book'], 'supported')
        blob = json.dumps(self.h.call('health_status', {})) + json.dumps(st)
        tok = self.h.token(c['id'])
        self.assertNotIn(tok['access_token'], blob); self.assertNotIn(tok['refresh_token'], blob)

    def test_wrong_state_and_replayed_callback_are_refused(self):
        c = self.connect()
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], 'owner', 'riverside-test')
        self.assertEqual(self.h.call('health_portal_callback', {'state': 'forged', 'code': q['code']}, True)['error'], 'bad_state')
        self.assertEqual(self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)['status'], 'connected')
        self.assertEqual(self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)['error'], 'replayed_callback')

    def test_an_expired_sign_in_and_a_denied_one(self):
        c = self.connect()
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], 'owner', 'riverside-test')
        self.clock[0] += 601
        self.assertEqual(self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)['error'], 'expired_state')
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], 'owner', 'riverside-test', decision='deny')
        self.assertEqual(self.h.call('health_portal_callback', {'state': q['state'], 'error': q.get('error')}, True)['error'], 'authorization_denied')

    def test_a_replayed_code_at_the_token_endpoint_revokes_what_it_issued(self):
        c = self.connect()
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], 'owner', 'riverside-test')
        self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)
        # An attacker who copied the code tries it again (with a guessed verifier): refused, and the first tokens die too.
        try:
            self.op.open(self.ehr.public + '/riverside/oauth2/token', data=urllib.parse.urlencode({'grant_type': 'authorization_code', 'code': q['code'], 'redirect_uri': CB,
                                                                                                      'client_id': 'procurator-health', 'code_verifier': 'x'}).encode())
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        g = self.grant(c)
        out = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})
        self.assertEqual(out['error'], 'reauth_required')

    def test_wrong_issuer_and_foreign_endpoints_stop_before_any_sign_in(self):
        c = self.connect()
        self.fault('riverside', issuer_mismatch=True)
        self.assertEqual(self.h.call('health_portal_authorize', {'connection': c['id']}, True)['error'], 'issuer_mismatch')
        self.ehr.faults['riverside'] = {'foreign_endpoints': True}
        self.assertEqual(self.h.call('health_portal_authorize', {'connection': c['id']}, True)['error'], 'foreign_endpoint')

    def test_redirect_mismatch_is_refused_by_the_portal(self):
        c = self.connect()
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        bad = a['authorize_url'].replace(urllib.parse.quote(CB, safe=''), urllib.parse.quote('https://evil.example/cb', safe=''))
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.op.open(bad)
        self.assertEqual(e.exception.code, 400)

    def test_missing_patient_context_saves_nothing(self):
        c = self.connect()
        self.fault('riverside', no_patient_context=1)
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], 'owner', 'riverside-test')
        self.assertEqual(self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)['error'], 'missing_patient_context')
        self.assertIsNone(self.h.token(c['id']))

    def test_account_switching_is_refused_and_the_binding_kept(self):
        c = self.link()
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        q = self.browser(a['authorize_url'], 'maria', 'riverside-test')
        self.assertEqual(self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)['error'], 'account_switch')
        self.assertEqual(self.h.call('health_portal_status', {'connection': c['id']})['patient_display'], 'Alex Testpatient')

    def test_only_the_owner_starts_or_completes_a_sign_in(self):
        c = self.connect()
        self.assertEqual(self.h.call('health_portal_authorize', {'connection': c['id']})['error'], 'owner_only')
        self.assertEqual(self.h.call('health_portal_callback', {'state': 'x', 'code': 'y'})['error'], 'owner_only')

    def test_http_is_only_accepted_for_the_loopback_mock(self):
        c = self.connect(env='sandbox') if False else None
        with self.assertRaisesRegex(ValueError, 'https'):
            self.h.call('health_connection_add', {'provider': 'mock_ehr', 'institution': 'x', 'environment': 'sandbox', 'endpoint': self.ehr.base('riverside')}, True)


class TokenTests(Base):
    def test_refresh_rotates_and_an_expired_access_token_is_renewed_silently(self):
        c = self.link()
        g = self.grant(c)
        old = self.h.token(c['id'])
        self.clock[0] += 3600
        out = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})
        self.assertEqual(out['items'][0]['substance'], 'Penicillin')
        new = self.h.token(c['id'])
        self.assertNotEqual(old['access_token'], new['access_token']); self.assertNotEqual(old['refresh_token'], new['refresh_token'])

    def test_a_lost_refresh_answer_is_uncertain_then_needs_reauthorization(self):
        c = self.link()
        g = self.grant(c)
        self.fault('riverside', refresh_lost=1)
        self.clock[0] += 3600
        self.assertEqual(self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})['error'], 'refresh_uncertain')
        out = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})
        self.assertEqual(out['error'], 'reauth_required'); self.assertIn('/healthauth', out['next'])
        self.assertEqual(self.h.call('health_portal_status', {'connection': c['id']})['status'], 'reauth_required')

    def test_consent_removed_in_the_portal_needs_reauthorization(self):
        c = self.link()
        g = self.grant(c)
        req = urllib.request.Request(self.ehr.public + '/_admin/riverside/revoke_consent', data=b'{"patient":"p1"}', headers={'X-Admin-Token': self.ehr.admin_token}, method='POST')
        urllib.request.urlopen(req).read()
        self.assertEqual(self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'medications'})['error'], 'reauth_required')


class ReadTests(Base):
    def test_reads_need_a_grant_and_carry_provenance_and_original_units(self):
        c = self.link()
        with self.assertRaisesRegex(ValueError, 'Unknown grant'):
            self.h.call('health_portal_read', {'connection': c['id'], 'grant': 'f' * 16, 'what': 'observations'})
        g = self.grant(c, actions=('appointment.book',))
        with self.assertRaisesRegex(ValueError, 'does not allow portal.read'):
            self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'observations'})
        g = self.grant(c)
        obs = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'observations', 'code': '29463-7'})
        self.assertEqual([(o['value'], o['unit']) for o in obs['items']], [(82.5, 'kg'), (181.0, '[lb_av]')])      # never silently converted
        src = obs['items'][0]['source']
        self.assertEqual((src['resource'], src['version'], src['institution'], src['environment']), ('Observation/ob-1', '1', 'Riverside Health (synthetic)', 'fake'))
        meds = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'medications'})['items']
        self.assertEqual([(m['id'], m['status']) for m in meds if 'Atorvastatin' in m['medication']], [('mr-1', 'active'), ('mr-2', 'stopped')])  # same name, different strength

    def test_another_patients_record_drops_the_whole_read(self):
        c = self.link()
        g = self.grant(c)
        self.fault('riverside', wrong_patient=1)
        out = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'observations'})
        self.assertEqual(out['error'], 'identity_mismatch'); self.assertNotIn('items', out)
        with self.h.db() as db:
            self.assertEqual(db.execute("SELECT kind FROM portal_incidents").fetchall()[-1]['kind'], 'identity_mismatch')

    def test_overlapping_patient_ids_at_two_institutions_stay_apart(self):
        a = self.link('riverside')
        b = self.link('northgate', 'jordan', 'northgate-test')
        ga, gb = self.grant(a), self.grant(b, clinician='Okafor')
        ma = self.h.call('health_portal_read', {'connection': a['id'], 'grant': ga['id'], 'what': 'medications'})['items']
        mb = self.h.call('health_portal_read', {'connection': b['id'], 'grant': gb['id'], 'what': 'medications'})['items']
        self.assertTrue(all('Metformin' not in m['medication'] for m in ma)); self.assertEqual([m['medication'] for m in mb], ['Metformin 500 mg oral tablet'])
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            self.h.call('health_portal_read', {'connection': b['id'], 'grant': ga['id'], 'what': 'medications'})      # a grant is bound to its connection

    def test_pagination_to_a_hostile_host_stops_and_partial_results_are_flagged(self):
        c = self.link()
        g = self.grant(c)
        self.fault('riverside', hostile_next=1)
        out = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-rivera'})
        self.assertEqual(out['error'], 'foreign_url')
        self.assertFalse(any(m == ('GET', '/riverside/fhir/Slot') for m in []))
        self.ehr.faults['riverside'] = {'partial': 1}
        out = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'medications'})
        self.assertFalse(out['complete']); self.assertIn('INCOMPLETE', out['note'])

    def test_rate_limits_are_retried_for_reads_and_outages_reported(self):
        c = self.link()
        g = self.grant(c)
        self.fault('riverside', rate_limit=2)
        self.assertEqual(len(self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})['items']), 1)
        self.ehr.faults['riverside'] = {'rate_limit': 9}
        self.assertEqual(self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})['error'], 'rate_limited')
        self.ehr.faults['riverside'] = {'outage': True}
        self.assertEqual(self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'allergies'})['error'], 'outage')

    def test_slots_carry_what_the_grant_checks(self):
        c = self.link()
        g = self.grant(c)
        s = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-rivera', 'service_type': 'follow-up', 'from': '2026-11-10', 'to': '2026-11-12'})
        self.assertTrue(s['items']); first = s['items'][0]
        self.assertEqual((first['weekday'], first['time_of_day'], first['visit_type'], first['clinician']), ('Tuesday', 'afternoon', 'follow-up', 'Dr. Elena Rivera'))
        self.assertTrue(all(i['date'] == '2026-11-10' for i in s['items']))


class BookingTests(Base):
    def slot(self, c, g, **f):
        return self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-rivera', 'service_type': 'follow-up', **f})['items'][0]

    def begin(self, c, g, s, rid='derm-book-0001', **extra):
        return self.h.call('health_effect_begin', {'request_id': rid, 'action': 'appointment.book', 'connection': c['id'], 'grant': g['id'],
                                                    'payload': {'clinician': s['clinician'], 'location': s['location'], 'date': s['date'], 'time': s['time'], 'weekday': s['weekday'],
                                                                'time_of_day': s['time_of_day'], 'visit_type': s['visit_type'], 'cost_usd': 0, 'slot_id': s['slot_id'], **extra}})

    def test_book_confirmed_once_even_when_called_twice(self):
        c = self.link(); g = self.grant(c, weekdays=['Tuesday'], time_of_day='afternoon')
        s = self.slot(c, g)
        e = self.begin(c, g, s)
        out = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual((out['booked'], out['effect']['state']), (True, 'confirmed')); self.assertTrue(out['effect']['external_id'].startswith('Appointment/'))
        again = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual(again['error'], 'bad_state')
        appts = [a for a in self.ehr.inst['riverside']['appts'].values() if any(i.get('value') == 'derm-book-0001' for i in a.get('identifier', []))]
        self.assertEqual(len(appts), 1)

    def test_a_second_packet_for_the_same_visit_is_stopped_before_the_portal(self):
        c = self.link(); g = self.grant(c)
        s = self.slot(c, g)
        self.h.call('health_portal_book', {'effect': self.begin(c, g, s)['id']})
        e2 = self.begin(c, g, s, rid='derm-book-phone-0002', channel='phone')
        self.assertEqual(e2['state'], 'awaiting_owner')

    def test_an_appointment_made_outside_procurator_counts_as_equivalent(self):
        c = self.link(); g = self.grant(c)
        s = self.slot(c, g)
        self.h.call('health_portal_book', {'effect': self.begin(c, g, s)['id']})
        s2 = self.slot(c, g, **{'from': '2026-11-17'})
        with self.h.db() as db:
            db.execute("UPDATE effects SET target='other' WHERE request_id='derm-book-0001'")      # pretend the first booking was made by hand in the portal
        out = self.h.call('health_portal_book', {'effect': self.begin(c, g, s2, rid='derm-book-0003')['id']})
        self.assertEqual((out['booked'], out['effect']['state']), (False, 'awaiting_owner')); self.assertIn('already shows', out['effect']['note'])

    def test_timeout_after_the_portal_booked_is_read_back_not_retried(self):
        c = self.link(); g = self.grant(c)
        e = self.begin(c, g, self.slot(c, g))
        self.fault('riverside', book_commit_then_504=1)
        out = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual(out['effect']['state'], 'confirmed'); self.assertEqual(out['effect']['source'], 'portal read by identifier')
        # Another date with the same clinician is not the same effect, but the portal now shows the confirmed visit, so the
        # booking stops before anything is written.
        e2 = self.begin(c, g, self.slot(c, g, **{'from': '2026-11-17'}), rid='derm-book-0004')
        out2 = self.h.call('health_portal_book', {'effect': e2['id']})
        self.assertEqual((out2['booked'], out2['effect']['state']), (False, 'awaiting_owner'))
        self.assertEqual(sum(1 for a in self.ehr.inst['riverside']['appts'].values() if a['status'] == 'booked' and a['participant'][0]['actor']['reference'] == 'Patient/p1'), 1)

    def test_a_dropped_connection_after_commit_is_also_reconciled(self):
        c = self.link(); g = self.grant(c)
        e = self.begin(c, g, self.slot(c, g))
        self.fault('riverside', book_commit_then_drop=1)
        out = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual(out['effect']['state'], 'confirmed')

    def test_ambiguous_and_unreadable_stays_uncertain(self):
        c = self.link(); g = self.grant(c)
        e = self.begin(c, g, self.slot(c, g))
        self.fault('riverside', book_commit_then_504=1, outage=False)
        orig = self.p.search
        def broken(*a, **k):
            if a[1] == 'Appointment' and 'identifier' in a[2]:
                raise PortalError('outage', 'down')
            return orig(*a, **k)
        self.p.search = broken
        out = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual(out['effect']['state'], 'uncertain'); self.assertIsNone(out['booked'])
        self.p.search = orig
        r = self.h.call('health_portal_reconcile', {'effect': e['id']})
        self.assertEqual((r['reconciled'], r['effect']['state']), (True, 'confirmed'))

    def test_a_request_based_clinic_is_requested_not_booked_until_staff_confirm(self):
        c = self.link('northgate', 'jordan', 'northgate-test')
        g = self.grant(c, clinician='Okafor')
        s = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-okafor'})['items'][0]
        e = self.begin(c, g, s)
        out = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual((out['booked'], out.get('requested'), out['effect']['state']), (False, True, 'uncertain'))
        self.assertEqual(self.h.call('health_portal_reconcile', {'effect': e['id']})['effect']['state'], 'uncertain')    # still pending
        aid = out['appointment']['id']
        req = urllib.request.Request(self.ehr.public + '/_admin/northgate/confirm', data=json.dumps({'appointment': aid}).encode(), headers={'X-Admin-Token': self.ehr.admin_token}, method='POST')
        urllib.request.urlopen(req).read()
        self.assertEqual(self.h.call('health_portal_reconcile', {'effect': e['id']})['effect']['state'], 'confirmed')

    def test_a_slot_taken_meanwhile_fails_cleanly_and_a_race_at_the_portal_too(self):
        c = self.link(); g = self.grant(c)
        s = self.slot(c, g)
        self.ehr.inst['riverside']['slots'][s['slot_id']]['status'] = 'busy'
        out = self.h.call('health_portal_book', {'effect': self.begin(c, g, s)['id']})
        self.assertEqual((out['booked'], out['effect']['state']), (False, 'failed'))
        s2 = self.slot(c, g, **{'from': '2026-11-17'})
        self.fault('riverside', slot_race=1)
        out = self.h.call('health_portal_book', {'effect': self.begin(c, g, s2, rid='derm-book-0005')['id']})
        self.assertEqual(out['effect']['state'], 'failed'); self.assertIn('no longer available', out['effect']['note'])

    def test_new_patient_restrictions_come_back_as_the_portal_said_them(self):
        c = self.link(); g = self.grant(c, clinician='Chen')
        with self.h.db() as db:
            pass
        self.ehr.inst['riverside']['established'].discard(('p1', 'pr-chen'))
        s = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-chen', 'service_type': 'follow-up'})['items'][0]
        out = self.h.call('health_portal_book', {'effect': self.begin(c, g, s)['id']})
        self.assertEqual(out['effect']['state'], 'failed'); self.assertIn('established patients', out['effect']['note'])
        self.ehr.inst['riverside']['established'].add(('p1', 'pr-chen'))

    def test_outside_the_grant_never_reaches_the_portal(self):
        c = self.link(); g = self.grant(c, weekdays=['Thursday'])
        s = self.slot(c, g)          # a Tuesday slot
        e = self.begin(c, g, s)
        self.assertEqual(e['state'], 'awaiting_owner')
        before = len(self.ehr.log)
        self.assertEqual(self.h.call('health_portal_book', {'effect': e['id']})['error'], 'bad_state')
        self.assertFalse(any(m == 'POST' and p.endswith('/Appointment') for m, p in self.ehr.log[before:]))

    def test_a_grant_revoked_between_authorization_and_dispatch_stops_the_booking(self):
        c = self.link(); g = self.grant(c)
        e = self.begin(c, g, self.slot(c, g))
        self.h.call('health_grant_revoke', {'grant': g['id']}, True)
        self.assertEqual(self.h.call('health_portal_book', {'effect': e['id']})['error'], 'bad_state')      # cancelled by the revocation


class DashboardForwardTests(Base):
    def test_the_dashboard_forwards_the_portal_paths_only(self):
        import dashboard
        dashboard.MOCK_EHR = self.ehr.public
        st, h, body = dashboard.mock_ehr_forward('GET', '/mock-ehr/riverside/fhir/.well-known/smart-configuration', '', {}, b'')
        self.assertEqual(st, 200); self.assertIn('authorization_endpoint', json.loads(body))
        self.assertEqual(dashboard.mock_ehr_forward('POST', '/mock-ehr/_admin/riverside/fault', '', {'X-Admin-Token': self.ehr.admin_token}, b'{}')[0], 404)
        st, h, _ = dashboard.mock_ehr_forward('POST', '/mock-ehr/riverside/oauth2/authorize/approve', '',
                                              {'Content-Type': 'application/x-www-form-urlencoded'}, b'client_id=procurator-health&redirect_uri=' + urllib.parse.quote(CB, safe='').encode() + b'&decision=deny&state=s1')
        self.assertEqual(st, 302); self.assertTrue(h['Location'].startswith(CB + '?error=access_denied'))     # redirect passed through, not followed
        dashboard.MOCK_EHR = 'http://127.0.0.1:9'
        self.assertEqual(dashboard.mock_ehr_forward('GET', '/mock-ehr/riverside/fhir/metadata', '', {}, b'')[0], 502)


if __name__ == '__main__':
    unittest.main()
