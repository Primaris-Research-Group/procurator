"""0.20.4 (Nov 6, hotels.com live): the research task approved "Destination: Miami, Florida" and the bot's "Miami" in the
search box was refused. An entry that says no more than an approved fact is allowed; anything else still stops."""
import unittest
from task_policy import fact_matches, research_decision, autonomous_decision
from test_broker import control

HOTELS = 'https://www.hotels.com/'
FACTS = ['Destination: Miami, Florida', 'Check-in: December 25, 2026', 'Check-out: December 26, 2026', 'Two adults', 'One room']


def spec(**extra):
    return {'mode': 'research', 'facts': FACTS, 'fields': [], 'options': [], 'spend_limit_usd': None, 'fill_form': False,
            'site_login': 'hotels'} | extra


def fill(label, value):
    return {'url': HOTELS, 'steps': [{'kind': 'fill', 'control': control('e1', 'text', label), 'value': value}], 'page_errors': [], 'detected_total': None}


class FactValueTests(unittest.TestCase):
    def test_the_hotels_search_entries_are_allowed(self):
        for label, value in [('Where to?', 'Miami'), ('Where to?', 'Miami, Florida'), ('Where to?', 'Mia'), ('Check-in', '12/25/2026'),
                             ('Check-in', 'Dec 25'), ('Check-out', '2026-12-26'), ('Check-out', '26 December 2026'), ('Adults', '2'), ('Rooms', '1')]:
            self.assertEqual(research_decision(spec(), fill(label, value)), (True, None), value)

    def test_anything_more_than_the_facts_still_stops(self):
        for value in ['Orlando', 'Mi', '12/24/2026', 'Dec 25 2025', '3', 'Destination: Orlando', 'Tampa luxury suites']:
            self.assertFalse(fact_matches(value, FACTS), value)
            self.assertEqual(research_decision(spec(), fill('Where to?', value))[1], 'value_not_approved', value)
        # 0.20.7: a richer search phrase ABOUT the task's own facts may be typed into a search/place field (read-only research).
        self.assertEqual(research_decision(spec(), fill('Where to?', 'Miami Beach luxury suites')), (True, None))
        self.assertEqual(research_decision(spec(), fill('Guest name', 'Miami Beach luxury suites'))[1], 'value_not_approved')   # not a search field
        self.assertIn(autonomous_decision(spec(mode='autonomous'), fill('Password', 'Miami'))[1], ('login', 'account_protected'))   # never a credential field

    def test_plain_facts_and_labels_with_times_keep_working(self):
        self.assertTrue(fact_matches('30309', ['30309'])); self.assertTrue(fact_matches('7:30 PM', ['7:30 PM']))
        self.assertTrue(fact_matches('7:30 PM', ['Time: 7:30 PM'])); self.assertFalse(fact_matches('30:', ['Time: 7:30 PM']))
        self.assertFalse(fact_matches('', FACTS)); self.assertFalse(fact_matches('anything', []))


if __name__ == '__main__':
    unittest.main()
