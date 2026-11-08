#!/usr/bin/env python3
"""Owner-console Google consent; credentials and callback never enter chat."""

import argparse
import datetime
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit


BASE = Path('/opt/procurator')


def validate_saved_result(result, account, services):
    if not isinstance(result, dict) or result.get('stored') is not True:
        raise RuntimeError('Google did not confirm that it saved the new authorization.')
    if result.get('email', '').lower() != account.lower() or result.get('client') != 'procurator-admin':
        raise RuntimeError('The saved Google authorization does not match the configured account and client.')
    if set(result.get('services', [])) != set(services.split(',')):
        raise RuntimeError('The saved Google authorization does not match the requested services.')


def validate_callback(auth_url, callback):
    auth = urlsplit(auth_url)
    params = parse_qs(auth.query)
    if auth.scheme != 'https' or auth.hostname != 'accounts.google.com':
        raise ValueError('Unexpected Google authorization address.')
    expected = urlsplit(params['redirect_uri'][0])
    actual = urlsplit(callback)
    if (actual.scheme, actual.netloc, actual.path) != (
        expected.scheme, expected.netloc, expected.path
    ):
        raise ValueError('Paste the final browser address, starting with the expected localhost address.')
    result = parse_qs(actual.query)
    if 'error' in result:
        raise ValueError('Google consent was declined. Run this command again when ready.')
    if len(result.get('state', [])) != 1 or not secrets.compare_digest(
        result['state'][0], params['state'][0]
    ):
        raise ValueError('This browser address belongs to a different sign-in attempt. Run the command again.')
    if len(result.get('code', [])) != 1 or not result['code'][0] or actual.fragment:
        raise ValueError('The browser address does not contain a complete authorization response.')


def save_private(path, data, owner):
    temporary = path.parent / (path.name + '-' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(data, output)
    if owner == 1000:
        os.chown(temporary, 1000, 1000)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Check saved account access without reading messages or files')
    mode.add_argument('--start', action='store_true', help='Print the Google link and exit; finish later using --finish')
    mode.add_argument('--finish', action='store_true', help='Paste the return address for the prepared sign-in')
    parser.add_argument('--force-consent', action='store_true', help='Request fresh consent after changing the Google app publishing status')
    parser.add_argument('--gmail-send', action='store_true', help='Add Gmail send permission for owner-approved assistant drafts')
    parser.add_argument('--calendar', action='store_true', help='Add Google Calendar and Google Tasks (read, add and change your own events/tasks; invites and deletions are owner-approved)')
    parser.add_argument('--contacts', action='store_true', help='Add Google Contacts (0.25.0: the assistant searches your contacts for a name, email or phone; it never changes them)')
    parser.add_argument('--all', action='store_true', help='Everything the assistant uses: Gmail send + modify, Calendar, Tasks and Contacts (one consent)')
    parser.add_argument('--gmail-modify', action='store_true', help='Add Gmail modify permission (mark read/unread, spam, trash; never permanent delete). Implies --gmail-send so sending is kept')
    args = parser.parse_args()
    if args.all:
        args.gmail_modify = args.gmail_send = args.calendar = args.contacts = True
    if (args.check or args.finish) and (args.force_consent or args.gmail_send or args.gmail_modify or args.calendar or args.contacts):
        parser.error('Permission and consent options belong on --start or the normal sign-in command')
    if os.geteuid() != 0:
        raise SystemExit('Run this in your root server console.')
    if not args.check and not args.start and not sys.stdin.isatty():
        raise SystemExit('Use an interactive server console for Google sign-in.')
    google = BASE / 'google'
    account = json.loads((google / 'account.json').read_text())['email']
    env = os.environ.copy()
    env.update(
        GOG_HOME=str(google / 'home'),
        GOG_KEYRING_BACKEND='file',
        GOG_KEYRING_PASSWORD=(google / 'keyring-password').read_text(),
        GOG_CLIENT='procurator-admin',
        GOG_GMAIL_NO_SEND='true',
    )
    command = [str(BASE / 'vendor' / 'gog-0.47.1'), '--home', str(google / 'home'),
               '--client', 'procurator-admin', '--account', account,
               '--json', '--no-input', '--gmail-no-send']
    # Once mounted into the gateway, keep refreshed credentials owned by its UID.
    identity = {'user': 1000, 'group': 1000, 'extra_groups': []} if google.stat().st_uid == 1000 else {}

    def run(arguments):
        process = subprocess.run(command + arguments, env=env, capture_output=True, text=True, timeout=90, **identity)
        if process.returncode:
            # Provider errors can contain authorization URLs or codes.
            raise RuntimeError(f'Google connector did not complete (exit {process.returncode}). Credentials were not displayed.')
        return json.loads(process.stdout)

    if args.check:
        result = run(['auth', 'list', '--check'])
        # Account health contains no message/file data; keep output private anyway.
        path = google / 'last-account-check.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(result, output)
        print('Account check completed; private result saved for the operator.')
        return

    pending_path = google / 'pending-owner-consent.json'
    force_consent = args.force_consent
    gmail_modify = args.gmail_modify
    gmail_send = args.gmail_send or gmail_modify
    calendar = args.calendar
    contacts = args.contacts
    if args.finish:
        if not pending_path.exists():
            raise RuntimeError('No prepared sign-in found. Run this helper with --start --force-consent first.')
        pending = json.loads(pending_path.read_text())
        if pending.get('email') != account or pending.get('client') != 'procurator-admin':
            raise RuntimeError('Prepared sign-in does not match the configured Google account.')
        auth_url = pending['auth_url']
        force_consent = pending.get('force_consent') is True
        gmail_send = pending.get('gmail_send') is True
        gmail_modify = pending.get('gmail_modify') is True
        calendar = pending.get('calendar') is True
        contacts = pending.get('contacts') is True

    services = 'gmail,drive,docs' + (',calendar,tasks' if calendar else '') + (',contacts' if contacts else '')
    flow = ['auth', 'add', account, '--services', services,
            '--gmail-scope', 'readonly', '--remote', '--timeout', '20m']
    if force_consent:
        flow.append('--force-consent')
    extra = (['https://www.googleapis.com/auth/gmail.send'] if gmail_send else []) + (['https://www.googleapis.com/auth/gmail.modify'] if gmail_modify else [])
    if extra:
        flow.extend(['--extra-scopes', ','.join(extra)])
    if not args.finish:
        auth_url = run(flow + ['--step', '1'])['auth_url']
        save_private(pending_path, {
            'auth_url': auth_url, 'email': account, 'client': 'procurator-admin',
            'force_consent': force_consent, 'gmail_send': gmail_send, 'gmail_modify': gmail_modify, 'calendar': calendar, 'contacts': contacts,
            'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, google.stat().st_uid)
    print(f'Connect Google account: {account}')
    print('Requested access: read Gmail; read and edit Drive files and Google Docs.' +
          (' Gmail sending is additionally requested; every assistant send requires exact-content Telegram owner approval.' if gmail_send else ' No new email send permission requested.') +
          (' Gmail modify is requested: mark read/unread, move to spam/inbox and move to Trash (owner-approved). It does not allow permanent deletion.' if gmail_modify else '') +
          (' Google Calendar and Tasks are requested: read your schedule, add/change your own events and to-dos; inviting guests and deleting events need your Telegram approval.' if calendar else '') +
          (' Google Contacts is requested (Google shows it as contacts access): the assistant only SEARCHES your contacts to find a name, email or phone; it never adds, edits or deletes one.' if contacts else ''))
    if not args.finish:
        print('\n1. Open this address in your browser:\n')
        print(auth_url)
        print('\n2. Sign in with the account above and review the Google consent screen.')
        print('3. After consent, a localhost page may fail to load. Copy its FULL address from the browser address bar.')
        if args.start:
            print('\n4. Return to the droplet console and run:')
            print('python3 /opt/procurator/bin/connect-google.py --finish')
            print('Then paste the final browser address at its hidden prompt. Keep that address out of chat and screenshots.')
            print('\nSign-in link prepared. This command has finished; Google access has not been changed yet.')
            return
    print('\nPaste the final browser address below. Input is hidden. Keep it out of chat and screenshots.\n')
    callback = getpass.getpass('Final browser address (hidden): ').strip()
    validate_callback(auth_url, callback)
    granted = parse_qs(urlsplit(callback).query).get('scope', [''])[0].split()
    if gmail_send and 'https://www.googleapis.com/auth/gmail.send' not in granted:
        raise ValueError('The Google response did not grant Gmail sending. Review all requested permissions and retry sign-in.')
    if gmail_modify and 'https://www.googleapis.com/auth/gmail.modify' not in granted:
        raise ValueError('The Google response did not grant Gmail modify. Tick every requested permission and retry sign-in.')
    if calendar and not {'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/tasks'} <= set(granted):
        raise ValueError('The Google response did not grant Calendar and Tasks. Tick every requested permission and retry sign-in.')
    if contacts and 'https://www.googleapis.com/auth/contacts' not in granted:
        raise ValueError('The Google response did not grant Contacts. Tick every requested permission and retry sign-in.')
    saved = run(flow + ['--step', '2', '--auth-url', callback])
    validate_saved_result(saved, account, services)
    checked = run(['auth', 'list', '--check'])
    valid = any(
        item.get('email', '').lower() == account.lower()
        and item.get('client') == 'procurator-admin'
        and item.get('valid') is True
        for item in checked.get('accounts', [])
    )
    if not valid:
        raise RuntimeError('Google saved the authorization, but its connection check did not pass. Ask the operator to inspect it.')
    receipt = {
        'completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'email': account,
        'client': 'procurator-admin',
        'services': sorted(saved['services']),
        'force_consent': force_consent, 'gmail_send': gmail_send, 'gmail_modify': gmail_modify, 'calendar': calendar, 'contacts': contacts,
        'stored': True,
        'refresh_check_valid': True,
    }
    save_private(google / 'last-consent-receipt.json', receipt, google.stat().st_uid)
    if pending_path.exists() and json.loads(pending_path.read_text()).get('auth_url') == auth_url:
        pending_path.unlink()
    print('\nGoogle sign-in completed and credentials saved privately.')
    print('New authorization verified; completion receipt saved.')
    print('Return to Codex and say: Google connected.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        if isinstance(error, (ValueError, RuntimeError)):
            print(str(error), file=sys.stderr)
        else:
            print('Google setup could not complete. No credentials were displayed; ask the operator to check setup.', file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nGoogle sign-in cancelled.', file=sys.stderr)
        sys.exit(1)
