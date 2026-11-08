"""Apple Health companion, server side (web 0.23.0; build-plans/health/05 acceptance tests that the server can check)."""
import json
import tempfile
import unittest

from health import Health
from health_nutrition import FDC, Nutrition
from health_sync import HealthSync, SyncError

NOW = 1_794_200_000.0



def ready(h):
    """0.25.0: the owner confirmed 18+ (/healthadult) and allowed stored-data reads (/healthread) — the tests below exercise
    the health features themselves; test_health_gates.py covers the gates."""
    h.call('health_adult_confirm', {}, True)
    for c in ('activity', 'nutrition', 'appointments'):
        h.call('health_read_allow', {'category': c, 'days': 365}, True)
    return h

class Quiet:
    def send_with_buttons(self, *a):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [NOW]
        self.h = Health(Quiet(), self.tmp.name, now=lambda: self.clock[0])
        ready(self.h)
        self.n = self.h.attach('health_nutrition_', Nutrition(self.h, FDC(None, key='')))
        self.s = self.h.attach('health_sync_', HealthSync(self.h, self.n, 'PROCURATOR (test)', 'https://assistant.example.com'))

    def tearDown(self):
        self.tmp.cleanup()

    def paired(self, types=None, name='The owner iPhone'):
        code = self.h.call('health_sync_pair_code', {'types': types} if types else {}, True)['code']
        return self.s.pair(json.dumps({'code': code, 'device_name': name}).encode())

    def send(self, dev, batch_id, seq, samples=(), deletions=(), statistics=()):
        body = json.dumps({'version': 1, 'batch_id': batch_id, 'sequence': seq, 'samples': list(samples), 'deletions': list(deletions), 'statistics': list(statistics)}).encode()
        return self.s.sync({'Authorization': 'Bearer ' + dev['credential']}, body)

    @staticmethod
    def weight(uuid, value, unit='kg', when='2026-11-07T07:30:00-05:00'):
        return {'type': 'body_mass', 'source_id': uuid, 'start': when, 'end': when, 'tz': 'America/New_York', 'value': value, 'unit': unit, 'source_app': 'Health', 'source_device': 'Scale'}

    def weights(self):
        with self.h.db() as db:
            return [dict(r) for r in db.execute("SELECT value,unit,source,source_id,deleted FROM measurements WHERE kind='weight' ORDER BY measured_at")]


class PairingTests(Base):
    def test_codes_are_single_use_short_lived_and_owner_only(self):
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_sync_pair_code', {})
        code = self.h.call('health_sync_pair_code', {'types': 'body_mass,step_count'}, True)
        dev = self.s.pair(json.dumps({'code': code['code'].lower(), 'device_name': 'iPhone'}).encode())
        self.assertEqual((dev['installation'], dev['types']), ('PROCURATOR (test)', ['body_mass', 'step_count']))
        self.assertTrue(dev['credential'].startswith('phd_'))
        with self.assertRaises(SyncError) as e:
            self.s.pair(json.dumps({'code': code['code']}).encode())
        self.assertEqual(e.exception.kind, 'bad_code')
        late = self.h.call('health_sync_pair_code', {}, True)['code']
        self.clock[0] += 601
        with self.assertRaises(SyncError):
            self.s.pair(json.dumps({'code': late}).encode())
        blob = json.dumps(self.h.call('health_sync_devices', {}))
        self.assertNotIn(dev['credential'], blob)
        with self.h.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM sync_codes WHERE code_hash=?', (code['code'],)).fetchone())   # only hashes

    def test_guessing_codes_cancels_open_codes(self):
        good = self.h.call('health_sync_pair_code', {}, True)['code']
        for _ in range(5):
            with self.assertRaises(SyncError):
                self.s.pair(b'{"code":"AAAA-AAAA"}')
        with self.assertRaises(SyncError) as e:
            self.s.pair(json.dumps({'code': good}).encode())
        self.assertEqual(e.exception.kind, 'too_many_attempts')

    def test_revoked_and_expired_credentials_stop_uploads(self):
        dev = self.paired()
        self.h.call('health_sync_revoke', {'device': dev['device_id']}, True)
        with self.assertRaises(SyncError) as e:
            self.send(dev, 'batch-0001', 1, [self.weight('AAAA-1111-2222', 80)])
        self.assertEqual(e.exception.kind, 'revoked')
        dev2 = self.paired()
        self.clock[0] += 366 * 86400
        with self.assertRaises(SyncError) as e:
            self.send(dev2, 'batch-0001', 1, [])
        self.assertEqual(e.exception.kind, 'expired')
        with self.assertRaises(SyncError) as e:
            self.s.sync({'Authorization': 'Bearer phd_forged'}, b'{}')
        self.assertEqual(e.exception.kind, 'unknown_device')


class SyncTests(Base):
    def test_batches_are_idempotent_and_samples_upsert_by_uuid(self):
        dev = self.paired()
        ack = self.send(dev, 'batch-0001', 1, [self.weight('AAAA-1111-2222', 180.0, 'lb')])
        self.assertEqual((ack['accepted'], ack['ack']), (1, True))
        again = self.send(dev, 'batch-0001', 1, [self.weight('AAAA-1111-2222', 180.0, 'lb')])
        self.assertTrue(again['duplicate'])
        with self.assertRaises(SyncError) as e:
            self.send(dev, 'batch-0001', 1, [self.weight('AAAA-1111-2222', 181.0, 'lb')])
        self.assertEqual(e.exception.kind, 'batch_id_reused')
        self.send(dev, 'batch-0002', 2, [self.weight('AAAA-1111-2222', 179.5, 'lb')])          # an edit in Health
        w = self.weights()
        self.assertEqual(len(w), 1); self.assertEqual((w[0]['value'], w[0]['unit'], w[0]['source']), (179.5, 'lb', 'apple_health'))

    def test_reinstall_and_a_second_device_do_not_duplicate_history(self):
        a = self.paired(name='iPhone')
        self.send(a, 'batch-a-0001', 1, [self.weight('UUID-0000-0001', 80.0), self.weight('UUID-0000-0002', 79.6, when='2026-11-08T07:30:00-05:00')])
        b = self.paired(name='iPhone (reinstalled)')
        ack = self.send(b, 'batch-b-0001', 1, [self.weight('UUID-0000-0001', 80.0), self.weight('UUID-0000-0002', 79.6, when='2026-11-08T07:30:00-05:00')])
        self.assertEqual(ack['accepted'], 2)
        self.assertEqual(len(self.weights()), 2)

    def test_deletions_propagate_and_an_older_resend_stays_deleted(self):
        dev = self.paired()
        self.send(dev, 'batch-0001', 1, [self.weight('UUID-DEL-0001', 82.0)])
        self.assertEqual(self.send(dev, 'batch-0003', 3, deletions=[{'type': 'body_mass', 'source_id': 'UUID-DEL-0001'}])['deleted'], 1)
        late = self.send(dev, 'batch-0002', 2, [self.weight('UUID-DEL-0001', 82.0)])           # delivered out of order
        self.assertEqual((late['accepted'], late['stale_after_delete']), (0, 1))
        self.assertEqual([w['deleted'] for w in self.weights()], [1])
        back = self.send(dev, 'batch-0004', 4, [self.weight('UUID-DEL-0001', 82.0)])           # restored later in Health
        self.assertEqual(back['accepted'], 1); self.assertEqual([w['deleted'] for w in self.weights()], [0])

    def test_unshared_types_bad_values_and_manual_entries(self):
        dev = self.paired(types='body_mass')
        self.n.add_measurement({'kind': 'weight', 'value': 81, 'unit': 'kg', 'measured_at': '2026-11-07'})
        ack = self.send(dev, 'batch-0001', 1, [self.weight('UUID-BAD-0001', 900), {'type': 'height', 'source_id': 'UUID-H-0001', 'start': '2026-11-01', 'value': 180, 'unit': 'cm'},
                                                self.weight('UUID-OK-0001', 80.5)],
                        statistics=[{'type': 'step_count', 'date': '2026-11-07', 'value': 9000, 'unit': 'count'}])
        self.assertEqual(ack['accepted'], 1)
        whys = ' '.join(i['why'] for i in ack['ignored'])
        self.assertIn('plausible', whys); self.assertIn('not shared', whys)
        self.assertEqual(sorted(w['source'] for w in self.weights()), ['apple_health', 'manual'])     # the manual entry is kept beside it

    def test_daily_statistics_are_not_summed_from_raw_samples_and_energy_is_never_added_to_the_target(self):
        dev = self.paired()
        st = [{'type': 'step_count', 'date': '2026-11-07', 'tz': 'America/New_York', 'value': 8421, 'unit': 'count'},
              {'type': 'active_energy', 'date': '2026-11-07', 'value': 2092, 'unit': 'kJ'}]
        self.assertEqual(self.send(dev, 'batch-0001', 1, statistics=st)['statistics'], 2)
        self.send(dev, 'batch-0002', 2, statistics=[dict(st[0], value=9050)])          # the day's total grew
        day = self.h.call('health_sync_activity', {'days': 3, 'to': '2026-11-08'})
        self.assertEqual(day['days']['2026-11-07']['step_count']['value'], 9050)
        self.assertEqual(day['days']['2026-11-07']['active_energy'], {'value': 500.0, 'unit': 'kcal', 'device': dev['device_id']})
        self.assertIn('NOT added', day['note'])

    def test_weights_from_health_feed_the_estimate_with_their_source(self):
        dev = self.paired()
        self.n.set_profile({'birth_date': '1996-03-01', 'sex_for_equation': 'male', 'activity': 'moderate'})
        self.send(dev, 'batch-0001', 1, [self.weight('UUID-EST-0001', 80.0), {'type': 'height', 'source_id': 'UUID-EST-0002', 'start': '2026-10-01', 'value': 180, 'unit': 'cm'}])
        est = self.h.call('health_nutrition_estimate', {})
        self.assertEqual(est['estimate']['resting_kcal'], 1780); self.assertIn('apple_health', est['inputs']['weight'])

    def test_disconnect_can_delete_what_the_device_sent(self):
        dev = self.paired()
        self.send(dev, 'batch-0001', 1, [self.weight('UUID-RM-0001', 80.0)], statistics=[{'type': 'step_count', 'date': '2026-11-07', 'value': 5000, 'unit': 'count'}])
        out = self.h.call('health_sync_revoke', {'device': dev['device_id']}, True)   # 0.25.0 (H4): deleting is the default
        self.assertEqual(out['deleted_records'], 2)
        self.assertFalse(out['kept_data'])
        self.assertEqual(self.h.call('health_sync_activity', {'to': '2026-11-08'})['days'], {})
        self.assertEqual(self.h.call('health_sync_devices', {})['devices'][0]['status'], 'revoked')

    def test_disconnect_can_keep_the_data_when_asked(self):
        dev = self.paired()
        self.send(dev, 'batch-0001', 1, [], statistics=[{'type': 'step_count', 'date': '2026-11-07', 'value': 5000, 'unit': 'count'}])
        out = self.h.call('health_sync_revoke', {'device': dev['device_id'], 'keep_data': True}, True)
        self.assertEqual((out['deleted_records'], out['kept_data']), (0, True))
        self.assertIn('2026-11-07', self.h.call('health_sync_activity', {'to': '2026-11-08'})['days'])

    def test_malformed_envelopes(self):
        dev = self.paired()
        for body, kind in ((b'nope', 'bad_json'), (b'{"version":2}', 'unsupported_version'), (b'{"version":1,"batch_id":"x"}', 'bad_batch_id'),
                           (b'{"version":1,"batch_id":"batch-0009"}', 'bad_sequence')):
            with self.assertRaises(SyncError) as e:
                self.s.sync({'Authorization': 'Bearer ' + dev['credential']}, body)
            self.assertEqual(e.exception.kind, kind)


class DashboardTests(Base):
    def test_the_dashboard_routes_pairing_and_sync_without_a_session(self):
        import dashboard

        class B:
            health = self.h
            class provider:
                sent = []
                @staticmethod
                def telegram(m):
                    B.provider.sent.append(m)
        code = self.h.call('health_sync_pair_code', {}, True)['code']
        st, out = dashboard.health_post(B, '/health/pair', {}, json.dumps({'code': code, 'device_name': 'iPhone'}).encode())
        self.assertEqual(st, 200)
        body = json.dumps({'version': 1, 'batch_id': 'batch-dash-1', 'sequence': 1, 'samples': [self.weight('UUID-DASH-0001', 80)]}).encode()
        st, ack = dashboard.health_post(B, '/health/sync', {'Authorization': 'Bearer ' + out['credential']}, body)
        self.assertEqual((st, ack['accepted']), (200, 1))
        self.assertEqual(dashboard.health_post(B, '/health/sync', {}, body)[0], 401)
        self.assertEqual(dashboard.health_post(B, '/health/rx-callback/0123456789abcdef', {}, b'{}')[0], 503)    # no pharmacy module here
        self.assertEqual(dashboard.health_post(B, '/health/other', {}, b'')[0], 404)


if __name__ == '__main__':
    unittest.main()
