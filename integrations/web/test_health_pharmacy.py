"""Pharmacy path against the synthetic pharmacy (web 0.23.0; build-plans/health/04 acceptance tests). Loopback only."""
import json
import re
import urllib.request

from health_pharmacy import Pharmacy
from test_health_portal import Base

CALLBACK = 'https://assistant.example.com/assistant/dashboard/health/rx-callback'


class Rx(Base):
    def setUp(self):
        super().setUp()
        self.ehr.rx.clock = lambda: self.clock[0]
        self.ph = self.h.attach('health_pharmacy_', Pharmacy(self.h, self.p, CALLBACK))

    def pharmacy(self, user='owner'):
        c = self.h.call('health_connection_add', {'provider': 'mock_pharmacy', 'institution': 'Cornerstone Pharmacy (synthetic)', 'environment': 'fake',
                                                  'endpoint': self.ehr.base('corner')}, True)
        a = self.h.call('health_portal_authorize', {'connection': c['id']}, True)
        self.assertIn('rx%2Fprescriptions.read', a['authorize_url'])
        q = self.browser(a['authorize_url'], user, 'corner-test')
        out = self.h.call('health_portal_callback', {'state': q['state'], 'code': q['code']}, True)
        self.assertEqual(out.get('status'), 'connected', out)
        self.assertEqual(out['capabilities']['rx.refill'], 'supported')
        return c

    def rxgrant(self, c, rx=('rx-1001',), **k):
        p = self.h.call('health_grant_propose', {'connection': c['id'], 'title': 'Refills', 'actions': ['rx.status', 'rx.refill'],
                                                  'constraints': {'prescriptions': list(rx), 'pharmacy': 'st-100', 'fulfillment': ['pickup'], 'spend_cap_usd': 10, **k}})
        return self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)

    def begin(self, c, g, rid='rx-refill-0001', **p):
        payload = {'prescription_id': 'rx-1001', 'rx_number': 'RX7001', 'pharmacy': 'st-100', 'fulfillment': 'pickup', 'cost_usd': 0.0, **p}
        return self.h.call('health_effect_begin', {'request_id': rid, 'action': 'rx.refill', 'connection': c['id'], 'grant': g['id'], 'payload': payload})

    def refills(self):
        return list(self.ehr.inst['corner']['refills'].values())

    def advance(self, refill, status, deliver=False):
        req = urllib.request.Request(self.ehr.public + '/_admin/corner/advance', data=json.dumps({'refill': refill, 'status': status, 'deliver': deliver}).encode(),
                                     headers={'X-Admin-Token': self.ehr.admin_token}, method='POST')
        return json.loads(urllib.request.urlopen(req).read())

    def hook(self, c, ev):
        return self.h.call('health_pharmacy_webhook', {'connection': c['id'], 'headers': {'X-Rx-Signature': ev['signature']}, 'body': ev['body']}, True)


class ReadTests(Rx):
    def test_listing_keeps_same_name_prescriptions_apart(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        items = self.h.call('health_pharmacy_list', {'connection': c['id'], 'grant': g['id']})['prescriptions']
        ator = [(i['prescription_id'], i['medication'], i['store_id']) for i in items if i['medication'].startswith('Atorvastatin')]
        self.assertEqual(ator, [('rx-1001', 'Atorvastatin 20 mg tablet', 'st-100'), ('rx-1002', 'Atorvastatin 40 mg tablet', 'st-100'), ('rx-1005', 'Atorvastatin 20 mg tablet', 'st-200')])
        self.assertNotIn('RX8001', json.dumps(items))        # another person's prescription

    def test_reads_need_rx_status(self):
        c = self.pharmacy()
        p = self.h.call('health_grant_propose', {'connection': c['id'], 'title': 'x', 'actions': ['rx.refill'], 'constraints': {'prescriptions': ['rx-1001']}})
        g = self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)
        with self.assertRaisesRegex(ValueError, 'does not allow rx.status'):
            self.h.call('health_pharmacy_list', {'connection': c['id'], 'grant': g['id']})

    def test_another_patients_record_drops_the_answer(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        self.fault('corner', wrong_patient=True)
        out = self.h.call('health_pharmacy_list', {'connection': c['id'], 'grant': g['id']})
        self.assertEqual(out['error'], 'identity_mismatch'); self.assertNotIn('prescriptions', out)

    def test_eligibility_states(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        el = lambda pid, num, st='st-100': self.h.call('health_pharmacy_eligibility', {'connection': c['id'], 'grant': g['id'], 'prescription_id': pid, 'rx_number': num, 'store_id': st})
        ok = el('rx-1001', 'RX7001')
        self.assertEqual((ok['eligible'], ok['cost_usd']), (True, 0.0))
        soon = el('rx-1002', 'RX7002')
        self.assertEqual(soon['reason'], 'too_soon'); self.assertIn(soon['earliest_date'], soon['next'])
        self.assertIn('RENEWAL', el('rx-1003', 'RX7003')['next'])
        self.assertEqual(el('rx-1004', 'RX7004')['reason'], 'expired')
        self.assertIn('unknown', el('rx-1006', 'RX7006', 'st-200')['next'])
        self.assertEqual(el('rx-1001', 'RX7005')['error'], 'mismatch')            # right id, wrong number
        self.assertEqual(el('rx-1001', 'RX7001', 'st-200')['error'], 'mismatch')  # right id, wrong store


class RefillTests(Rx):
    def test_a_refill_is_requested_then_ready_by_signed_webhook(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g)
        self.assertEqual(e['state'], 'authorized', e)
        out = self.h.call('health_pharmacy_refill', {'effect': e['id']})
        self.assertTrue(out['requested']); self.assertEqual(out['effect']['state'], 'uncertain'); self.assertTrue(out['effect']['provider_state'].startswith('requested'))
        self.assertEqual(len(self.refills()), 1); self.assertEqual(self.refills()[0]['idempotency_key'], 'rx-refill-0001')
        rf = out['refill']['id']
        ev = self.advance(rf, 'processing')
        self.assertEqual(self.hook(c, ev)['outcome'], 'applied')
        self.assertEqual(self.h.public_effect_by_id(e['id'])['state'], 'uncertain')
        ready = self.advance(rf, 'ready')
        r = self.hook(c, ready)
        self.assertEqual((r['state'], r['outcome']), ('confirmed', 'applied')); self.assertIn('pharmacy request', r['notify'])
        self.assertNotIn('Atorvastatin', r['notify'])
        self.assertEqual(self.hook(c, ready)['duplicate'], True)                        # replay
        self.assertEqual(self.hook(c, ev)['duplicate'], True)
        done = self.advance(rf, 'picked_up')
        self.assertEqual(self.hook(c, done)['state'], 'confirmed')
        self.assertTrue(self.h.public_effect_by_id(e['id'])['provider_state'].startswith('picked_up'))
        nxt = self.begin(c, g, rid='rx-refill-dec-0001')       # next month's refill is a new request, not a duplicate
        self.assertEqual(nxt['state'], 'authorized', nxt)

    def test_out_of_order_and_forged_webhooks(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        rf = self.h.call('health_pharmacy_refill', {'effect': self.begin(c, g)['id']})['refill']['id']
        older = self.advance(rf, 'processing'); newer = self.advance(rf, 'ready')
        self.assertEqual(self.hook(c, newer)['state'], 'confirmed')
        self.assertEqual(self.hook(c, older)['outcome'], 'stale')
        self.assertTrue(self.h.public_effect_by_id(self.refill_effect(rf))['provider_state'].startswith('ready'))
        forged = dict(newer, body=newer['body'].replace('"ready"', '"rejected"').replace(newer['event']['event_id'], 'ev-forged01'))
        self.assertEqual(self.hook(c, forged)['http_status'], 401)
        late = self.advance(rf, 'delivered')
        self.clock[0] += 3600
        self.assertEqual(self.hook(c, late)['error'], 'stale_signature')

    def refill_effect(self, rf):
        with self.h.db() as db:
            return db.execute('SELECT effect_id FROM rx_refills WHERE refill_id=?', (rf,)).fetchone()['effect_id']

    def test_a_lost_answer_after_the_pharmacy_recorded_it_is_read_back(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g)
        self.fault('corner', refill_commit_then_504=1)
        out = self.h.call('health_pharmacy_refill', {'effect': e['id']})
        self.assertTrue(out['reconciled']); self.assertEqual(out['refill']['status'], 'received')
        self.assertEqual(out['effect']['state'], 'uncertain'); self.assertEqual(len(self.refills()), 1)
        # A second packet for the same prescription is stopped before the pharmacy.
        e2 = self.begin(c, g, rid='rx-refill-phone-02', channel='phone')
        self.assertEqual(e2['state'], 'awaiting_owner')
        self.assertEqual(len(self.refills()), 1)

    def test_same_request_twice_is_one_refill(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g)
        self.h.call('health_pharmacy_refill', {'effect': e['id']})
        self.assertEqual(self.begin(c, g)['id'], e['id'])
        self.assertEqual(self.h.call('health_pharmacy_refill', {'effect': e['id']})['error'], 'bad_state')
        self.assertEqual(len(self.refills()), 1)

    def test_a_refill_made_outside_procurator_holds_for_the_owner(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        self.ehr.inst['corner']['refills']['rf-1'] = {'id': 'rf-1', 'prescription_id': 'rx-1001', 'patient': 'c1', 'status': 'processing', 'idempotency_key': 'other-app-0001', 'version': 1}
        out = self.h.call('health_pharmacy_refill', {'effect': self.begin(c, g)['id']})
        self.assertEqual((out['requested'], out['effect']['state']), (False, 'awaiting_owner'))

    def test_ineligible_and_outside_the_grant_never_reach_the_pharmacy(self):
        c = self.pharmacy(); g = self.rxgrant(c, rx=('rx-1001', 'rx-1002', 'rx-1003'))
        soon = self.h.call('health_pharmacy_refill', {'effect': self.begin(c, g, rid='rx-refill-soon1', prescription_id='rx-1002', rx_number='RX7002')['id']})
        self.assertEqual((soon['requested'], soon['reason'], soon['effect']['state']), (False, 'too_soon', 'failed'))
        ren = self.h.call('health_pharmacy_refill', {'effect': self.begin(c, g, rid='rx-refill-renew1', prescription_id='rx-1003', rx_number='RX7003')['id']})
        self.assertEqual(ren['reason'], 'renewal_required')
        other = self.begin(c, g, rid='rx-refill-other1', prescription_id='rx-1005', rx_number='RX7005', pharmacy='st-200')
        self.assertEqual(other['state'], 'awaiting_owner')                   # not a named prescription; not the approved store
        deliv = self.begin(c, g, rid='rx-refill-deliv1', fulfillment='delivery')
        self.assertEqual(deliv['state'], 'awaiting_owner'); self.assertIn('approved way', deliv['note'])
        unknown_cost = self.begin(c, g, rid='rx-refill-cost01', cost_usd=None)
        self.assertEqual(unknown_cost['state'], 'awaiting_owner')
        self.assertEqual(self.refills(), [])

    def test_a_number_that_no_longer_matches_is_not_substituted(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g, rx_number='RX7005')
        out = self.h.call('health_pharmacy_refill', {'effect': e['id']})
        self.assertEqual(out['error'], 'mismatch'); self.assertEqual(self.h.public_effect_by_id(e['id'])['state'], 'failed')
        self.assertEqual(self.refills(), [])

    def test_a_revoked_grant_stops_requests_and_polling(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g)
        out = self.h.call('health_pharmacy_refill', {'effect': e['id']})
        self.h.call('health_grant_revoke', {'grant': g['id']}, True)
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.h.call('health_pharmacy_refill_status', {'effect': e['id']})

    def test_polling_is_bounded_and_a_failed_read_changes_nothing(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g)
        rf = self.h.call('health_pharmacy_refill', {'effect': e['id']})['refill']['id']
        self.advance(rf, 'ready')
        self.fault('corner', outage=True)
        bad = self.h.call('health_pharmacy_refill_status', {'effect': e['id']})
        self.assertEqual(bad['read_failed'], 'outage'); self.assertEqual(bad['effect']['state'], 'uncertain')
        self.ehr.faults['corner'] = {}
        self.assertIn('less than 10 minutes', self.h.call('health_pharmacy_refill_status', {'effect': e['id']})['note'])
        self.clock[0] += 601
        ok = self.h.call('health_pharmacy_refill_status', {'effect': e['id']})
        self.assertEqual((ok['changed'], ok['effect']['state']), (True, 'confirmed'))

    def test_rejection_and_out_of_stock_fail_with_the_reason(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        e = self.begin(c, g)
        rf = self.h.call('health_pharmacy_refill', {'effect': e['id']})['refill']['id']
        r = self.hook(c, self.advance(rf, 'out_of_stock'))
        self.assertEqual(r['state'], 'failed')
        self.assertIn('Out of stock', self.h.public_effect_by_id(e['id'])['note'])

    def test_notifications_and_results_keep_secrets_out(self):
        c = self.pharmacy(); g = self.rxgrant(c)
        self.h.call('health_pharmacy_refill', {'effect': self.begin(c, g)['id']})
        with self.h.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rx_webhooks').fetchone()[0], 1)
        secret = self.ehr.inst['corner']['webhooks']['c1']['secret']
        self.assertNotIn(secret, json.dumps(self.h.call('health_status', {})))
        self.assertTrue(re.match(CALLBACK + '/[a-f0-9]{16}$', self.ehr.inst['corner']['webhooks']['c1']['url']))


if __name__ == '__main__':
    import unittest
    unittest.main()
