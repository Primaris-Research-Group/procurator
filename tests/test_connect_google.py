import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('connect_google', Path(__file__).parents[1] / 'bin/connect-google.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
ACCOUNT = 'owner@example.com'
AUTH = 'https://accounts.google.com/o/oauth2/auth?state=test-state&redirect_uri=http%3A%2F%2F127.0.0.1%3A40000%2Foauth2%2Fcallback'
CALLBACK = 'http://127.0.0.1:40000/oauth2/callback?state=test-state&code=private-test-code'


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.google = self.base / 'google'
        self.google.mkdir(mode=0o700)
        (self.google / 'account.json').write_text(json.dumps({'email': ACCOUNT}))
        (self.google / 'keyring-password').write_text('private-test-password')
        self.calls = []
        self.stored = True

    def run_cli(self, command, **kwargs):
        self.calls.append(command)
        if '--step' in command and command[command.index('--step') + 1] == '1':
            result = {'auth_url': AUTH}
        elif '--step' in command:
            result = {'stored': self.stored, 'email': ACCOUNT, 'client': 'procurator-admin', 'services': command[command.index('--services') + 1].split(',')}
        else:
            result = {'accounts': [{'email': ACCOUNT, 'client': 'procurator-admin', 'valid': True}]}
        return type('Result', (), {'returncode': 0, 'stdout': json.dumps(result)})()

    def invoke(self, args, callback=CALLBACK, tty=True):
        output = io.StringIO()
        with patch.object(helper, 'BASE', self.base), patch.object(helper.os, 'geteuid', return_value=0), \
             patch.object(helper.sys, 'argv', ['connect-google.py'] + args), patch.object(helper.sys.stdin, 'isatty', return_value=tty), \
             patch.object(helper.subprocess, 'run', side_effect=self.run_cli), patch.object(helper.getpass, 'getpass', return_value=callback), \
             contextlib.redirect_stdout(output):
            helper.main()
        self.assertNotIn('private-test-code', output.getvalue())
        self.assertNotIn('private-test-password', output.getvalue())
        return output.getvalue()

    def start(self):
        return self.invoke(['--start', '--force-consent'], tty=False)

    def test_start_exits_before_exchange_and_persists_private_resume_state(self):
        output = self.start()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('--finish', output)
        self.assertNotIn('Google sign-in completed', output)
        pending = self.google / 'pending-owner-consent.json'
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.google / 'last-consent-receipt.json').exists())

    def test_finish_resumes_same_force_consent_flow_and_checks_saved_grant(self):
        self.start()
        output = self.invoke(['--finish'])
        exchange = next(c for c in self.calls if '--step' in c and c[c.index('--step') + 1] == '2')
        self.assertIn('--force-consent', exchange)
        self.assertEqual(exchange[exchange.index('--client') + 1], 'procurator-admin')
        self.assertIn('Google sign-in completed', output)
        receipt = self.google / 'last-consent-receipt.json'
        self.assertTrue(json.loads(receipt.read_text())['refresh_check_valid'])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.google / 'pending-owner-consent.json').exists())

    def test_old_callback_does_not_exchange_or_destroy_resume_state(self):
        self.start()
        with self.assertRaisesRegex(ValueError, 'different sign-in attempt'):
            self.invoke(['--finish'], callback=CALLBACK.replace('test-state', 'old-state'))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.google / 'pending-owner-consent.json').exists())

    def test_false_store_confirmation_never_reports_success_or_receipt(self):
        self.start()
        self.stored = False
        with self.assertRaisesRegex(RuntimeError, 'did not confirm'):
            self.invoke(['--finish'])
        self.assertFalse((self.google / 'last-consent-receipt.json').exists())
        self.assertTrue((self.google / 'pending-owner-consent.json').exists())

    def test_send_permission_is_explicit_and_survives_split_flow(self):
        self.invoke(['--start', '--force-consent', '--gmail-send'], tty=False)
        self.invoke(['--finish'], callback=CALLBACK+'&scope=https://www.googleapis.com/auth/gmail.send')
        exchanges=[c for c in self.calls if '--step' in c]
        for command in exchanges:
            self.assertEqual(command[command.index('--extra-scopes')+1], 'https://www.googleapis.com/auth/gmail.send')
            self.assertEqual(command[command.index('--gmail-scope')+1], 'readonly')
        self.assertTrue(json.loads((self.google/'last-consent-receipt.json').read_text())['gmail_send'])

    def test_send_permission_must_be_granted_in_callback(self):
        self.invoke(['--start', '--force-consent', '--gmail-send'], tty=False)
        with self.assertRaisesRegex(ValueError,'did not grant Gmail sending'):
            self.invoke(['--finish'])
        self.assertFalse((self.google/'last-consent-receipt.json').exists())
        self.assertEqual(len(self.calls),1)

    def test_all_requests_calendar_tasks_send_and_modify_in_one_consent(self):
        out = self.invoke(['--start', '--force-consent', '--all'], tty=False)
        self.assertIn('Google Calendar and Tasks are requested', out)
        scopes = '%20'.join(['https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/gmail.modify',
                             'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/tasks', 'https://www.googleapis.com/auth/contacts'])
        self.assertIn('only SEARCHES your contacts', out)   # 0.25.0: --all includes Contacts
        with self.assertRaisesRegex(ValueError, 'did not grant Calendar and Tasks'):
            self.invoke(['--finish'], callback=CALLBACK + '&scope=https://www.googleapis.com/auth/gmail.send%20https://www.googleapis.com/auth/gmail.modify')
        self.invoke(['--finish'], callback=CALLBACK + '&scope=' + scopes)
        exchange = [c for c in self.calls if '--step' in c][-1]
        self.assertEqual(exchange[exchange.index('--services') + 1], 'gmail,drive,docs,calendar,tasks,contacts')
        receipt = json.loads((self.google / 'last-consent-receipt.json').read_text())
        self.assertTrue(receipt['calendar'] and receipt['gmail_modify'] and receipt['gmail_send'] and receipt['contacts'])

    def test_finish_requires_a_prepared_sign_in(self):
        with self.assertRaisesRegex(RuntimeError, 'No prepared sign-in'):
            self.invoke(['--finish'])
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
