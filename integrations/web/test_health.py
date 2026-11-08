"""Consumer health foundation (web 0.21.0): subjects, connections, grants with revisions, effects, policy, receipts.
Everything here runs against fakes; nothing touches a provider."""
import json
import tempfile
import unittest
from pathlib import Path

from health import Health, payload_hash, target_key



def ready(h):
    """0.25.0: the owner confirmed 18+ (/healthadult) and allowed stored-data reads (/healthread) — the tests below exercise
    the health features themselves; test_health_gates.py covers the gates."""
    h.call('health_adult_confirm', {}, True)
    for c in ('activity', 'nutrition', 'appointments'):
        h.call('health_read_allow', {'category': c, 'days': 365}, True)
    return h

class FakeBroker:
    def __init__(self):
        self.messages = []
    def send_with_buttons(self, message, rows):
        self.messages.append(message)
        return {'ok': True}
    def telegram(self, message):
        self.messages.append(message)


BOOK = {'clinician': 'Dr. Rivera (dermatology)', 'location': 'Midtown clinic', 'date': '2026-11-18', 'time': '14:30', 'visit_type': 'follow-up', 'weekday': 'Tuesday', 'time_of_day': 'afternoon', 'cost_usd': 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1_800_000_000.0]
        self.p = FakeBroker()
        self.h = Health(self.p, self.tmp.name, now=lambda: self.clock[0])
        ready(self.h)
        self.conn = self.h.call('health_connection_add', {'provider': 'clinic_web', 'institution': 'Midtown Dermatology', 'environment': 'fake',
                                                           'capabilities': {'appointment.book': 'supported', 'appointment.cancel': 'unknown'}}, True)
    def tearDown(self):
        self.tmp.cleanup()
    def tick(self, seconds):
        self.clock[0] += seconds
    def grant(self, **extra):
        p = self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Derm follow-up', 'actions': ['appointment.search', 'appointment.book'],
                                                  'constraints': {'clinician': 'Dr. Rivera', 'visit_type': 'follow-up', 'weekdays': ['Tuesday'], 'time_of_day': 'afternoon',
                                                                  'locations': ['Midtown clinic'], 'time_windows': [{'from': '2026-11-10', 'to': '2026-12-20'}], 'spend_cap_usd': 50},
                                                  'disclosures': ['name', 'date of birth', 'phone number', 'insurance carrier'], 'unresolved': ['whether a referral is on file'], **extra})
        self.assertIn('/healthgrant %s %s' % (p['proposal'], p['hash']), self.p.messages[-1])
        return self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)


class FoundationTests(Base):
    def test_owner_is_the_only_subject_until_caregivers_are_enabled(self):
        st = self.h.call('health_status', {})
        self.assertEqual([s['relation'] for s in st['subjects']], ['self'])
        with self.h.db() as db:
            db.execute("INSERT INTO subjects VALUES(?,'owner','Parent','parent','active',0)", ('b' * 16,))
        with self.assertRaisesRegex(ValueError, 'Caregiver'):
            self.h.call('health_connection_add', {'provider': 'cvs', 'institution': 'CVS', 'subject': 'b' * 16}, True)
        self.h.call('health_policy', {'set': {'caregivers': 'on'}}, True)
        self.assertEqual(self.h.call('health_connection_add', {'provider': 'cvs', 'institution': 'CVS', 'subject': 'b' * 16}, True)['subject'], 'b' * 16)

    def test_connections_keep_tokens_out_of_every_tool_result(self):
        self.assertEqual(self.conn['status'], 'pending'); self.assertFalse(self.conn['has_token'])
        self.h.set_token(self.conn['id'], {'access_token': 'secret-A', 'refresh_token': 'secret-R'})
        st = self.h.call('health_status', {})
        self.assertNotIn('secret-A', json.dumps(st)); self.assertNotIn('secret-R', json.dumps(st))
        self.assertTrue(st['connections'][0]['has_token']); self.assertEqual(st['connections'][0]['status'], 'connected')
        self.assertEqual(self.h.token(self.conn['id'])['access_token'], 'secret-A')          # adapters only
        self.h.call('health_connection_status', {'connection': self.conn['id'], 'status': 'revoked', 'why': 'owner disconnected'}, True)
        self.assertIsNone(self.h.token(self.conn['id']))
        self.assertEqual(list(Path(self.tmp.name, 'health-tokens').glob('*.json')), [])
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.h.call('health_connection_add', {'provider': 'epic', 'institution': 'x', 'note': 'password: hunter22'}, True)

    def test_grants_are_proposed_by_the_model_and_approved_only_by_the_owner_with_the_hash(self):
        p = self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Derm', 'actions': ['appointment.book'], 'constraints': {'clinician': 'Dr. Rivera'}})
        self.assertIn('dry run', p['dry_run']); self.assertIn('Dr. Rivera', p['dry_run']); self.assertIn('Stop here', p['next'])
        self.assertEqual(self.h.call('health_status', {})['grants'], [])                         # a proposal is not authority
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']})
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': '0' * 16}, True)
        g = self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)
        self.assertEqual((g['revision'], g['status'], g['actions']), (1, 'active', ['appointment.book']))
        with self.assertRaisesRegex(ValueError, 'already approved'):
            self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)
        with self.assertRaisesRegex(ValueError, 'names the exact prescriptions'):
            self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Rx', 'actions': ['rx.refill']})
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Derm', 'actions': ['appointment.book'], 'constraints': {'clinician': 'x', 'pin': 'otp 123456'}})

    def test_0243_unknown_limits_are_refused_and_the_review_shows_disclosures_and_validity_exactly(self):
        with self.assertRaisesRegex(ValueError, 'Unknown limit.*weekday\\b'):
            self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Derm', 'actions': ['appointment.book'], 'constraints': {'clinician': 'x', 'weekday': 'Monday'}})
        p = self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Read', 'actions': ['appointment.search', 'portal.read'], 'disclosures': [], 'valid_days': 1})
        self.assertIn('May be disclosed to the other party: nothing (no disclosures listed)', p['dry_run'])
        self.assertIn('Valid for: 1 day (until ', p['dry_run'])
        self.assertNotIn('name and contact', p['dry_run'])
        full = self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Chen', 'actions': ['appointment.search', 'appointment.book'],
                                                    'constraints': {'clinician': 'pr-chen', 'specialty': 'Internal Medicine', 'visit_type': 'follow-up', 'locations': ['Midtown'],
                                                                    'weekdays': ['Monday', 'Friday'], 'time_of_day': 'morning', 'spend_cap_usd': 0, 'reschedule_order': 'book_then_cancel'}})
        self.assertIn('Valid for: 90 days', full['dry_run']); self.assertIn('"morning"', full['dry_run'])

    def test_effect_lifecycle_and_request_id_binding(self):
        g = self.grant()
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK, 'step': 'T1/S3'})
        self.assertEqual((e['state'], e['grant_revision'], e['kind']), ('authorized', 1, 'appointment'))
        again = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK, 'step': 'T1/S3'})
        self.assertEqual((again['id'], again['state']), (e['id'], 'authorized'))      # same request_id + same content = the same effect
        with self.assertRaisesRegex(ValueError, 'different operation'):
            self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': {**BOOK, 'time': '15:00'}})
        d = self.h.call('health_effect_dispatch', {'effect': e['id']})
        self.assertEqual(d['state'], 'dispatching'); self.assertEqual(d['disclosures'][0], 'name')
        self.assertIn('Already dispatching', self.h.call('health_effect_dispatch', {'effect': e['id']})['next'])
        s = self.h.call('health_effect_settle', {'effect': e['id'], 'outcome': 'confirmed', 'source': 'clinic site confirmation page', 'external_id': 'CONF-88812'})
        self.assertEqual(s['state'], 'confirmed'); self.assertIsNotNone(s['confirmed_at']); self.assertIsNotNone(s['attempted_at'])
        with self.assertRaisesRegex(ValueError, 'already confirmed'):
            self.h.call('health_effect_settle', {'effect': e['id'], 'outcome': 'failed', 'source': 'x'})
        got = self.h.call('health_effect_get', {'effect': e['id']})
        self.assertEqual([x['state'] for x in got['events']], ['authorized', 'dispatching', 'confirmed'])

    def test_outside_the_grant_waits_for_the_owner_and_never_dispatches(self):
        g = self.grant()
        for bad, why in (({**BOOK, 'clinician': 'Dr. Chen'}, 'clinician'), ({**BOOK, 'weekday': 'Friday'}, 'approved day'), ({**BOOK, 'date': '2027-01-05'}, 'windows'),
                         ({**BOOK, 'cost_usd': 120}, 'cap'), ({k: v for k, v in BOOK.items() if k != 'cost_usd'}, 'cost is unknown'), ({**BOOK, 'location': 'Buckhead'}, 'location')):
            e = self.h.call('health_effect_begin', {'request_id': 'book-bad-' + why.replace(' ', '')[:8], 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': bad})
            self.assertEqual(e['state'], 'awaiting_owner', why); self.assertIn(why, e['note'])
            with self.assertRaisesRegex(ValueError, 'cannot be dispatched'):
                self.h.call('health_effect_dispatch', {'effect': e['id']})
        e = self.h.call('health_effect_begin', {'request_id': 'cancel-0001', 'action': 'appointment.cancel', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        self.assertEqual(e['state'], 'awaiting_owner'); self.assertIn('does not allow appointment.cancel', e['note'])

    def test_equivalent_operations_from_another_packet_or_channel_are_stopped(self):
        g = self.grant()
        a = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        b = self.h.call('health_effect_begin', {'request_id': 'book-derm-phone-0002', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'],
                                                 'payload': {**BOOK, 'time': '15:00', 'channel': 'phone'}})
        self.assertEqual((b['state'], b['duplicate_of']), ('awaiting_owner', a['id']))
        self.assertEqual(target_key('refill', {'prescription_id': 'RX-77', 'pharmacy': 'CVS #4426'}), target_key('refill', {'prescription_id': 'rx-77', 'pharmacy': 'cvs #4426', 'note': 'x'}))

    def test_revision_cancels_queued_work_and_revocation_stops_everything(self):
        g = self.grant()
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        r = self.h.call('health_grant_revise', {'grant': g['id'], 'actions': ['appointment.search', 'appointment.book'], 'constraints': {'clinician': 'Dr. Rivera', 'spend_cap_usd': 0}}, True)
        self.assertEqual((r['revision'], r['cancelled_queued']), (2, 1))
        self.assertEqual(self.h.call('health_effect_get', {'effect': e['id']})['state'], 'cancelled')
        e2 = self.h.call('health_effect_begin', {'request_id': 'book-derm-0002', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        self.assertEqual(e2['grant_revision'], 2)
        self.h.call('health_effect_dispatch', {'effect': e2['id']})
        rv = self.h.call('health_grant_revoke', {'grant': g['id'], 'why': 'owner'}, True)
        self.assertEqual(rv['status'], 'revoked')
        self.assertEqual(self.h.call('health_effect_get', {'effect': e2['id']})['state'], 'uncertain')   # already sent: reconcile, do not cancel blindly
        e3 = self.h.call('health_effect_begin', {'request_id': 'book-derm-0003', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        self.assertEqual(e3['state'], 'awaiting_owner'); self.assertIn('revoked', e3['note'])
        # The grant survives a restart (new Health over the same files).
        h2 = Health(self.p, self.tmp.name, now=lambda: self.clock[0])
        self.assertEqual(h2.call('health_status', {})['grants'], [])
        self.assertEqual(h2.call('health_effect_get', {'effect': e2['id']})['state'], 'uncertain')

    def test_uncertain_results_are_read_back_never_retried(self):
        g = self.grant()
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        self.h.call('health_effect_dispatch', {'effect': e['id']})
        s = self.h.call('health_effect_settle', {'effect': e['id'], 'outcome': 'uncertain', 'source': 'browser', 'detail': 'page timed out after the press'})
        self.assertEqual(s['state'], 'uncertain'); self.assertIn('read the provider', s['next'])
        with self.assertRaisesRegex(ValueError, 'reconcile it first'):
            e2 = self.h.call('health_effect_begin', {'request_id': 'book-derm-0002', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': {**BOOK, 'time': '15:00'}})
            self.assertEqual(e2['state'], 'awaiting_owner')      # equivalent of an uncertain one
            self.h.call('health_effect_dispatch', {'effect': e['id']})
        r = self.h.call('health_effect_reconcile', {'effect': e['id'], 'found': 'confirmed', 'source': 'portal appointments list', 'external_id': 'CONF-1'})
        self.assertEqual((r['state'], r['external_id']), ('confirmed', 'CONF-1'))
        # A request the provider merely accepted is not a confirmation.
        e3 = self.h.call('health_effect_begin', {'request_id': 'refill-0001', 'action': 'rx.refill', 'connection': self.conn['id'], 'grant': g['id'], 'payload': {'prescription_id': 'RX-1'}})
        self.assertEqual(e3['state'], 'awaiting_owner')         # this grant has no rx.refill
        e4 = self.h.call('health_effect_begin', {'request_id': 'book-derm-0009', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': {**BOOK, 'date': '2026-11-25'}})
        self.h.call('health_effect_dispatch', {'effect': e4['id']})
        s4 = self.h.call('health_effect_settle', {'effect': e4['id'], 'outcome': 'requested', 'source': 'phone transcript', 'detail': 'receptionist said she put in the request'})
        self.assertEqual(s4['state'], 'uncertain'); self.assertIn('requested', s4['provider_state'])
        a = self.h.call('health_effect_reconcile', {'effect': e4['id'], 'found': 'absent', 'source': 'portal'})
        self.assertEqual(a['state'], 'failed'); self.assertIn('NEW request_id', a['next'])

    def test_an_expired_lease_is_uncertain_not_failed(self):
        g = self.grant()
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        self.h.call('health_effect_dispatch', {'effect': e['id']})
        self.tick(601)
        self.assertEqual(self.h.call('health_effect_get', {'effect': e['id']})['state'], 'uncertain')

    def test_expired_grant_and_dispatch_recheck(self):
        g = self.grant(valid_days=1)
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        self.tick(2 * 86400)
        with self.assertRaisesRegex(ValueError, 'validity'):
            self.h.call('health_effect_dispatch', {'effect': e['id']})

    def test_receipts_are_private_single_links_and_policy_defaults_keep_health_out_of_recall(self):
        self.assertEqual(self.h.call('health_policy', {})['recall'], 'off')
        self.assertEqual(self.h.call('health_policy', {})['notify_detail'], 'minimal')
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_policy', {'set': {'recall': 'on'}})
        g = self.grant()
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': g['id'], 'payload': BOOK})
        r = self.h.call('health_receipt', {'effect': e['id'], 'body': 'Dermatology follow-up, Dr. Rivera, Tue Nov 18 2:30 PM, Midtown clinic. Confirmation CONF-1.'})
        self.assertNotIn('CONF-1', json.dumps(self.h.call('health_status', {})))         # receipt details are not in the status the model sees
        self.assertEqual(self.h.call('health_receipt_read', {'token': r['token']}, True)['body'][:11], 'Dermatology')
        self.assertIn('error', self.h.call('health_receipt_read', {'token': 'nope'}, True))
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.h.call('health_receipt', {'effect': e['id'], 'body': 'card 4111 1111 1111 1111'})
        self.assertEqual(payload_hash({'a': 1, 'b': [2]}), payload_hash({'b': [2], 'a': 1}))


if __name__ == '__main__':
    unittest.main()
