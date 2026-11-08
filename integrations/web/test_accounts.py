"""Accounts (web 0.29.0): DAV client against a fake iCloud, the account store, and the dashboard Accounts tab."""
import datetime as dt
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import accounts
import icloud_dav
from icloud_dav import DAVClient, DAVError, parse_vevents, parse_vcards, build_vevent, fold


MS = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:card="urn:ietf:params:xml:ns:carddav" ' \
     'xmlns:cs="http://calendarserver.org/ns/" xmlns:ical="http://apple.com/ns/ical/">%s</d:multistatus>'


def resp(href, props):
    return '<d:response><d:href>%s</d:href><d:propstat><d:prop>%s</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>' % (href, props)


class FakeICloud(http.server.BaseHTTPRequestHandler):
    """Enough of iCloud's CalDAV/CardDAV to exercise discovery, listing, querying and creating. Principal 12345."""
    puts = []
    bad_password = False

    def log_message(self, *a):
        pass

    def body(self):
        return self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()

    def answer(self, status, xml, headers=None):
        data = xml.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if FakeICloud.bad_password or self.headers.get('Authorization') != 'Basic ' + 'dXNlckBpY2xvdWQuY29tOmFiY2QtZWZnaC1pamtsLW1ub3A=':
            self.send_response(401); self.send_header('Content-Length', '0'); self.end_headers()
            return False
        return True

    def do_PROPFIND(self):
        if not self.authorized():
            return
        b = self.body()
        base = 'http://%s' % self.headers['Host']
        if self.path == '/' and 'current-user-principal' in b:
            return self.answer(207, MS % resp('/', '<d:current-user-principal><d:href>/12345/principal/</d:href></d:current-user-principal>'))
        if self.path == '/12345/principal/' and 'calendar-home-set' in b:
            return self.answer(207, MS % resp('/12345/principal/', '<c:calendar-home-set><d:href>%s/12345/calendars/</d:href></c:calendar-home-set>' % base))
        if self.path == '/12345/principal/' and 'addressbook-home-set' in b:
            return self.answer(207, MS % resp('/12345/principal/', '<card:addressbook-home-set><d:href>/12345/carddavhome/</d:href></card:addressbook-home-set>'))
        if self.path == '/12345/calendars/':
            return self.answer(207, MS % (
                resp('/12345/calendars/', '<d:resourcetype><d:collection/></d:resourcetype>') +
                resp('/12345/calendars/home/', '<d:displayname>Home</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
                     '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set><ical:calendar-color>#FF2D55FF</ical:calendar-color><cs:getctag>ct1</cs:getctag>') +
                resp('/12345/calendars/reminders/', '<d:displayname>Reminders</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
                     '<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>') +
                resp('/12345/calendars/work/', '<d:displayname>Work</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>')))
        if self.path == '/12345/carddavhome/':
            return self.answer(207, MS % (resp('/12345/carddavhome/', '<d:resourcetype><d:collection/></d:resourcetype>') +
                                          resp('/12345/carddavhome/card/', '<d:displayname>All Contacts</d:displayname><d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>')))
        self.answer(404, '')

    def do_REPORT(self):
        if not self.authorized():
            return
        b = self.body()
        if self.path == '/12345/calendars/home/':
            ics = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e1\r\nSUMMARY:Dentist\\, Dr. Lee\r\nDTSTART;TZID=America/New_York:20261110T090000\r\n'
                   'DTEND;TZID=America/New_York:20261110T100000\r\nLOCATION:12 Main St\r\nDESCRIPTION:Bring insurance card\\nand ID\r\nEND:VEVENT\r\n'
                   'BEGIN:VEVENT\r\nUID:e2\r\nSUMMARY:Conference\r\nDTSTART;VALUE=DATE:20261112\r\nDTEND;VALUE=DATE:20261114\r\nRRULE:FREQ=YEARLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
            self.assertion = 'time-range' in b
            return self.answer(207, MS % (resp('/12345/calendars/home/e1.ics', '<d:getetag>"1"</d:getetag><c:calendar-data>%s</c:calendar-data>' % ics.replace('&', '&amp;'))))
        if self.path.startswith('/12345/calendars/'):
            return self.answer(207, MS % '')
        if self.path == '/12345/carddavhome/card/':
            vcf = ('BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1\r\nFN:Ana Perez\r\nN:Perez;Ana;;;\r\nTEL;TYPE=CELL:+1 555 0100\r\nEMAIL;TYPE=HOME:ana@example.com\r\nORG:Primaris;Research\r\n'
                   'BDAY:19900415\r\nEND:VCARD\r\nBEGIN:VCARD\r\nVERSION:3.0\r\nN:Lee;Sam;;;\r\nTEL:+1 555 0199\r\nEND:VCARD\r\n')
            return self.answer(207, MS % resp('/12345/carddavhome/card/c1.vcf', '<d:getetag>"9"</d:getetag><card:address-data>%s</card:address-data>' % vcf))
        self.answer(404, '')

    def do_PUT(self):
        if not self.authorized():
            return
        FakeICloud.puts.append({'path': self.path, 'body': self.body(), 'if_none_match': self.headers.get('If-None-Match')})
        self.answer(201, '', {'ETag': '"new1"'})


class DAVTests(unittest.TestCase):
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
        self.c = DAVClient(self.base, 'user@icloud.com', 'abcd-efgh-ijkl-mnop')

    def test_discovery_and_calendars_skip_reminders(self):
        cals = self.c.calendars()
        self.assertEqual([c['name'] for c in cals], ['Home', 'Work'])
        self.assertTrue(cals[0]['url'].startswith(self.base + '/12345/calendars/home/'))
        self.assertEqual(cals[0]['color'], '#FF2D55FF')

    def test_events_parse_timezones_dates_and_escapes(self):
        evs = self.c.events(self.base + '/12345/calendars/home/', dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc), dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc))
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]['summary'], 'Dentist, Dr. Lee')
        self.assertEqual(evs[0]['start'], '2026-11-10T09:00:00 America/New_York')
        self.assertEqual(evs[0]['description'], 'Bring insurance card\nand ID')
        self.assertFalse(evs[0]['all_day'])
        self.assertEqual(evs[1]['start'], '2026-11-12'); self.assertTrue(evs[1]['all_day']); self.assertTrue(evs[1]['recurring'])
        self.assertEqual(evs[0]['etag'], '"1"')

    def test_create_event_never_overwrites(self):
        r = self.c.create_event(self.base + '/12345/calendars/home/', 'Call Leaveco; re: claim', dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.timezone.utc),
                                dt.datetime(2026, 11, 20, 14, 30, tzinfo=dt.timezone.utc), location='Phone')
        self.assertEqual(len(FakeICloud.puts), 1)
        put = FakeICloud.puts[0]
        self.assertEqual(put['if_none_match'], '*')
        self.assertTrue(put['path'].endswith(r['uid'] + '.ics'))
        self.assertIn('SUMMARY:Call Leaveco\\; re: claim', put['body'])
        self.assertIn('DTSTART:20261120T140000Z', put['body'])
        self.assertEqual(r['etag'], '"new1"')

    def test_contacts(self):
        books = self.c.addressbooks()
        self.assertEqual(books[0]['name'], 'All Contacts')
        cards = self.c.contacts(books[0]['url'])
        self.assertEqual([c['name'] for c in cards], ['Ana Perez', 'Sam Lee'])
        self.assertEqual(cards[0]['phones'][0], {'type': 'cell', 'number': '+1 555 0100'})
        self.assertEqual(cards[0]['org'], 'Primaris'); self.assertEqual(cards[0]['birthday'], '1990-04-15')

    def test_wrong_password_is_a_clear_message_without_the_secret(self):
        bad = DAVClient(self.base, 'user@icloud.com', 'wrong-pass-word-here')
        with self.assertRaises(DAVError) as cm:
            bad.calendars()
        self.assertIn('app-specific password', str(cm.exception))
        self.assertNotIn('wrong-pass', str(cm.exception))

    def test_parsers_and_folding(self):
        self.assertEqual(parse_vevents('BEGIN:VEVENT\r\nSUMMARY:A very long\r\n  summary\r\nDTSTART:20260101T120000Z\r\nEND:VEVENT')[0]['summary'], 'A very long summary')
        self.assertEqual(parse_vcards('BEGIN:VCARD\r\nFN:X\r\nEND:VCARD')[0]['name'], 'X')
        long = 'SUMMARY:' + 'é' * 100
        folded = fold(long)
        self.assertTrue(all(len(l.encode()) <= 75 for l in folded.split('\r\n')))
        self.assertEqual(icloud_dav.unfold(folded), long)
        ics = build_vevent('u1', 'Trip', dt.date(2026, 12, 1), dt.date(2026, 12, 3), all_day=True)
        self.assertIn('DTSTART;VALUE=DATE:20261201', ics)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.calls = []
        def imap(host, port, user, pw):
            self.calls.append(('imap', host, user, pw)); return {'ok': pw != 'abcd-efgh-ijkl-wrng', 'messages': 42} if pw != 'abcd-efgh-ijkl-wrng' else {'ok': False, 'error': 'The mail server did not accept this email and app password.'}
        def smtp(host, port, user, pw):
            return {'ok': False, 'blocked': True, 'error': 'port 587 blocked'} if host.endswith('me.com') else {'ok': True}
        def caldav(base, user, pw):
            return {'ok': True, 'calendars': [{'name': 'Home', 'url': base + '/h/'}, {'name': 'Work', 'url': base + '/w/'}]}
        def carddav(base, user, pw):
            return {'ok': True, 'addressbooks': [{'name': 'All', 'url': base + '/c/'}]}
        self.store = accounts.Accounts(root / 'accounts.sqlite3', root / 'secrets', now=lambda: 1_700_000_000.0,
                                       probes={'imap': imap, 'smtp': smtp, 'caldav': caldav, 'carddav': carddav})
        self.root = root

    def test_connect_icloud_lights_services_and_writes_legacy_file(self):
        r = self.store.connect('icloud', ' Owner@icloud.example ', 'ABCD-EFGH-IJKL-MNOP')
        self.assertEqual(r['email'], 'owner@icloud.example')
        self.assertEqual(r['services']['mail'], {'state': 'ok', 'messages': 42})
        self.assertEqual(r['services']['send']['state'], 'blocked')
        self.assertEqual(r['services']['calendar']['calendars'], ['Home', 'Work'])
        self.assertEqual(r['services']['contacts'], {'state': 'ok', 'addressbooks': 1})
        secret = self.root / 'secrets' / 'accounts' / (r['id'] + '.json')
        self.assertEqual(oct(secret.stat().st_mode & 0o777), '0o600')
        legacy = json.loads((self.root / 'secrets' / 'accounts' / 'icloud-mail.json').read_text())
        self.assertEqual(legacy, {'email': 'owner@icloud.example', 'username': 'owner@icloud.example', 'password': 'abcd-efgh-ijkl-mnop'})
        listing = json.dumps(self.store.list())
        self.assertNotIn('abcd-efgh', listing)
        self.assertEqual(self.store.for_service('calendar')['id'], r['id'])
        self.assertEqual(self.store.for_service('send')['id'], r['id'])  # blocked still routes to the companion/relay

    def test_wrong_password_saves_nothing(self):
        with self.assertRaises(accounts.AccountError) as cm:
            self.store.connect('icloud', 'a@icloud.com', 'abcd-efgh-ijkl-wrng')
        self.assertIn('did not accept', str(cm.exception)); self.assertIn('Nothing was saved', str(cm.exception))
        self.assertEqual(self.store.list()['accounts'], [])
        self.assertFalse((self.root / 'secrets' / 'accounts').exists())

    def test_password_shape_is_checked_before_any_network_call(self):
        for pid, pw in (('icloud', 'my real apple password'), ('gmail', 'short')):
            with self.assertRaises(accounts.AccountError):
                self.store.connect(pid, 'a@b.com', pw)
        self.assertEqual(self.calls, [])

    def test_gmail_app_password_spaces_tolerated_and_no_dav(self):
        r = self.store.connect('gmail', 'owner@example.com', 'abcd efgh ijkl mnop')
        self.assertEqual(set(r['services']), {'mail', 'send'})
        self.assertEqual(r['services']['send']['state'], 'ok')
        self.assertEqual(json.loads((self.root / 'secrets' / 'accounts' / (r['id'] + '.json')).read_text())['password'], 'abcdefghijklmnop')
        self.assertFalse((self.root / 'secrets' / 'accounts' / 'icloud-mail.json').exists())

    def test_reconnect_same_email_updates_and_disconnect_removes_everything(self):
        a = self.store.connect('icloud', 'a@icloud.com', 'abcd-efgh-ijkl-mnop')
        b = self.store.connect('icloud', 'a@icloud.com', 'abcd-efgh-ijkl-mnoq')
        self.assertEqual(a['id'], b['id']); self.assertEqual(len(self.store.list()['accounts']), 1)
        self.store.disconnect(a['id'])
        self.assertEqual(self.store.list()['accounts'], [])
        self.assertFalse((self.root / 'secrets' / 'accounts' / (a['id'] + '.json')).exists())
        self.assertFalse((self.root / 'secrets' / 'accounts' / 'icloud-mail.json').exists())
        with self.assertRaises(accounts.AccountError):
            self.store.credentials(a['id'])

    def test_recheck_marks_reconnect_when_provider_now_refuses(self):
        a = self.store.connect('icloud', 'a@icloud.com', 'abcd-efgh-ijkl-mnop')
        self.store.probes['imap'] = lambda h, p, u, pw: {'ok': False, 'error': 'The mail server did not accept this email and app password.'}
        r = self.store.recheck(a['id'])
        self.assertEqual(r['status'], 'reconnect'); self.assertIn('did not accept', r['error'])

    def test_google_identities_adapter(self):
        ids = accounts.google_identities('tok', fetch=lambda url, h: {'sub': '1', 'email': 'x@gmail.com'} if h['Authorization'] == 'Bearer tok' else {})
        self.assertEqual(ids, [{'external_id': '1', 'display': 'x@gmail.com', 'kind': 'person'}])
        self.assertNotIn('gmail.readonly', ' '.join(accounts.GOOGLE_OAUTH['scopes']))
        self.assertNotIn('gmail.modify', ' '.join(accounts.GOOGLE_OAUTH['scopes']))


class DashboardAccountsTests(unittest.TestCase):
    """The Accounts tab end to end through the HTTP handler: page, connect (probes stubbed), listing without secrets, disconnect."""
    def setUp(self):
        import test_dashboard as td
        self.td = td.Tests('test_login_link_is_single_use_and_sessions_are_capped')
        self.td.setUp()
        self.addCleanup(self.td.doCleanups)
        self.td.login()
        acc = self.td.dash.accounts
        self.assertIsNotNone(acc)
        acc.probes['imap'] = lambda h, p, u, pw: {'ok': pw == 'abcd-efgh-ijkl-mnop', 'messages': 7} if pw == 'abcd-efgh-ijkl-mnop' else {'ok': False, 'error': 'The mail server did not accept this email and app password.'}
        acc.probes['smtp'] = lambda h, p, u, pw: {'ok': False, 'blocked': True, 'error': 'port 587 blocked'}
        acc.probes['caldav'] = lambda b, u, pw: {'ok': True, 'calendars': [{'name': 'Home', 'url': b + '/h/'}]}
        acc.probes['carddav'] = lambda b, u, pw: {'ok': True, 'addressbooks': [{'name': 'All', 'url': b + '/c/'}]}

    def test_tab_connect_list_disconnect(self):
        status, page = self.td.req('/accounts')
        self.assertEqual(status, 200)
        self.assertIn('Apple (iCloud)', page); self.assertIn('App-specific password', page); self.assertIn('type="password"', page)
        self.assertIn('/accounts', page)  # the tab link
        status, j = self.td.req('/api/accounts/connect', {'provider': 'icloud', 'email': 'User@iCloud.com', 'password': 'abcd-efgh-ijkl-mnop'})
        self.assertEqual(status, 200, j)
        self.assertTrue(j['ok']); self.assertIn('mail, calendar, contacts live', j['message']); self.assertIn('send via the Mac companion', j['message'])
        self.assertNotIn('abcd-efgh', json.dumps(j))
        status, j2 = self.td.req('/api/accounts')
        self.assertEqual(j2['accounts'][0]['email'], 'user@icloud.com')
        self.assertNotIn('abcd-efgh', json.dumps(j2))
        status, page = self.td.req('/accounts')
        self.assertIn('user@icloud.com', page); self.assertIn('Home', page); self.assertIn('via mac or relay', page)
        status, bad = self.td.req('/api/accounts/connect', {'provider': 'icloud', 'email': 'other@icloud.com', 'password': 'abcd-efgh-ijkl-xxxx'})
        self.assertEqual(status, 400); self.assertIn('Nothing was saved', bad['error'])
        aid = j2['accounts'][0]['id']
        status, j3 = self.td.req('/api/accounts/%s/disconnect' % aid, {})
        self.assertTrue(j3['ok']); self.assertIn('deleted', j3['message'])
        self.assertEqual(self.td.req('/api/accounts')[1]['accounts'], [])

    def test_unknown_action_and_unauthenticated(self):
        status, j = self.td.req('/api/accounts/nope/explode', {})
        self.assertEqual(status, 400)
        self.td.cookie = None
        self.assertEqual(self.td.req('/accounts')[0], 401)


if __name__ == '__main__':
    unittest.main()
