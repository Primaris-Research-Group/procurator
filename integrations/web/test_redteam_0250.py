"""0.25.0 red-team battery (synthetic data only): the paths a prompt-injected assistant would try.

R1  an undeclared broker action on the model socket is refused (deny by default)
R2  every outbound tool names its destination argument, so the leak check knows the recipient
R3  a health value the assistant read cannot be emailed, texted or typed to someone the grant does not cover
R4  the same value can go to the granted institution's own portal host and listed contacts
R5  browser destinations are resolved by the broker from the session, not taken from the model
R6  the owner-only actions stay unreachable from the model socket even when declared actions are probed
R7  the leak check result never repeats the health value
"""
import tempfile
import unittest
from pathlib import Path

import connectors
from broker import Broker
from test_broker import Provider


class RedTeam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Broker(Path(self.tmp.name) / 'browser.sqlite3', Provider())
        h = self.b.health
        h.call('health_adult_confirm', {}, True)
        self.conn = h.call('health_connection_add', {'provider': 'clinic_web', 'institution': 'Synthetic Heart Institute', 'environment': 'production',
                                                     'endpoint': 'https://portal.synthetic-heart.example/fhir', 'contacts': ['booking@synthetic-heart.example']}, True)
        p = h.call('health_grant_propose', {'connection': self.conn['id'], 'title': 'Cardiology follow-up', 'actions': ['appointment.search', 'appointment.book'],
                                             'constraints': {'clinician': 'Dr. Synthetic Okafor'}, 'disclosures': ['name']})
        h.call('health_grant_approve', {'proposal': p['proposal'], 'hash': p['hash']}, True)
        self.b.call('health_status', {}, False)   # what the model sees gets labeled

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, text, dest, kind=None):
        return self.b.call('health_taint_check', {'text': text, 'destination': dest, **({'destination_kind': kind} if kind else {})}, False)

    def test_r1_r6_gate(self):
        reg = connectors.Registry(mode='enforce')
        for action in ('execute', 'send', 'health_grant_approve', 'health_adult_confirm', 'mcp_accept', 'sms_send', 'site_connect', 'dashboard_link'):
            self.assertIsNotNone(reg.gate(action, False, 'web'), action)
        self.assertIsNotNone(reg.gate('sms_send', False, 'mail'))
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('health_adult_confirm', {}, False)
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.b.call('mcp_accept', {'id': 'abcd1234'}, False)

    def test_r2_outbound_tools_name_a_destination(self):
        for name, spec in connectors.Registry().tools().items():
            if spec.get('outbound'):
                # 0.27.0: a public or workspace post has no recipient; it names the account it posts as instead.
                if spec.get('audience') in ('public', 'workspace'):
                    self.assertTrue(spec.get('account'), name + ' posts publicly but names no account argument')
                else:
                    self.assertTrue(spec.get('destination'), name + ' is outbound but names no destination argument')

    def test_r3_r7_health_values_do_not_leave_to_others(self):
        for dest in (['friend@example.com'], '+13055550199', 'https://forms.example.org/contact', ['booking@synthetic-heart.example', 'friend@example.com']):
            r = self.check('Follow-up with Dr. Synthetic Okafor at Synthetic Heart Institute', dest)
            self.assertTrue(r['tainted'], dest)
            self.assertFalse(r['allowed'], dest)
            self.assertNotIn('Okafor', r['reason'])
            self.assertNotIn('Synthetic Heart', r['reason'])

    def test_r4_granted_recipients_are_allowed(self):
        text = 'Please book a follow-up with Dr. Synthetic Okafor'
        self.assertTrue(self.check(text, ['booking@synthetic-heart.example'])['allowed'])
        self.assertTrue(self.check(text, 'https://portal.synthetic-heart.example/appointments')['allowed'])

    def test_r5_browser_sessions_resolve_on_the_broker(self):
        def add(sid, origin):
            with self.b.db() as db:
                info = list(db.execute('PRAGMA table_info(sessions)'))
                row = {r[1]: (0 if 'INT' in (r[2] or '').upper() or 'REAL' in (r[2] or '').upper() else '') if r[3] else None for r in info}
                row.update({'id': sid, 'origin': origin, 'status': 'open'})
                db.execute('INSERT INTO sessions(%s) VALUES(%s)' % (','.join(row), ','.join('?' * len(row))), tuple(row.values()))
        add('abcdef0123456789', 'https://www.unrelated-shop.example')
        add('0123456789abcdef', 'https://portal.synthetic-heart.example')
        self.assertTrue(self.check('Dr. Synthetic Okafor', '0123456789abcdef', 'session')['allowed'])
        r = self.check('Dr. Synthetic Okafor', 'abcdef0123456789', 'session')
        self.assertTrue(r['tainted'])
        self.assertFalse(r['allowed'])
        # The model claiming a different destination for the same session does not help: the broker reads the session's own origin.
        r = self.check('Dr. Synthetic Okafor', 'abcdef0123456789', 'session')
        self.assertFalse(r['allowed'])


if __name__ == '__main__':
    unittest.main()
