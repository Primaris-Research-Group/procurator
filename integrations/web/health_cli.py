#!/usr/bin/env python3
"""Server-side credential store for health connections (web 0.21.0). Used by bin/set-health-token.sh over SSH; never a model tool.

  python3 health_cli.py set CONNECTION_ID      (token JSON or bare token on stdin)
  python3 health_cli.py status CONNECTION_ID
  python3 health_cli.py revoke CONNECTION_ID
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from health import Health  # noqa: E402

STATE = Path('/var/lib/procurator-web')


class Quiet:
    def send_with_buttons(self, *a):
        pass
    def telegram(self, *a):
        pass


def main(argv):
    if len(argv) != 2 or argv[0] not in ('set', 'status', 'revoke'):
        print(__doc__); return 2
    h = Health(Quiet(), STATE)
    cid = argv[1]
    if argv[0] == 'set':
        raw = sys.stdin.read().strip()
        if not raw:
            print('No token given.'); return 1
        try:
            token = json.loads(raw)
            if not isinstance(token, dict):
                raise ValueError
        except ValueError:
            token = {'access_token': raw}
        out = h.set_token(cid, token)
        print('Connection %s: %s (token stored on the server only)' % (out['id'], out['status'])); return 0
    with h.db() as db:
        c = h.connection(db, cid)
    if argv[0] == 'status':
        print('Connection %s: %s %s (%s) status=%s token=%s' % (c['id'], c['provider'], c['institution'], c['environment'], c['status'], 'yes' if c.get('token_ref') else 'no')); return 0
    h.set_connection_status(cid, 'revoked', 'revoked on the server')
    print('Connection %s revoked; token deleted; its grants are revoked.' % cid); return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
