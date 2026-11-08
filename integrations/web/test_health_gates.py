"""0.25.0 health gates: 18+ confirmation (H8), ask-before-read with once/standing scopes (H2), and disconnecting deletes what a
connection or device synced (H4)."""
import tempfile
import unittest

from health import Health


class Quiet:
    def __init__(self):
        self.messages = []
    def telegram(self, m):
        self.messages.append(m)
    def send_with_buttons(self, m, rows):
        self.messages.append(m)


class Gates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1_800_000_000.0]
        self.p = Quiet()
        self.h = Health(self.p, self.tmp.name, now=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_module_is_off_until_the_owner_confirms_18_or_older(self):
        with self.assertRaisesRegex(ValueError, '18 or older'):
            self.h.call('health_connection_add', {'provider': 'cvs', 'institution': 'CVS'}, True)
        with self.assertRaisesRegex(ValueError, '18 or older'):
            self.h.call('health_grant_propose', {})
        self.assertEqual(self.h.call('health_status', {})['subjects'][0]['relation'], 'self')   # status still answers
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_adult_confirm', {}, False)
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_policy', {'set': {'adult_confirmed': 'yes'}}, False)
        self.assertEqual(self.h.call('health_adult_confirm', {}, True)['adult_confirmed'], 'yes')
        self.assertEqual(self.h.call('health_connection_add', {'provider': 'cvs', 'institution': 'CVS'}, True)['provider'], 'cvs')

    def test_reads_of_stored_data_ask_first_then_honor_once_and_standing_scopes(self):
        self.h.call('health_adult_confirm', {}, True)
        self.h.attach('health_sync_', type('S', (), {'call': staticmethod(lambda action, a, privileged: {'days': {'2026-11-07': {'step_count': 5000}}})})())
        with self.assertRaisesRegex(ValueError, 'not allowed reading'):
            self.h.call('health_sync_activity', {})
        self.assertEqual(len(self.p.messages), 1)
        self.assertRegex(self.p.messages[0], r'/healthread [a-f0-9]{8} once')
        self.assertNotIn('5000', self.p.messages[0])                       # the request never carries the data
        with self.assertRaises(ValueError):
            self.h.call('health_sync_activity', {})
        self.assertEqual(len(self.p.messages), 1)                          # one request, not one per retry
        rid = self.p.messages[0].split('/healthread ')[1].split()[0]
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_read_allow', {'request': rid, 'allow': 'once'}, False)
        self.h.call('health_read_allow', {'request': rid, 'allow': 'once'}, True)
        self.assertIn('days', self.h.call('health_sync_activity', {}))
        with self.assertRaises(ValueError):
            self.h.call('health_sync_activity', {})                         # once means once
        out = self.h.call('health_read_allow', {'category': 'activity', 'days': 30}, True)
        self.assertTrue(out['scopes']['activity'].startswith('until '))
        self.h.call('health_sync_activity', {})
        self.clock[0] += 31 * 86400
        with self.assertRaises(ValueError):
            self.h.call('health_sync_activity', {})
        self.h.call('health_read_allow', {'category': 'activity', 'days': 30}, True)
        self.h.call('health_read_allow', {'category': 'activity', 'allow': 'off'}, True)
        with self.assertRaises(ValueError):
            self.h.call('health_sync_activity', {})
        self.assertIn('days', self.h.call('health_sync_activity', {}, True))   # their own dashboard reads are not gated

    def test_revoking_a_connection_deletes_its_synced_records(self):
        self.h.call('health_adult_confirm', {}, True)
        c = self.h.call('health_connection_add', {'provider': 'epic', 'institution': 'Bay Health', 'environment': 'fake'}, True)
        with self.h.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS portal_links(connection TEXT PRIMARY KEY,iss TEXT,patient_id TEXT,patient_name TEXT,scopes TEXT,linked_at REAL,last_ok REAL,last_error TEXT)')
            db.execute("INSERT INTO portal_links VALUES(?,?,?,?,?,?,?,?)", (c['id'], 'iss', 'p1', 'Synthetic Patient', '[]', 0, 0, ''))
            db.execute('CREATE TABLE IF NOT EXISTS rx_refills(effect_id TEXT PRIMARY KEY,connection TEXT,refill_id TEXT,prescription_id TEXT,status TEXT,status_text TEXT)')
            db.execute("INSERT INTO rx_refills VALUES('e1',?,'r1','p1','ready','')", (c['id'],))
        out = self.h.call('health_connection_status', {'connection': c['id'], 'status': 'revoked'}, True)
        self.assertEqual(out['deleted'], {'portal link': 1, 'refill record': 1})
        with self.h.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM portal_links WHERE connection=?', (c['id'],)).fetchone())


if __name__ == '__main__':
    unittest.main()


class VisitsAndDigest(unittest.TestCase):
    BOOK = {'clinician': 'Dr. Rivera', 'location': 'Midtown clinic', 'date': '2027-01-12', 'time': '14:30', 'visit_type': 'follow-up', 'weekday': 'Tuesday', 'time_of_day': 'afternoon', 'cost_usd': 0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [1_799_600_000.0]   # January 10, 2027 (local)
        self.p = Quiet()
        self.h = Health(self.p, self.tmp.name, now=lambda: self.clock[0])
        self.h.call('health_adult_confirm', {}, True)
        self.conn = self.h.call('health_connection_add', {'provider': 'clinic_web', 'institution': 'Midtown Dermatology', 'environment': 'fake'}, True)
        p = self.h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Derm', 'actions': ['appointment.search', 'appointment.book'],
                                                  'constraints': {'clinician': 'Dr. Rivera', 'time_windows': [{'from': '2027-01-01', 'to': '2027-02-01'}]}, 'disclosures': ['name']})
        self.grant = self.h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_visit_packet_lists_logistics_and_his_own_notes(self):
        e = self.h.call('health_effect_begin', {'request_id': 'book-derm-0001', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': self.grant['id'], 'payload': self.BOOK})
        v = self.h.call('health_visit_packet', {}, True)['visits']
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0]['date'], v[0]['clinician'], v[0]['institution']), ('2027-01-12', 'Dr. Rivera', 'Midtown Dermatology'))
        self.assertIn('Insurance card', v[0]['bring'])
        out = self.h.call('health_visit_note', {'effect': e['id'], 'questions': 'Is the follow-up still needed?\nCan I get the results online?', 'bring': ['Photo ID']}, True)
        self.assertEqual(out['visits'][0]['questions'], ['Is the follow-up still needed?', 'Can I get the results online?'])
        self.assertEqual(out['visits'][0]['bring'], ['Photo ID'])
        with self.assertRaisesRegex(ValueError, 'dashboard'):
            self.h.call('health_visit_packet', {}, False)
        with self.assertRaises(ValueError):
            self.h.call('health_visit_note', {'effect': e['id'], 'questions': 'my password is hunter2'}, True)
        self.clock[0] += 30 * 86400   # after the visit: no longer upcoming
        self.assertEqual(self.h.call('health_visit_packet', {}, True)['visits'], [])

    def test_digest_is_counts_only(self):
        self.h.call('health_effect_begin', {'request_id': 'book-derm-0002', 'action': 'appointment.book', 'connection': self.conn['id'], 'grant': self.grant['id'], 'payload': self.BOOK})
        d = self.h.call('health_digest', {})
        self.assertEqual(d['appointments_next_7_days'], 1)
        self.assertEqual(d['line'], 'Health: 1 item needs you — open the dashboard Health tab.')
        self.assertNotIn('Rivera', str(d))
