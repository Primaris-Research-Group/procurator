"""Nov 5: per-site routing (never / read-only / owner presses the final button / normal)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import site_policy
from broker import Broker
from test_broker import Provider, control
from test_tasks import CONTEXT


class PolicyTable(unittest.TestCase):
    def test_lookup_and_overrides(self):
        self.assertEqual(site_policy.policy('www.wellsfargo.com')['mode'], 'never')
        self.assertEqual(site_policy.policy('secure.chase.com')['mode'], 'never')
        self.assertEqual(site_policy.policy('www.amazon.com')['mode'], 'read_only')
        self.assertEqual(site_policy.policy('www.delta.com')['mode'], 'read_only')
        self.assertEqual(site_policy.policy('www.walmart.com')['mode'], 'owner_final')
        self.assertEqual(site_policy.policy('www.cvs.com')['mode'], 'browser')
        self.assertEqual(site_policy.policy('notamazon.com')['mode'], 'browser', 'suffix match only on a dot boundary')
        d, rules = site_policy.set_override({}, 'https://www.Amazon.com/dp/x', 'browser')
        self.assertEqual((d, rules), ('amazon.com', {'amazon.com': 'browser'}))
        self.assertEqual(site_policy.policy('www.amazon.com', rules)['mode'], 'browser')
        with self.assertRaisesRegex(ValueError, 'fixed list'):
            site_policy.set_override({}, 'wellsfargo.com', 'browser')
        with self.assertRaises(ValueError):
            site_policy.set_override({}, 'amazon.com', 'yolo')
        self.assertEqual(site_policy.set_override(rules, 'amazon.com', 'default')[1], {})


class BrokerRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Provider(); self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', self.p)

    def session_on(self, origin, page):
        s = self.b.call('open', {'request_id': 'site-open-' + origin[-8:], 'url': 'practice'})
        with self.b.db() as db:
            db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (origin, page, s['id']))
        self.p.snap['url'] = page
        return s

    def test_never_sites_do_not_open(self):
        with patch('broker.url', return_value=('https://www.wellsfargo.com/', 'https://www.wellsfargo.com')):
            with self.assertRaisesRegex(ValueError, 'Not opened — wellsfargo.com'):
                self.b.call('open', {'request_id': 'wf-open-01', 'url': 'https://www.wellsfargo.com/'})
        self.assertEqual(self.p.created, 0)

    def test_read_only_allows_links_not_typing_or_buttons(self):
        s = self.session_on('https://www.amazon.com', 'https://www.amazon.com/s?k=desk')
        link = control('e5', 'link', 'Standing desk') | {'tag': 'a', 'href': 'https://www.amazon.com/dp/B0TEST', 'form_action': '', 'type': ''}
        self.p.snap['controls'] = [control('e1', 'text', 'Search Amazon'), control('e2', 'submit', 'Add to Cart'), link]
        for steps in ([{'kind': 'fill', 'ref': 'e1', 'value': 'desk'}], [{'kind': 'click', 'ref': 'e2'}]):
            with self.assertRaisesRegex(ValueError, 'read-only'):
                self.b.call('prepare', {'id': s['id'], 'request_id': 'readonly-' + steps[0]['ref'], 'steps': steps})
        with patch('broker.url', return_value=('https://www.amazon.com/dp/B0TEST', 'https://www.amazon.com')):
            self.assertTrue(self.b.call('prepare', {'id': s['id'], 'request_id': 'ro-link-1', 'steps': [{'kind': 'click', 'ref': 'e5'}]})['id'])

    def test_owner_final_sites_never_press_buy(self):
        s = self.session_on('https://www.walmart.com', 'https://www.walmart.com/checkout')
        self.p.snap.update({'text': 'Review your order\nTotal $42.10\nVisa ending in 4242',
                            'controls': [control('e9', 'submit', 'Place order') | {'tag': 'button', 'name': '', 'id': '', 'form_action': ''}]})
        with self.assertRaisesRegex(ValueError, 'presses the final order button themselves'):
            self.b.call('prepare', {'id': s['id'], 'request_id': 'wm-buy-01', 'steps': [{'kind': 'click', 'ref': 'e9'}]})
        self.assertEqual(self.p.acts, 0)

    def test_owner_sets_rules_and_reads_them(self):
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('webpolicy_set', {'domain': 'amazon.com', 'mode': 'browser'})
        out = self.b.call('webpolicy_set', {'domain': 'amazon.com', 'mode': 'browser'}, True)
        self.assertEqual((out['mode'], out['source']), ('browser', 'owner'))
        self.assertEqual(self.b.call('webpolicy', {'domain': 'smile.amazon.com'})['mode'], 'browser')
        with self.assertRaisesRegex(ValueError, 'fixed list'):
            self.b.call('webpolicy_set', {'domain': 'chase.com', 'mode': 'browser'}, True)
        self.assertIn('wellsfargo.com', self.b.call('webpolicy', {})['fixed_never'])


class PageTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Provider(); self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', self.p)
        self.s = self.b.call('open', {'request_id': 'shop-open-01', 'url': 'practice'})
        calls = self.calls = []
        real = self.p.run
        def run(s, op, **kw):
            if op == 'page_tool':
                calls.append(kw); return {'ok': True, 'result': '{"products":[{"title":"Mug"}]}', 'url': 'https://shop.example/'}
            return real(s, op, **kw)
        self.p.run = run

    def test_only_reading_tools_run(self):
        out = self.b.call('page_tool', {'id': self.s['id'], 'tool': 'search_catalog', 'arguments': {'query': 'mug'}})
        self.assertIn('Mug', out['result']); self.assertIn('untrusted', out['note'])
        self.assertEqual(self.calls, [{'tool': 'search_catalog', 'tool_args': {'query': 'mug'}}])
        for name in ('complete_checkout', 'add_to_cart', 'proceed_to_checkout', 'manage_orders', 'get_cart_and_pay', 'update_cart_lines'):
            with self.assertRaisesRegex(ValueError, 'Only reading tools'):
                self.b.call('page_tool', {'id': self.s['id'], 'tool': name})
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()


class ClinicalSitesTest(unittest.TestCase):
    def test_openevidence_and_mdcalc_are_never_browsed_but_owner_can_change(self):
        for host in ('www.openevidence.com', 'openevidence.com', 'www.mdcalc.com'):
            p = site_policy.policy(host)
            self.assertEqual(p['mode'], 'never', host)
        self.assertIn('evidence_search', site_policy.policy('www.openevidence.com')['reason'])
        _, o = site_policy.set_override({}, 'mdcalc.com', 'read_only')
        self.assertEqual(site_policy.policy('www.mdcalc.com', o)['mode'], 'read_only')

class PathRuleTests(unittest.TestCase):
    """0.20.7 (W12): Google Flights/Hotels/Maps open; Google search never does."""
    def test_google_travel_opens_and_search_does_not(self):
        from site_policy import policy
        self.assertEqual(policy('www.google.com', {}, '/travel/flights')['mode'], 'browser')
        self.assertEqual(policy('www.google.com', {}, '/travel/hotels?q=x')['mode'], 'browser')
        self.assertEqual(policy('www.google.com', {}, '/maps/place/x')['mode'], 'browser')
        self.assertEqual(policy('www.google.com', {}, '/search')['mode'], 'never')
        self.assertEqual(policy('www.google.com', {})['mode'], 'never')
        self.assertEqual(policy('chase.com', {}, '/travel/')['mode'], 'never')      # path rules never relax the fixed finance list

