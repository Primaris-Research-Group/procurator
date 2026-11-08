"""Purchases on the owner's own store accounts (Nov 4): one approval with total + card last 4, per-purchase cap."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from broker import Broker
from task_policy import account_protected, purchase_details, autonomous_decision
from test_broker import Provider, control
from test_tasks import CONTEXT

HOTELS = 'https://www.hotels.com'
CHECKOUT = HOTELS + '/Checkout?tripid=abc'
PAGE = '\n'.join(['Review and book', 'Hotel Arya, Coconut Grove', 'Check-in', 'Fri, Nov 13', 'Check-out', 'Sun, Nov 15', '2 nights, 1 room, 2 adults',
                  'Fully refundable before Nov 12', 'Taxes and fees $41.20', 'Total $289.40', 'Pay with Visa ending in 4242', 'Phone number'])


def button(ref, label, kind='submit'):
    return control(ref, kind, label) | {'tag': 'button', 'name': '', 'id': '', 'form_action': ''}


class PolicyTests(unittest.TestCase):
    def test_checkout_fields_are_ordinary_account_settings_are_not(self):
        self.assertFalse(account_protected('Phone number', CHECKOUT))
        self.assertFalse(account_protected('Payment method', 'https://www.amazon.com/gp/buy/spc/handlers/display.html'))
        self.assertTrue(account_protected('Phone number', HOTELS + '/profile/personal-info'))
        self.assertTrue(account_protected('Add a card', 'https://www.amazon.com/cpe/yourpayments/wallet'))
        self.assertTrue(account_protected('Save', 'https://www.amazon.com/a/addresses/edit'))
        self.assertTrue(account_protected('Password', CHECKOUT))                       # credentials: never
        self.assertTrue(account_protected('Direct deposit', 'https://portal.leaveco.example/leave'))
        self.assertTrue(account_protected('Phone number', 'https://portal.leaveco.example/leave/contact'))  # not a checkout
        self.assertFalse(account_protected('Report time', 'https://portal.leaveco.example/leave/report'))
    def test_purchase_details_reads_card_dates_room_and_total(self):
        d = purchase_details(PAGE)
        self.assertEqual(d['card_last4'], ['4242'])
        self.assertIn('Check-in Fri, Nov 13', d['details']); self.assertIn('Total $289.40', d['details'])
        self.assertIn('Fully refundable before Nov 12', d['details'])
    def test_autonomous_signed_in_checkout_fill_is_routine_final_comes_back(self):
        spec = {'mode': 'autonomous', 'site_login': 'hotels', 'facts': ['3055551234'], 'fields': [], 'options': [], 'spend_limit_usd': 400}
        fill = {'url': CHECKOUT, 'steps': [{'kind': 'fill', 'control': control('e2', 'tel', 'Phone number'), 'value': '3055551234'}], 'detected_total': 289.4}
        self.assertEqual(autonomous_decision(spec, fill), (True, 'routine'))
        book = {'url': CHECKOUT, 'steps': [{'kind': 'click', 'control': button('e9', 'Complete booking')}], 'detected_total': 289.4, 'page_errors': []}
        self.assertEqual(autonomous_decision(spec, book), (False, 'final'))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Provider(); self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', self.p)
        self.s = self.b.call('open', {'request_id': 'hotels-open-1', 'url': 'practice'})
        with self.b.db() as db:
            db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (HOTELS, CHECKOUT, self.s['id']))
            db.execute("INSERT INTO site_logins(name,start_url,origin,context_id,status,updated,session_id,sequence) VALUES('hotels','https://www.hotels.com/',?,'ctx-1','owner_connected',0,NULL,0)", (HOTELS,))
            db.execute('INSERT INTO site_sessions VALUES(?,?)', (self.s['id'], 'hotels'))
        self.p.snap.update({'url': CHECKOUT, 'title': 'Hotels.com checkout', 'text': PAGE,
                            'controls': [control('e2', 'tel', 'Phone number'), button('e8', 'Continue', 'button'), button('e9', 'Complete booking')]})
    def prepare(self, rid, ref):
        return self.b.call('prepare', {'id': self.s['id'], 'request_id': rid, 'steps': [{'kind': 'click', 'ref': ref}]})
    def test_book_click_gets_one_purchase_approval_with_total_card_and_cap(self):
        a = self.prepare('buy-hotel-1', 'e9'); plan = json.loads(self.b.plan(a['id'])['plan'])
        pu = plan['purchase']
        self.assertEqual((pu['site'], pu['signed_in'], pu['total'], pu['card_last4'], pu['cap']), ('hotels.com', True, 289.4, ['4242'], 500.0))
        self.b.call('review', {'id': a['id']}); msg = self.p.messages[-1]
        for want in ['PURCHASE APPROVAL — ', 'PURCHASE on hotels.com (signed in as you)', 'Total on page: $289.40', 'Your per-purchase cap: $500.00',
                     'Card: ending 4242', 'Check-out Sun, Nov 15', '/webapprove ' + a['id'], '/webtakeover ' + self.s['id']]:
            self.assertIn(want, msg)
        self.assertNotIn('purchase', json.loads(self.b.plan(self.prepare('continue-1', 'e8')['id'])['plan']))  # plain Continue
    def test_cap_blocks_over_limit_orders_and_only_owner_changes_it(self):
        self.p.snap['text'] = PAGE.replace('Total $289.40', 'Total $1,289.40')
        with self.assertRaisesRegex(ValueError, r'\$1289\.40, over the owner.s \$500\.00 per-purchase cap'):
            self.prepare('buy-hotel-2', 'e9')
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('purchase_cap', {'amount': 2000})
        for bad in [0, 20000, '500', True]:
            with self.assertRaises(ValueError): self.b.call('purchase_cap', {'amount': bad}, True)
        self.assertEqual(self.b.call('purchase_cap', {'amount': 1500}, True)['purchase_cap_usd'], 1500.0)
        self.assertEqual(self.b.call('purchase_cap', {})['purchase_cap_usd'], 1500.0)
        self.assertTrue(self.prepare('buy-hotel-3', 'e9')['id'])
    def test_lowering_the_cap_after_review_stops_the_approval(self):
        a = self.prepare('buy-hotel-4', 'e9'); self.b.call('review', {'id': a['id']})
        self.b.call('purchase_cap', {'amount': 100}, True)
        p = self.b.plan(a['id'])
        with self.assertRaisesRegex(ValueError, 'now over your \\$100.00'):
            self.b.call('execute', {'id': a['id'], 'hash': p['hash'][:16], 'context': CONTEXT}, True)
        self.assertEqual(self.p.acts, 0)

    def test_purchase_card_rule_refuses_other_or_unknown_cards(self):
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('purchase_cards', {'cards': ['1111']})
        for bad in [['12345'], ['abcd'], '4242', ['1', '2', '3', '4', '5', '6']]:
            with self.assertRaises(ValueError): self.b.call('purchase_cards', {'cards': bad}, True)
        self.b.call('purchase_cards', {'cards': ['1111']}, True)
        with self.assertRaisesRegex(ValueError, 'card ending 4242, not one of your purchase cards \\(1111\\)'):
            self.prepare('buy-card-1', 'e9')
        self.p.snap['text'] = PAGE.replace('Pay with Visa ending in 4242', 'Pay with saved card')
        with self.assertRaisesRegex(ValueError, 'does not show which card'):
            self.prepare('buy-card-2', 'e9')
        self.p.snap['text'] = PAGE
        self.b.call('purchase_cards', {'cards': ['4242', '1111']}, True)
        a = self.prepare('buy-card-3', 'e9'); self.b.call('review', {'id': a['id']})
        self.assertIn('Card: ending 4242 ✓ your purchase card', self.p.messages[-1])
        self.b.call('purchase_cards', {'cards': ['1111']}, True)          # rule tightened after the review
        p = self.b.plan(a['id'])
        with self.assertRaisesRegex(ValueError, 'purchase-card rule changed'):
            self.b.call('execute', {'id': a['id'], 'hash': p['hash'][:16], 'context': CONTEXT}, True)
        self.assertEqual(self.p.acts, 0)
        self.b.call('purchase_cards', {'cards': []}, True)
        self.assertTrue(self.prepare('buy-card-4', 'e9')['id'], 'rule off: any card, cap still applies')


if __name__ == '__main__':
    unittest.main()
