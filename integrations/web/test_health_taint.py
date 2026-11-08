"""Health labels and leak checks (0.25.0, H6): values the assistant saw from health tools, and the values in their grants and
connections, may only leave toward the granted institution's own recipients (or their own address)."""
import tempfile
import unittest

from health import Health
from health_taint import Taint, label_worthy, phone_key



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
    def telegram(self, message):
        self.messages.append(message)


class TaintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1_800_000_000.0]
        self.p = FakeBroker()
        self.h = Health(self.p, self.tmp.name, now=lambda: self.clock[0])
        ready(self.h)
        self.t = Taint(self.h, now=lambda: self.clock[0])
        self.conn = self.h.call('health_connection_add', {'provider': 'clinic_web', 'institution': 'Midtown Dermatology', 'environment': 'production',
                                                           'endpoint': 'https://portal.midtownderm.example/fhir', 'contacts': ['scheduling@midtownderm.example', '+13055550142']}, True)

    def tearDown(self):
        self.tmp.cleanup()

    def grant(self):
        p = self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Derm follow-up', 'actions': ['appointment.search', 'appointment.book'],
                                                  'constraints': {'clinician': 'Dr. Ana Rivera', 'visit_type': 'follow-up', 'locations': ['Brickell office']},
                                                  'disclosures': ['name', 'date of birth']})
        return self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)

    def test_labels_come_from_identifying_fields_only(self):
        n = self.t.note({'appointments': [{'id': 'a' * 16, 'status': 'confirmed', 'clinician': 'Dr. Ana Rivera', 'location': 'Brickell office',
                                           'reason': 'Mole check', 'start': '2026-11-18T14:30:00-05:00', 'phone': '(305) 555-0199', 'visit_type': 'follow-up'}]})
        vals = self.t.values()
        self.assertIn('dr ana rivera', vals)
        self.assertIn('brickell office', vals)
        self.assertIn('mole check', vals)
        self.assertIn('3055550199', vals)
        self.assertNotIn('confirmed', vals)
        self.assertNotIn('follow up', vals)
        self.assertGreaterEqual(n, 4)
        self.assertIsNone(label_worthy('Tuesday'))
        self.assertIsNone(label_worthy('2026-11-18'))
        self.assertEqual(phone_key('+1 (305) 555-0199'), '3055550199')

    def test_text_with_a_label_is_refused_toward_an_unlisted_recipient(self):
        self.t.note({'clinician': 'Dr. Ana Rivera', 'reason': 'Mole check'})
        r = self.t.check('Hi Sam, my appointment with Dr. Ana Rivera moved to Friday.', ['sam@example.com'])
        self.assertTrue(r['tainted'])
        self.assertFalse(r['allowed'])
        self.assertIn('Nothing was sent', r['reason'])
        self.assertNotIn('Rivera', r['reason'])   # the refusal never repeats the health value
        self.assertFalse(self.t.check('Lunch on Friday?', ['sam@example.com'])['tainted'])

    def test_granted_institution_recipients_are_allowed(self):
        self.t.note({'clinician': 'Dr. Ana Rivera'})
        text = 'Please move my visit with Dr. Ana Rivera.'
        self.assertFalse(self.t.check(text, ['scheduling@midtownderm.example'])['allowed'])   # no active grant yet
        self.grant()
        self.assertTrue(self.t.check(text, ['scheduling@midtownderm.example'])['allowed'])
        self.assertTrue(self.t.check(text, ['other.desk@midtownderm.example'])['allowed'])      # same domain as a listed contact
        self.assertTrue(self.t.check(text, '+1 305 555 0142')['allowed'])
        self.assertTrue(self.t.check(text, 'https://portal.midtownderm.example/book')['allowed'])
        self.assertFalse(self.t.check(text, ['scheduling@midtownderm.example', 'sam@example.com'])['allowed'])  # every recipient must be allowed
        self.assertFalse(self.t.check(text, None)['allowed'])

    def test_grant_values_are_labeled_while_the_grant_lives_and_revocation_closes_recipients(self):
        g = self.grant()
        r = self.t.check('Booked: Brickell office, Thursday', ['friend@example.com'])
        self.assertTrue(r['tainted'])
        self.h.call('health_grant_revoke', {'grant': g['id'], 'why': 'test'}, True)
        self.assertFalse(self.t.check('Booked: Brickell office', ['friend@example.com'])['tainted'])
        self.t.note({'clinician': 'Dr. Ana Rivera'})
        self.assertFalse(self.t.check('Dr. Ana Rivera', ['scheduling@midtownderm.example'])['allowed'])

    def test_phone_numbers_match_in_any_format(self):
        self.t.note({'pharmacy_phone': '305-555-0177'})
        self.assertTrue(self.t.check('Call them at (305) 555 0177 please', ['x@y.co'])['tainted'])
        self.assertTrue(self.t.check('Call +13055550177', ['x@y.co'])['tainted'])
        self.assertFalse(self.t.check('Call 305-555-0100', ['x@y.co'])['tainted'])

    def test_owner_address_is_always_allowed_and_labels_expire(self):
        self.t.note({'medication': 'Tretinoin cream'})
        self.assertTrue(self.t.check('Pick up Tretinoin cream', ['owner@example.com'], owner_addresses=['owner@example.com'])['allowed'])
        self.clock[0] += 31 * 86400
        self.assertFalse(self.t.check('Pick up Tretinoin cream', ['sam@example.com'])['tainted'])

    def test_word_boundaries_avoid_partial_matches(self):
        self.t.note({'clinician': 'Dr. Lee'})
        self.assertFalse(self.t.check('Dr. Leeds sent the invoice', ['x@y.co'])['tainted'])
        self.assertTrue(self.t.check('dr lee called', ['x@y.co'])['tainted'])

    def test_contacts_are_validated(self):
        with self.assertRaisesRegex(ValueError, 'contacts'):
            self.h.call('health_connection_add', {'provider': 'cvs', 'institution': 'CVS', 'contacts': ['not a recipient']}, True)
        c = self.h.call('health_connection_add', {'provider': 'clinic_phone', 'institution': 'Bay Clinic', 'endpoint': 'tel:+13055550123'}, True)
        self.assertEqual(c['endpoint'], 'tel:+13055550123')
        self.assertEqual(c['contacts'], [])


class BrokerWiring(unittest.TestCase):
    def test_health_results_on_the_model_socket_are_labeled(self):
        import broker as B
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = B.Broker(tmp.name + '/browser.sqlite3', provider=None) if 'provider' in B.Broker.__init__.__code__.co_varnames else B.Broker(tmp.name + '/browser.sqlite3')
        b.health.call('health_adult_confirm', {}, True)
        b.health.call('health_connection_add', {'provider': 'clinic_web', 'institution': 'Coral Gables Cardiology', 'environment': 'fake'}, True)
        b.call('health_status', {}, False)   # the model reads status: the institution name is now labeled
        r = b.call('health_taint_check', {'text': 'I am seeing Coral Gables Cardiology next week', 'destination': ['friend@example.com']}, False)
        self.assertTrue(r['tainted'])
        self.assertFalse(r['allowed'])
        self.assertEqual(set(r), {'tainted', 'allowed', 'labels', 'reason', 'label_kinds'})


if __name__ == '__main__':
    unittest.main()
