#!/usr/bin/env python3
"""Connect iCloud IMAP using a hidden Apple app-specific password. Never SMTP."""
import getpass
import imaplib
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile

ACCOUNT = 'owner@icloud.example'

def main():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise RuntimeError('Run this in your interactive root droplet console.')
    print('Connect iCloud inbox: ' + ACCOUNT)
    print('Use an Apple app-specific password, not your normal Apple Account password.')
    print('This enables reading iCloud mail. Sending is not available on this DigitalOcean host.')
    password = getpass.getpass('Apple app-specific password (hidden): ').strip()
    if not password:
        raise RuntimeError('No password entered; connection unchanged.')
    # Apple recommends trying the full address if the local username is rejected.
    username = ACCOUNT
    try:
        with imaplib.IMAP4_SSL('imap.mail.me.com',993,ssl_context=ssl.create_default_context(),timeout=20) as client:
            client.login(username,password)
            if client.select('INBOX',readonly=True)[0] != 'OK':
                raise RuntimeError()
    except Exception:
        raise RuntimeError('iCloud did not confirm inbox access. Verify the app-specific password; nothing was changed.') from None
    directory = Path('/var/lib/procurator-mail')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.icloud-',dir=directory)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as output:
            json.dump({'email':ACCOUNT,'username':username,'password':password},output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,directory/'icloud.json')
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('iCloud inbox access verified and credentials saved privately. Email sending remains disabled for iCloud.')

if __name__=='__main__':
    try:
        main()
    except (KeyboardInterrupt,EOFError):
        print('\niCloud sign-in cancelled.',file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
