#!/usr/bin/env python3
"""Console setup: model key, Telegram bot, and pairing your phone — typed hidden, never printed, never sent to chat.

    sudo python3 bin/connect-accounts.py                 # everything (what the installer offers in the terminal)
    sudo python3 bin/connect-accounts.py --anthropic-only
    sudo python3 bin/connect-accounts.py --telegram-only # new bot token and re-pair, keeping the model key

Pairing needs no code and no user-id lookup: the script prints a link (https://t.me/<your bot>?start=<one-time code>); open it on
your phone and press Start. Whoever presses Start with that link becomes the owner: config/owner.json and PROCURATOR_OWNER_ID are
written, and the Telegram channel is set to an allow-list holding only you (integrations/web/setup_core.py).

The browser does the same thing: the installer prints a one-time setup link (integrations/web/setup_wizard.py).
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'integrations' / 'web'))
import setup_core as core  # noqa: E402


def ask(prompt, check, attempts=3):
    for i in range(attempts):
        value = getpass.getpass(prompt).strip()
        try:
            return check(value)
        except core.SetupError as e:
            print('  ' + str(e) + ('' if i == attempts - 1 else ' Try again.'))
    raise SystemExit('No changes saved.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--telegram-only', action='store_true', help='New bot token and re-pair; keep the model key.')
    mode.add_argument('--anthropic-only', action='store_true', help='Replace only the Anthropic key.')
    ap.add_argument('--timeout', type=int, default=900, help='Seconds to wait for you to press Start (default 15 minutes).')
    a = ap.parse_args(argv)
    if not sys.stdin.isatty():
        raise SystemExit('Run this in your own server console (input must be a terminal).')
    if os.geteuid() != 0:
        raise SystemExit('Run as root:  sudo python3 bin/connect-accounts.py')
    os.umask(0o077)
    box = core.Box(ROOT)
    if not box.env.exists():
        raise SystemExit('The box is not initialised yet. Run:  sudo bash bin/install.sh --local')
    env = core.read_env(box.env)

    key = None
    if not a.telegram_only:
        print('\nStep 1 of 3 — your AI model key')
        print('  Make one at https://console.anthropic.com/settings/keys (Create key). It starts with sk-ant- and is hidden as you paste.')
        key = ask('  Anthropic API key: ', core.verify_anthropic)
        print('  Checked with Anthropic: it works.')
        if a.anthropic_only:
            core.write_env(box.env, {'ANTHROPIC_API_KEY': key})
            print('Saving and restarting the assistant…')
            box.start_gateway()
            print('Done. The new key is in use.')
            return 0
    elif not core.ANTHROPIC_KEY.fullmatch(env.get('ANTHROPIC_API_KEY', '')):
        raise SystemExit('No saved Anthropic key. Run without --telegram-only first.')

    print('\nStep 2 of 3 — your Telegram bot')
    print('  On your phone open Telegram › @BotFather › /newbot › any name › a username ending in "bot".')
    print('  BotFather replies with a token like 123456789:AA… — paste it here (hidden).')
    bot = ask('  Bot token: ', core.verify_bot)
    print('  Found your bot: @%s' % bot['username'])

    print('\nStep 3 of 3 — connect your phone')
    code = core.new_code()
    print('  Open this link ON YOUR PHONE and press Start:\n\n      %s\n' % core.pair_link(bot['username'], code))
    print('  (Whoever presses Start with this link becomes the only person the assistant listens to.)')
    print('  Waiting…', flush=True)
    try:
        owner = core.wait_for_start(bot['token'], code, timeout_s=a.timeout)
    except core.GatewayBusy:
        print('  The assistant is currently reading this bot; pausing it for a moment…', flush=True)
        box.stop_gateway()
        owner = core.wait_for_start(bot['token'], code, timeout_s=a.timeout)
    except core.SetupError as e:
        raise SystemExit('  ' + str(e) + ' No changes saved.')
    who = ' '.join(x for x in (owner.get('first_name'), owner.get('last_name')) if x) or 'you'
    print('  Paired with %s.' % who)

    print('\nSaving and restarting the assistant (about a minute)…', flush=True)
    tz = env.get('OPENCLAW_TZ', '')
    try:
        out = core.finish(box, key, bot, owner, timezone=tz)
    except core.SetupError as e:
        raise SystemExit(str(e))
    print('\nDone. %s now answers only to %s on Telegram.' % (out['bot'], who))
    print('Next: say hi to your bot — it asks a few quick questions (what to call you, he/she/they, what you need).')
    print('Then send /panel and open Accounts to connect iCloud and Gmail.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
