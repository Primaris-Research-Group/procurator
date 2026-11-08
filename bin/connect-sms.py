#!/usr/bin/env python3
"""Turn PROCURATOR text messages on or off on this server (0.25.0). Run in the root server console.

  python3 /opt/procurator/bin/connect-sms.py --status
  python3 /opt/procurator/bin/connect-sms.py --on     (asks you to confirm the number is registered for business texting)
  python3 /opt/procurator/bin/connect-sms.py --off

US carriers block application-to-person texts from numbers that are not registered: register the Twilio number first (Twilio
console: A2P 10DLC brand + campaign, or toll-free verification). Texting uses the Twilio account the phone module already verified
(/var/lib/procurator-voice/twilio.json). Every text still needs your /smssend approval in Telegram. Never prints credentials.
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

TWILIO = Path('/var/lib/procurator-voice/twilio.json')
SETTINGS = Path('/var/lib/procurator-mail/sms.json')


def save(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main(argv=None, ask=input):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--status', action='store_true')
    g.add_argument('--on', action='store_true')
    g.add_argument('--off', action='store_true')
    a = p.parse_args(argv)
    try:
        current = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        current = {}
    if a.status:
        print('Texting: %s%s' % ('ON from ' + current['from'] if current.get('enabled') else 'off',
                                  (' (%s, since %s)' % (current.get('registration'), current.get('enabled_at', '')[:10])) if current.get('enabled') else ''))
        return 0
    if a.off:
        save(SETTINGS, {**current, 'enabled': False, 'disabled_at': datetime.datetime.now(datetime.timezone.utc).isoformat()})
        print('Texting is off. Drafts can still be written; no review or send is possible.')
        return 0
    if os.geteuid() != 0:
        raise SystemExit('Run this in your root server console.')
    try:
        number = json.loads(TWILIO.read_text()).get('from_number')
    except (OSError, ValueError):
        raise SystemExit('The phone module is not connected (bin/connect-twilio.py first). Nothing changed.')
    if not re.fullmatch(r'\+1[2-9]\d{9}', str(number or '')):
        raise SystemExit('The verified Twilio number is not a US number. Nothing changed.')
    print('Texts will come from %s.' % number)
    kind = ask('How is it registered for business texting? Type 10dlc (A2P 10DLC campaign approved) or tollfree (toll-free verified): ').strip().lower()
    if kind not in ('10dlc', 'tollfree'):
        raise SystemExit('Texting stays off: register the number in the Twilio console first. Nothing changed.')
    save(SETTINGS, {'enabled': True, 'from': number, 'registration': kind, 'enabled_at': datetime.datetime.now(datetime.timezone.utc).isoformat()})
    print('Texting is ON. Every text still needs your /smssend approval in Telegram.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
