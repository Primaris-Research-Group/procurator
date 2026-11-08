#!/usr/bin/env python3
"""Set only the approved test line's voice webhook/status callback. Never call."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path('/opt/procurator')
STATE = Path('/var/lib/procurator-voice')
URL = 'https://assistant.example.com/assistant/voice/webhook'
FIELDS = {'voice_url': URL, 'voice_method': 'POST', 'status_callback': URL+'?type=status', 'status_callback_method': 'POST'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--apply', action='store_true')
    group.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Root console required.')
    spec = importlib.util.spec_from_file_location('connection', ROOT/'bin/connect-twilio.py')
    connection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(connection)
    credentials = json.loads((STATE/'twilio.json').read_text())
    sid, token = credentials['account_sid'], credentials['auth_token']
    connection.validate_credentials(sid, token)
    number_sid = connection.verify_number(sid, token)
    endpoint = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{number_sid}.json'
    opener = urllib.request.build_opener(connection.NoRedirects())
    auth = 'Basic '+base64.b64encode((sid+':'+token).encode()).decode()

    def request(data=None):
        req = urllib.request.Request(endpoint, data=data, headers={'Authorization': auth,
            'Content-Type': 'application/x-www-form-urlencoded'}, method='POST' if data is not None else 'GET')
        with opener.open(req, timeout=25) as response:
            return json.load(response)

    before = request()
    if args.apply:
        if before.get('trunk_sid') or before.get('voice_application_sid'):
            raise RuntimeError('Existing trunk/application routing needs review.')
        probe = urllib.request.Request(URL, data=b'CallSid=invalid', method='POST')
        try:
            with opener.open(probe, timeout=10):
                raise RuntimeError('Unsigned webhook was not rejected.')
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code != 401:
                raise RuntimeError('Expected signed webhook is not ready.') from None
        backup = STATE/'voice-routing-before-test.json'
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump({key: before.get(key) for key in FIELDS}, output)
                output.flush()
                os.fsync(output.fileno())
        data = urllib.parse.urlencode({'VoiceUrl': URL, 'VoiceMethod': 'POST',
               'StatusCallback': URL+'?type=status', 'StatusCallbackMethod': 'POST'}).encode()
        # This writes routing only, never /Calls or phone-number purchase endpoints.
        try:
            request(data)
        except Exception:
            # A timed-out configuration write is reconciled through readback.
            pass
    after = request()
    if any(after.get(key) != value for key, value in FIELDS.items()):
        raise RuntimeError('Routing readback did not match the approved webhook.')
    print('Voice webhook and status callback verified by Twilio readback. No calls placed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Twilio routing was not confirmed. Private provider details were withheld; inspect routing before retrying.') from None
