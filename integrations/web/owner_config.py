"""Owner identity file (config/owner.json), schema 2 (web 0.27.0; Phase 1 of the distributable plan writes and reads it).

The owner talks to PROCURATOR through one or more chat channels. Schema 2 lists them instead of a single telegram_id, so Slack
or Teams can become a second way to reach the assistant without a migration later:

  {
    "schema": 2,
    "name": "the owner", "short_name": "The owner", "pronoun": "the owner", "timezone": "America/New_York", "language": "en",
    "email": "...", "work_email": "...",
    "channels": [
      {"type": "telegram", "id": "123456789", "primary": true},
      {"type": "slack", "team": "T0123", "id": "U0456"},
      {"type": "teams", "tenant": "...", "id": "..."}
    ]
  }

Rules the loader enforces:
  * exactly one primary channel, and it is telegram in this release (approvals stay Telegram-only: the approval boundary does
    not gain a second path just because a second chat channel exists);
  * channel ids are opaque strings matched exactly; a slack/teams id only counts together with its team/tenant;
  * schema 1 files ({"telegram_id": "..."}) are read as one primary telegram channel, and upgrade() writes schema 2.

is_owner(type, id, team=None) is how a future Slack/Teams channel plugin decides whether a message comes from the owner. Messages
from anyone else on those channels are inbound events (events.py), never owner instructions.
"""
import json
import os
import re
from pathlib import Path

OWNER_FILE = Path(os.environ.get('PROCURATOR_OWNER_FILE', '/opt/procurator/config/owner.json'))
CHANNEL_TYPES = ('telegram', 'slack', 'teams')
APPROVAL_CHANNELS = ('telegram',)
FIELDS = ('name', 'short_name', 'pronoun', 'timezone', 'language', 'email', 'work_email')


class OwnerConfigError(ValueError):
    pass


def _need(cond, what):
    if not cond:
        raise OwnerConfigError('owner.json: ' + what)


def normalize(raw):
    _need(isinstance(raw, dict), 'not an object')
    data = dict(raw)
    if 'channels' not in data and 'telegram_id' in data:   # schema 1
        data['channels'] = [{'type': 'telegram', 'id': str(data.pop('telegram_id')), 'primary': True}]
    data.pop('telegram_id', None)
    chans = data.get('channels')
    _need(isinstance(chans, list) and 1 <= len(chans) <= 6, 'channels is a list of 1-6 chat channels')
    out, seen = [], set()
    for c in chans:
        _need(isinstance(c, dict) and c.get('type') in CHANNEL_TYPES, 'each channel has a type: ' + ', '.join(CHANNEL_TYPES))
        cid = str(c.get('id', ''))
        _need(re.fullmatch(r'[A-Za-z0-9:_.@-]{1,80}', cid) is not None, 'channel id %r is not a plain id' % cid[:20])
        if c['type'] == 'telegram':
            _need(cid.isdigit(), 'a telegram channel id is the numeric user id')
        if c['type'] in ('slack', 'teams'):
            scope = 'team' if c['type'] == 'slack' else 'tenant'
            _need(isinstance(c.get(scope), str) and c[scope], 'a %s channel names its %s' % (c['type'], scope))
        key = (c['type'], cid, c.get('team') or c.get('tenant'))
        _need(key not in seen, 'a channel is listed twice')
        seen.add(key)
        out.append({k: (str(v) if k != 'primary' else bool(v)) for k, v in c.items() if k in ('type', 'id', 'team', 'tenant', 'primary', 'label')})
    prim = [c for c in out if c.get('primary')]
    _need(len(prim) == 1, 'exactly one channel is primary')
    _need(prim[0]['type'] in APPROVAL_CHANNELS, 'the primary channel is telegram (approvals are Telegram-only in this release)')
    data['channels'] = out
    data['schema'] = 2
    for k in FIELDS:
        if k in data:
            _need(isinstance(data[k], str) and len(data[k]) <= 120, '%s is text' % k)
    return data


def load(path=OWNER_FILE):
    p = Path(path)
    if not p.exists():
        raise OwnerConfigError('owner.json is missing at %s; run the pairing step (Setup) to create it.' % p)
    return normalize(json.loads(p.read_text()))


def upgrade(path=OWNER_FILE):
    """Rewrite a schema 1 file as schema 2 (same owner, same channel). Returns True when it changed the file."""
    p = Path(path)
    raw = json.loads(p.read_text())
    if raw.get('schema') == 2 and 'telegram_id' not in raw:
        return False
    data = normalize(raw)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=1) + '\n')
    os.chmod(tmp, 0o640)
    os.replace(tmp, p)
    return True


def primary(owner):
    return next(c for c in owner['channels'] if c.get('primary'))


def owner_id():
    """The owner's Telegram user id for notifications: PROCURATOR_OWNER_ID in the environment, else the primary channel in
    owner.json, else '' (then nothing is sent and the log says why). The plugins use the same rule (owner-config.mjs ownerId)."""
    env = os.environ.get('PROCURATOR_OWNER_ID', '').strip()
    if env.isdigit():
        return env
    try:
        return str(primary(load())['id'])
    except Exception:
        return ''


def channel_ids(owner, ctype):
    return [c['id'] for c in owner['channels'] if c['type'] == ctype]


def is_owner(owner, ctype, cid, team=None):
    for c in owner['channels']:
        if c['type'] != ctype or c['id'] != str(cid):
            continue
        if ctype in ('slack', 'teams') and (c.get('team') or c.get('tenant')) != team:
            continue
        return True
    return False


def can_approve(owner, ctype, cid, team=None):
    """Approvals come only from an approval channel the owner holds (telegram in this release)."""
    return ctype in APPROVAL_CHANNELS and is_owner(owner, ctype, cid, team)
