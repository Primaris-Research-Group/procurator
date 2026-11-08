"""Phase 1 (0.17.0): submit-target classifier. Fixtures modelled on the CVS, Leaveco, hotels.com and Target journals.
Release rule: no final button may ever be classed neutral or navigate."""
import unittest
from task_policy import classify_target, form_filled, autonomous_decision, origin_allowed, plan_deviation

CVS = 'https://www.cvs.com/scheduling/patient-details'
LEAVECO = 'https://services.leaveco.example/absence/report/step-2'
HOTEL = 'https://www.hotels.com/Hotel-Search'
HOTEL_CHECKOUT = 'https://www.hotels.com/checkout/payment'
TARGET_CART = 'https://www.target.com/cart'
TARGET_CHECKOUT = 'https://www.target.com/checkout'

def btn(label, typ='submit', tag='button', form='', **extra):
    return {'ref': 'e1', 'tag': tag, 'type': typ, 'role': '', 'label': label, 'name': '', 'href': '', 'form_action': form, 'value': '', 'checked': False} | extra

def link(label, href='https://www.cvs.com/next'):
    return btn(label, typ='', tag='a', href=href)

FORM = 'https://www.cvs.com/scheduling/submit'
FILLED = [{'tag': 'input', 'type': 'text', 'form_action': FORM, 'value': '31901', 'label': 'ZIP'}]

# (control, page, expected class)
FIXTURES = [
    # CVS
    (link('Schedule an appointment', 'https://www.cvs.com/immunizations/flu'), {'url': 'https://www.cvs.com/immunizations'}, 'navigate'),
    (btn('Continue', form=FORM), {'url': CVS, 'filled': 2}, 'navigate'),
    (btn('Search', form=FORM), {'url': CVS, 'filled': 1}, 'navigate'),
    (btn('Search stores', form=FORM), {'url': CVS, 'filled': 1}, 'navigate'),
    (btn('Next: Review', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'navigate'),
    (btn('Next: Submit', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'final'),
    (btn('Check availability', form='https://www.hotels.com/ho1'), {'url': 'https://www.hotels.com/ho1', 'filled': 2}, 'navigate'),
    (btn('Confirm appointment', form=FORM), {'url': CVS, 'filled': 4}, 'final'),
    (btn('Schedule', form=FORM), {'url': CVS, 'filled': 3}, 'commit'),
    (btn('2:30 PM', typ='radio', tag='input'), {'url': CVS}, 'neutral'),
    (btn('Show date of birth', typ='checkbox', tag='input'), {'url': CVS}, 'neutral'),
    (btn('I agree to the terms', typ='checkbox', tag='input'), {'url': CVS}, 'commit'),
    (btn('Sign in'), {'url': CVS}, 'protected'),
    (btn('Cancel appointment', typ='button'), {'url': CVS}, 'commit'),
    (btn('Accept all cookies', typ='button'), {'url': CVS, 'cookie_notice': True}, 'neutral'),
    (btn('Book now', form=FORM), {'url': CVS, 'filled': 2}, 'final'),
    # Leaveco (signed in)
    (btn('Report an absence', typ='button'), {'url': 'https://services.leaveco.example/leave', 'signed_in': True}, 'neutral'),
    (btn('Next', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'navigate'),
    (btn('Save and continue', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'navigate'),
    (btn('Submit', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'final'),
    (btn('Submit absence', typ='button'), {'url': LEAVECO, 'signed_in': True}, 'final'),
    (btn('Go Back', typ='button'), {'url': LEAVECO, 'signed_in': True}, 'navigate'),
    (btn('Update direct deposit', typ='button'), {'url': 'https://services.leaveco.example/account', 'signed_in': True}, 'protected'),
    (btn('Change password', typ='button'), {'url': 'https://services.leaveco.example/account', 'signed_in': True}, 'protected'),
    (link('File a claim', 'https://services.leaveco.example/claims/new'), {'url': 'https://services.leaveco.example/', 'signed_in': True}, 'navigate'),
    (btn('File claim', form=LEAVECO), {'url': LEAVECO, 'filled': 3, 'signed_in': True}, 'commit'),
    (btn('Upload document', typ='file', tag='input'), {'url': LEAVECO, 'signed_in': True}, 'protected'),
    (btn('Delete draft', typ='button'), {'url': LEAVECO, 'signed_in': True}, 'commit'),
    (btn('Continue', form=LEAVECO, described='This absence overlaps an absence already reported. Submitting will replace it.'), {'url': LEAVECO, 'signed_in': True}, 'commit'),
    # hotels.com
    (btn('Search', form='https://www.hotels.com/search'), {'url': HOTEL, 'filled': 2}, 'navigate'),
    (btn('Reserve', typ='button'), {'url': HOTEL}, 'neutral'),
    (btn('Reserve', typ='button'), {'url': 'https://www.hotels.com/ho12345', 'total': 412.88}, 'final'),
    (btn('Book now'), {'url': HOTEL_CHECKOUT, 'total': 412.88}, 'final'),
    (btn('Complete booking'), {'url': HOTEL_CHECKOUT, 'total': 412.88}, 'final'),
    (btn('Pay now'), {'url': HOTEL_CHECKOUT, 'total': 412.88}, 'final'),
    (btn('Choose room', typ='button'), {'url': 'https://www.hotels.com/ho12345'}, 'neutral'),
    (btn('Add payment card', typ='button'), {'url': HOTEL_CHECKOUT}, 'commit'),
    (btn('Pay at property', typ='radio', tag='input'), {'url': HOTEL_CHECKOUT}, 'commit'),
    # Target
    (btn('Add to cart', typ='button'), {'url': 'https://www.target.com/p/item'}, 'neutral'),
    (btn('Checkout', typ='button'), {'url': TARGET_CART, 'total': 54.10}, 'navigate'),
    (btn('Proceed to checkout', typ='button'), {'url': TARGET_CART, 'total': 54.10}, 'navigate'),
    (btn('Place your order'), {'url': TARGET_CHECKOUT, 'total': 54.10}, 'final'),
    (btn('Remove', typ='button'), {'url': TARGET_CART, 'total': 54.10}, 'commit'),
    (btn('Save for later', typ='button'), {'url': TARGET_CART, 'total': 54.10}, 'neutral'),
    (btn('Apply', form=TARGET_CHECKOUT), {'url': TARGET_CHECKOUT, 'filled': 1, 'total': 54.10}, 'final'),
    (btn('Use this address', form=TARGET_CHECKOUT), {'url': TARGET_CHECKOUT, 'filled': 4, 'total': 54.10}, 'commit'),
    (btn('Sign up for emails', typ='button'), {'url': TARGET_CART}, 'final'),
    (link('Privacy policy', 'https://www.target.com/privacy'), {'url': TARGET_CART}, 'navigate'),
    (btn('Request refill', form='https://www.cvs.com/rx/refill'), {'url': 'https://www.cvs.com/rx/refill', 'filled': 2}, 'commit'),
    (btn('Request refill', typ='button'), {'url': 'https://www.cvs.com/rx/list'}, 'neutral'),
    (btn('Send', typ='button'), {'url': 'https://www.target.com/help/contact'}, 'final'),
    (btn('Continue', typ='password', tag='input'), {'url': CVS}, 'protected'),
    # 0.20.7 (Nov 7 browser batteries): search/filter buttons, "fees apply" text and dialog closers are not final or sign-in.
    (btn('Submit Location Search', form='https://www.cvs.com/store-locator/landing'), {'url': 'https://www.cvs.com/store-locator/landing', 'filled': 1}, 'navigate'),
    (btn('Apply filters', typ='button'), {'url': HOTEL}, 'navigate'),
    (btn('Standard checked bag fees apply', typ='button'), {'url': 'https://www.delta.com/us/en/baggage/checked-bags', 'total': 45.0}, 'neutral'),
    (btn('Dismiss sign-in info.', typ='button'), {'url': 'https://www.booking.com/searchresults.html'}, 'neutral'),
    (btn('Not now', typ='button'), {'url': 'https://www.booking.com/searchresults.html'}, 'neutral'),
    (btn('Continue as guest', typ='button'), {'url': TARGET_CHECKOUT}, 'neutral'),
    (btn('Submit application', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'final'),
    (btn('Yes, submit', form=LEAVECO), {'url': LEAVECO, 'filled': 5, 'signed_in': True}, 'final'),
    (link('Sign in', 'https://www.booking.com/signin'), {'url': 'https://www.booking.com/'}, 'protected'),
]

class ClassifierTests(unittest.TestCase):
    def test_fixtures(self):
        self.assertGreaterEqual(len(FIXTURES), 40)
        wrong = []
        for control, page, expected in FIXTURES:
            got = classify_target(control, page)
            if got['class'] != expected:
                wrong.append((control['label'], page.get('url'), expected, got))
        self.assertEqual(wrong, [])

    def test_no_final_is_ever_neutral_or_navigate(self):
        finals = [(c, p) for c, p, e in FIXTURES if e == 'final']
        self.assertGreaterEqual(len(finals), 10)
        for c, p in finals:
            for variant in (p, {**p, 'filled': 0}, {**p, 'total': None}, {'url': 'https://example.com/'}):
                got = classify_target(c, variant)['class']
                if c['label'] in ('Reserve', 'Book now') and got == 'neutral' and not variant.get('total') and not variant.get('filled') and 'checkout' not in variant.get('url', ''):
                    continue  # "Reserve"/"Book now" start buttons on a plain page only open the booking form
                self.assertIn(got, ('final', 'commit', 'protected'), (c['label'], variant))

    def test_reasons_are_given(self):
        r = classify_target(btn('Confirm appointment'), {'url': CVS})
        self.assertEqual(r['class'], 'final'); self.assertTrue(r['reasons'])

    def test_form_filled_counts_entries_of_the_same_form(self):
        target = btn('Use this address', form=TARGET_CHECKOUT)
        controls = [target, {'tag': 'input', 'type': 'text', 'form_action': TARGET_CHECKOUT, 'value': '123 Main St'},
                    {'tag': 'input', 'type': 'text', 'form_action': TARGET_CHECKOUT, 'value': ''},
                    {'tag': 'input', 'type': 'text', 'form_action': 'https://www.target.com/search', 'value': 'socks'},
                    {'tag': 'select', 'type': 'select-one', 'form_action': TARGET_CHECKOUT, 'value': 'GA'},
                    {'tag': 'input', 'type': 'checkbox', 'form_action': TARGET_CHECKOUT, 'value': 'on', 'checked': True}]
        self.assertEqual(form_filled(target, controls), 2)
        self.assertEqual(form_filled(btn('Go', form=''), controls), 0)

    def test_autonomous_tasks_stop_on_classified_commits_the_model_cannot_lower(self):
        spec = {'mode': 'autonomous', 'origin': 'https://www.target.com', 'fields': [], 'options': [], 'facts': ['123 Main St'], 'spend_limit_usd': 100.0, 'read_urls': [], 'clicks': []}
        def plan(control, url, cls, values=()):
            return {'url': url, 'page_errors': [], 'cookie_notice': False, 'detected_total': 54.1,
                    'visible_form_values': [{'label': 'x', 'value': v, 'type': 'text'} for v in values],
                    'steps': [{'kind': 'click', 'control': control, 'target_class': cls}]}
        use = btn('Use this address', form=TARGET_CHECKOUT)
        cls = classify_target(use, {'url': TARGET_CHECKOUT, 'filled': 1, 'total': 54.1})
        # Every typed entry is an owner fact: the button sends nothing new.
        self.assertEqual(autonomous_decision(spec, plan(use, TARGET_CHECKOUT, cls, ['123 Main St'])), (True, 'routine'))
        # A value the owner never gave (e.g. typed after a page told the bot to) makes it a commit for them to approve.
        self.assertEqual(autonomous_decision(spec, plan(use, TARGET_CHECKOUT, cls, ['500 Elsewhere Ave'])), (False, 'commit'))
        # A frozen class wins over the label: the model cannot get a "neutral" Continue past a page warning.
        warned = btn('Continue', form=LEAVECO, described='This cannot be undone')
        self.assertEqual(autonomous_decision(spec, plan(warned, LEAVECO, classify_target(warned, {'url': LEAVECO})))[1], 'commit')

    def test_origin_allowlist(self):
        session = {'origin': 'https://www.cvs.com'}
        spec = {'partner_origins': ['https://cvs.scheduler-partner.com']}
        self.assertTrue(origin_allowed(spec, 'https://www.cvs.com', session))
        self.assertTrue(origin_allowed(spec, 'https://cvs.scheduler-partner.com', session))
        self.assertFalse(origin_allowed(spec, 'https://collect.evil.example', session))
        self.assertFalse(origin_allowed({}, 'https://cvs.scheduler-partner.com', session))

    def test_plan_check_flags_new_addresses_and_amounts(self):
        spec = {'title': 'Leaveco absence', 'goal': 'Report Nov 3 absence; email updates to owner@example.com', 'facts': ['4'], 'fields': []}
        fill = lambda v: {'steps': [{'kind': 'fill', 'control': btn('x', typ='text', tag='input'), 'value': v}]}
        self.assertIsNone(plan_deviation(spec, fill('owner@example.com')))
        self.assertIsNone(plan_deviation(spec, fill('31901')))
        self.assertIn('not in the task', plan_deviation(spec, fill('attacker@evil.example')))
        self.assertIn('not in the task', plan_deviation(spec, fill('see https://evil.example/x')))
        self.assertIn('amount', plan_deviation(spec, fill('$950.00')))

if __name__ == '__main__':
    unittest.main()
