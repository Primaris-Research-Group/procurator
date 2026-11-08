import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error


SPEC = importlib.util.spec_from_file_location('connect_twilio', Path(__file__).parents[1] / 'bin/connect-twilio.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
SID, TOKEN, PHONE_SID = 'AC' + 'a' * 32, 'b' * 32, 'PN' + 'c' * 32


class Response(io.BytesIO):
    pass


class TwilioSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'voice'
        self.result = {'incoming_phone_numbers': [{'account_sid': SID, 'phone_number': helper.NUMBER,
                        'sid': PHONE_SID, 'capabilities': {'voice': True}}]}
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return Response(json.dumps(self.result).encode())

    def invoke(self, args=(), tty=True):
        output = io.StringIO()
        with patch.object(helper, 'DIRECTORY', self.directory), \
             patch.object(helper.os, 'geteuid', return_value=os.getuid()), \
             patch.object(helper.sys, 'argv', ['connect-twilio.py', *args]), \
             patch.object(helper.sys.stdin, 'isatty', return_value=tty), \
             patch.object(helper.getpass, 'getpass', side_effect=[SID, TOKEN]), \
             patch.object(helper.urllib.request.OpenerDirector, 'open', side_effect=self.open), \
             contextlib.redirect_stdout(output):
            # main requires root; verify with a UID matching this test's files.
            with patch.object(helper.os, 'geteuid', return_value=0), \
                 patch.object(helper, 'check_private_path', side_effect=self.check_private):
                helper.main()
        self.assertNotIn(TOKEN, output.getvalue())
        return output.getvalue()

    def check_private(self, path, directory=False):
        with patch.object(helper.os, 'geteuid', return_value=os.getuid()):
            ORIGINAL_CHECK(path, directory)

    def test_hidden_setup_verifies_exact_number_using_only_get_and_saves_private(self):
        output = self.invoke()
        saved = self.directory / 'twilio.json'
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(saved.read_text())['incoming_phone_number_sid'], PHONE_SID)
        request = self.requests[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIn('PhoneNumber=%2B15550100100', request.full_url)
        self.assertTrue(request.full_url.startswith('https://api.twilio.com/2010-04-01/Accounts/' + SID))
        self.assertNotIn(TOKEN, request.full_url)
        self.assertIn('Phone service is not enabled', output)

    def test_check_never_rewrites_credentials(self):
        self.invoke()
        saved = self.directory / 'twilio.json'
        before = saved.read_bytes()
        self.invoke(['--check'], tty=False)
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(len(self.requests), 2)

    def test_rejected_credentials_preserve_previous_file_and_withhold_details(self):
        self.invoke()
        saved = self.directory / 'twilio.json'
        before = saved.read_bytes()
        def rejected(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, TOKEN, {}, None)
        self.open = rejected
        with self.assertRaises(RuntimeError) as error:
            self.invoke()
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertEqual(saved.read_bytes(), before)

    def test_wrong_account_number_or_voice_capability_does_not_save(self):
        for field, value in [('account_sid', 'AC' + 'd' * 32), ('phone_number', '+15555550123'),
                             ('capabilities', {'voice': False}), ('sid', 'not-a-sid')]:
            with self.subTest(field=field):
                original = self.result['incoming_phone_numbers'][0][field]
                self.result['incoming_phone_numbers'][0][field] = value
                with self.assertRaises(RuntimeError):
                    self.invoke()
                self.assertFalse(self.directory.exists())
                self.result['incoming_phone_numbers'][0][field] = original

    def test_malformed_credentials_never_reach_network(self):
        with patch.object(helper.urllib.request, 'build_opener') as network:
            for sid, token in [('SK' + 'a' * 32, TOKEN), (SID, TOKEN + '\nleak')]:
                with self.assertRaises(RuntimeError):
                    helper.verify_number(sid, token)
            network.assert_not_called()

    def test_auth_rejection_has_safe_actionable_message(self):
        def rejected(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, TOKEN, {}, None)
        self.open = rejected
        with self.assertRaisesRegex(helper.SetupError, 'HTTP 401') as error:
            self.invoke()
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertFalse(self.directory.exists())

    def test_no_owned_number_is_distinct_from_invalid_authentication(self):
        self.result['incoming_phone_numbers'] = []
        with self.assertRaisesRegex(helper.SetupError, 'accepted authentication'):
            self.invoke()
        self.assertFalse(self.directory.exists())

    def test_network_failure_withholds_underlying_exception(self):
        def rejected(request, timeout):
            raise urllib.error.URLError(TOKEN)
        self.open = rejected
        with self.assertRaisesRegex(helper.SetupError, 'HTTPS connection') as error:
            self.invoke()
        self.assertNotIn(TOKEN, str(error.exception))

    def test_cli_reports_only_curated_errors_and_never_raw_exception_text(self):
        for error, expected in [(helper.SetupError('Copy the Account SID.'), 'Copy the Account SID.'),
                                (RuntimeError(TOKEN), 'private storage'), (ValueError(TOKEN), 'private storage')]:
            output = io.StringIO()
            with patch.object(helper, 'main', side_effect=error), contextlib.redirect_stderr(output):
                self.assertEqual(helper.run(), 1)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())

    def test_interactive_entry_rejects_pipe(self):
        with self.assertRaisesRegex(RuntimeError, 'interactive'):
            self.invoke(tty=False)
        self.assertFalse(self.requests)

    def test_hidden_entry_reprompts_empty_or_invalid_paste_without_echo(self):
        output = io.StringIO()
        with patch.object(helper.getpass, 'getpass', side_effect=['', 'wrong-field', SID]), \
             contextlib.redirect_stdout(output):
            value = helper.read_hidden('SID', r'AC[0-9a-fA-F]{32}', '34 characters beginning with AC')
        self.assertEqual(value, SID)
        self.assertIn('No text was received', output.getvalue())
        self.assertIn('Received 11 characters', output.getvalue())
        self.assertNotIn('wrong-field', output.getvalue())
        self.assertNotIn(SID, output.getvalue())

    def test_invalid_hidden_token_is_bounded_and_never_displayed(self):
        output = io.StringIO()
        with patch.object(helper.getpass, 'getpass', return_value='private-invalid-token') as prompt, \
             contextlib.redirect_stdout(output), self.assertRaises(helper.SetupError):
            helper.read_hidden('Token', r'[0-9a-fA-F]{32}', '32 hexadecimal characters')
        self.assertEqual(prompt.call_count, 3)
        self.assertNotIn('private-invalid-token', output.getvalue())

    def test_unsafe_hidden_input_fallback_is_blocked(self):
        def unsafe(prompt):
            import warnings
            warnings.warn('cannot hide', helper.getpass.GetPassWarning)
            self.fail('Echoed input fallback must never run')
        with patch.object(helper.getpass, 'getpass', side_effect=unsafe), \
             self.assertRaisesRegex(helper.SetupError, 'cannot hide input safely'):
            helper.read_hidden('Token', r'[0-9a-fA-F]{32}', '32 hexadecimal characters')

    def test_redirects_cannot_forward_credentials(self):
        self.assertIsNone(helper.NoRedirects().redirect_request(None, None, 302, 'redirect', {}, 'https://other.example/'))

    def test_unsafe_storage_and_symlinks_are_rejected(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(RuntimeError):
            self.check_private(self.directory, directory=True)
        self.directory.chmod(0o700)
        secret = self.directory / 'twilio.json'
        other = self.directory / 'other'
        other.write_text('unchanged')
        other.chmod(0o600)
        secret.symlink_to(other)
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual(other.read_text(), 'unchanged')


ORIGINAL_CHECK = helper.check_private_path

if __name__ == '__main__':
    unittest.main()
