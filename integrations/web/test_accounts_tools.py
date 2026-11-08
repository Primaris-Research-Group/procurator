"""Apple calendar/contacts tools (web 0.29.0) against the fake iCloud from test_accounts."""
import datetime as dt
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

import accounts
import accounts_tools
from test_accounts import FakeICloud


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeICloud)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeICloud.puts = []; FakeICloud.bad_password = False
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.saved = dict(accounts.PROVIDERS['icloud'])
        accounts.PROVIDERS['icloud']['caldav'] = self.base
        accounts.PROVIDERS['icloud']['carddav'] = self.base
        self.addCleanup(lambda: accounts.PROVIDERS['icloud'].update(self.saved))
        self.store = accounts.Accounts(root / 'a.sqlite3', root / 'secrets',
                                       probes={'imap': lambda *a: {'ok': True, 'messages': 1}, 'smtp': lambda *a: {'ok': True}})
        self.wrapped = []
        self.tools = accounts_tools.AppleTools(self.store, wrap=lambda t, s: (self.wrapped.append(s), '<u>%s</u>' % t)[1], now=lambda: 1000.0)

    def connect(self):
        return self.store.connect('icloud', 'user@icloud.com', 'abcd-efgh-ijkl-mnop')

    def test_not_connected_is_a_clear_message(self):
        with self.assertRaises(accounts.AccountError) as cm:
            self.tools.call('calendar_apple_events', {})
        self.assertIn('Accounts tab', str(cm.exception))
        self.assertEqual(self.tools.call('accounts_list', {})['accounts'], [])

    def test_events_default_window_wrapped_for_the_model(self):
        self.connect()
        r = self.tools.call('calendar_apple_events', {'start': '2026-11-09', 'days': '7'})
        self.assertEqual(r['account'], 'user@icloud.com'); self.assertEqual(r['calendars'], ['Home', 'Work'])
        self.assertEqual(r['count'], 2)
        self.assertEqual(r['events'][0]['summary'], '<u>Dentist, Dr. Lee</u>'); self.assertEqual(r['events'][0]['calendar'], 'Home')
        self.assertIn('icloud-calendar', self.wrapped)
        priv = self.tools.call('calendar_apple_events', {'start': '2026-11-09', 'calendar': 'Home'}, privileged=True)
        self.assertEqual(priv['events'][0]['summary'], 'Dentist, Dr. Lee')
        with self.assertRaises(ValueError):
            self.tools.call('calendar_apple_events', {'start': '2026-11-09', 'calendar': 'Nope'})
        with self.assertRaises(ValueError):
            self.tools.call('calendar_apple_events', {'start': '2026-01-01', 'end': '2026-12-01'})

    def test_create_event_timed_and_all_day(self):
        self.connect()
        r = self.tools.call('calendar_apple_create_event', {'summary': 'Call Leaveco', 'start': '2026-11-20T14:00:00-05:00', 'calendar': 'work', 'location': 'Phone'})
        self.assertTrue(r['created']); self.assertEqual(r['calendar'], 'Work'); self.assertEqual(r['end'], '2026-11-20T15:00:00-05:00')
        self.assertIn('DTSTART:20261120T190000Z', FakeICloud.puts[-1]['body'])
        r2 = self.tools.call('calendar_apple_create_event', {'summary': 'Trip', 'start': '2026-12-01', 'all_day': True})
        self.assertEqual(r2['calendar'], 'Home'); self.assertEqual(r2['end'], '2026-12-02')
        self.assertIn('DTSTART;VALUE=DATE:20261201', FakeICloud.puts[-1]['body'])
        for bad in ({'summary': '', 'start': '2026-12-01T10:00:00Z'}, {'summary': 'x', 'start': '2026-12-01'}, {'summary': 'x', 'start': '2026-12-01T10:00:00Z', 'end': '2026-12-01T09:00:00Z'}):
            with self.assertRaises(ValueError):
                self.tools.call('calendar_apple_create_event', bad)

    def test_contacts_search_by_name_phone_email_with_cache(self):
        self.connect()
        r = self.tools.call('contacts_apple_search', {'query': 'ana'})
        self.assertEqual(r['count'], 1); self.assertEqual(r['matches'][0]['name'], '<u>Ana Perez</u>'); self.assertEqual(r['matches'][0]['phones'][0]['number'], '+1 555 0100')
        self.assertEqual(self.tools.call('contacts_apple_search', {'query': '0199'})['matches'][0]['name'], '<u>Sam Lee</u>')
        self.assertEqual(self.tools.call('contacts_apple_search', {'query': 'ana@example.com'})['count'], 1)
        self.assertEqual(self.tools.call('contacts_apple_search', {'query': 'zzz'})['count'], 0)
        with self.assertRaises(ValueError):
            self.tools.call('contacts_apple_search', {'query': 'a'})
        FakeICloud.bad_password = True   # the cache answers for ten minutes even if iCloud is unreachable
        self.assertEqual(self.tools.call('contacts_apple_search', {'query': 'sam'})['count'], 1)

    def test_listing_states(self):
        self.connect()
        r = self.tools.call('accounts_list', {})
        self.assertEqual(r['accounts'][0]['services'], {'mail': 'ok', 'send': 'ok', 'calendar': 'ok', 'contacts': 'ok'})
        self.assertNotIn('abcd', str(r))


if __name__ == '__main__':
    unittest.main()
