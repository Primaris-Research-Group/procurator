#!/usr/bin/env python3
"""Privately verify an existing Twilio voice number. Never buy, route, or call."""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings


DIRECTORY = Path('/var/lib/procurator-voice')
NUMBER = '+15550100100'
MAX_RESPONSE_BYTES = 131072


class SetupError(RuntimeError):
    """Only fixed, credential-free messages may cross the CLI boundary."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to another endpoint.
        return None


def read_hidden(label, pattern, expected):
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                # Fail instead of falling back to echoed secret input on a broken TTY.
                warnings.simplefilter('error', getpass.GetPassWarning)
                value = getpass.getpass(label + ' (hidden; paste, then press Enter): ').strip()
        except getpass.GetPassWarning:
            raise SetupError('This console cannot hide input safely. Reopen the droplet console or use an SSH terminal.') from None
        if re.fullmatch(pattern, value):
            return value
        if not value:
            print('No text was received. Paste again and press Enter; hidden text will not appear.', flush=True)
        else:
            print('Received ' + str(len(value)) + ' characters; expected ' + expected
                  + '. The entered value was not displayed.', flush=True)
    raise SetupError('Input was not recognized after three attempts. Recopy the matching dashboard field and rerun setup.')


def validate_credentials(sid, token):
    if not isinstance(sid, str) or not re.fullmatch(r'AC[0-9a-fA-F]{32}', sid):
        raise SetupError('The Account SID format was not recognized. Copy the Account SID beginning with AC from your Twilio dashboard.')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', token):
        raise SetupError('The Auth Token format was not recognized. Copy the account Auth Token from your Twilio dashboard, not an API key SID.')


def verify_number(sid, token):
    validate_credentials(sid, token)
    query = urllib.parse.urlencode({'PhoneNumber': NUMBER, 'PageSize': 20})
    url = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json?{query}'
    auth = base64.b64encode(f'{sid}:{token}'.encode()).decode('ascii')
    request = urllib.request.Request(url, headers={
        'Authorization': 'Basic ' + auth,
        'Accept': 'application/json',
        'User-Agent': 'ProcuratorAssistant-TwilioSetup/0.1',
    }, method='GET')
    try:
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(request, timeout=25) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()
        result = json.loads(raw)
        matches = result.get('incoming_phone_numbers')
        if not isinstance(matches, list):
            raise ValueError()
        if len(matches) == 0:
            raise SetupError('Twilio accepted authentication but did not return the selected phone number. '
                             'Copy credentials from the same Twilio account that owns +15550100100.')
        if len(matches) != 1:
            raise ValueError()
        number = matches[0]
        if (number.get('phone_number') != NUMBER or number.get('account_sid') != sid
                or not re.fullmatch(r'PN[0-9a-fA-F]{32}', number.get('sid', ''))):
            raise ValueError()
        if number.get('capabilities', {}).get('voice') is not True:
            raise SetupError('Twilio returned the selected number without Voice capability. '
                             'Check that Voice is enabled for this number in Twilio.')
        return number['sid']
    except SetupError:
        raise
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 401:
            raise SetupError('Twilio rejected authentication (HTTP 401). Recopy the Account SID and Auth Token '
                             'from the same Twilio account dashboard.') from None
        if code == 403:
            raise SetupError('Twilio denied number access (HTTP 403). Check account access and restrictions in Twilio.') from None
        raise SetupError('Twilio returned an unsuccessful HTTP response. Existing credentials were preserved.') from None
    except (urllib.error.URLError, TimeoutError):
        raise SetupError('The server could not complete its HTTPS connection to Twilio. '
                         'This can be a network, TLS, or timeout error; credentials were not saved.') from None
    except Exception as error:
        # Provider errors may include request details. Never echo them or secrets.
        raise SetupError('Twilio returned an unexpected number-verification response; '
                         'existing credentials were preserved.') from None


def check_private_path(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SetupError('Voice credential storage has unexpected ownership or permissions; no change made.')


def save_credentials(sid, token, number_sid):
    DIRECTORY.mkdir(parents=True, mode=0o700, exist_ok=True)
    check_private_path(DIRECTORY, directory=True)
    target = DIRECTORY / 'twilio.json'
    if target.exists() or target.is_symlink():
        check_private_path(target)
    fd, temporary = tempfile.mkstemp(prefix='.twilio-', dir=DIRECTORY)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'account_sid': sid, 'auth_token': token, 'from_number': NUMBER,
                       'incoming_phone_number_sid': number_sid,
                       'verified_at': datetime.now(timezone.utc).isoformat()}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read-only verification of saved credentials')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SetupError('Run this in the root droplet console.')
    if args.check:
        check_private_path(DIRECTORY, directory=True)
        path = DIRECTORY / 'twilio.json'
        check_private_path(path)
        saved = json.loads(path.read_text())
        if saved.get('from_number') != NUMBER:
            raise SetupError('Saved number differs from the selected assistant number; no change made.')
        sid, token = saved.get('account_sid'), saved.get('auth_token')
    else:
        if not sys.stdin.isatty():
            raise SetupError('Use an interactive root droplet console so credentials can be entered hidden.')
        print('Connect existing Twilio voice number: ' + NUMBER)
        print('This checks account access only. It does not buy numbers, change webhooks, or place calls.')
        print('Hidden prompts show no characters or dots. Press Enter after each paste.', flush=True)
        sid = read_hidden('Twilio Account SID', r'AC[0-9a-fA-F]{32}', '34 characters beginning with AC')
        print('Account SID format accepted. Now copy the Auth Token from the same dashboard.', flush=True)
        token = read_hidden('Twilio Auth Token', r'[0-9a-fA-F]{32}', '32 hexadecimal characters')
        print('Auth Token format accepted.', flush=True)
    validate_credentials(sid, token)
    print('Checking the number with Twilio (connection timeout: 25 seconds)...', flush=True)
    number_sid = verify_number(sid, token)
    if not args.check:
        save_credentials(sid, token, number_sid)
    print('Twilio account access and voice number ownership verified.'
          + (' Credentials saved privately.' if not args.check else ' Saved credentials unchanged.'))
    print('Phone service is not enabled yet. Return to Codex and say: Twilio connected.')


def run():
    try:
        main()
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\nTwilio setup cancelled; existing settings were preserved.', file=sys.stderr)
    except SetupError as error:
        print('Twilio connection was not completed. ' + str(error)
              + ' No calls or routing changes were made.', file=sys.stderr)
    except (RuntimeError, OSError, ValueError):
        print('Twilio connection was not completed. Check the account credentials and private storage; '
              'existing settings were preserved. No calls or routing changes were made.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(run())
