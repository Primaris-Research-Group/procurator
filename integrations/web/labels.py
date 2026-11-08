"""Labeled data (web 0.28.0, plan v2 B3): health_taint generalized to three labels.

  health    set by health tool results, grants and connections (health_taint.py, unchanged since 0.25.0). May go to the granted
            institution's endpoint and contacts and to the owner's own address.
  presence  home occupancy, "away" modes, device location, calendar-derived away status. Owner's own address only.
  camera    snapshots, clips, doorbell images (media sha256s and their captions). Owner's own address only.

Any labeled text (or labeled media) to a public or workspace audience is refused, grant or not. Refusals never repeat the
value. health_taint_check stays the socket action name (the 0.25.0 gates call it); it now answers for all three labels.

Presence and camera values are written by the home module (note_values / note_media) and expire after 7 days; on public and
workspace audiences a short list of away-from-home phrases is also refused, because "we're away until Sunday" on a public page is
the leak this label exists to stop.
"""
import json
import re
import sqlite3
import time
from contextlib import contextmanager

LABELS = ('health', 'presence', 'camera')
TTL = 7 * 86400
AWAY = re.compile(r"(?i)\b(?:(?:no ?one|nobody) (?:is |will be )?(?:at )?home|(?:house|home|apartment|place) (?:is|will be) (?:empty|unoccupied)|"
                  r"(?:we|i)(?:'re| are|'ll be| will be| am|'m) (?:away|out of town|on vacation|travel(?:l)?ing) (?:until|till|through|for)|"
                  r"away from home (?:until|till|for))\b")


def norm(text):
    return ' '.join(re.sub(r'[^0-9a-z]+', ' ', str(text or '').lower()).split())


class Labels:
    def __init__(self, path, taint=None, now=time.time):
        self.path, self.taint, self.now = str(path), taint, now
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS label_values(value TEXT, label TEXT, source TEXT, created REAL, expires REAL, PRIMARY KEY(value, label))')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    # ---------- labeling ----------
    def note_values(self, values, label, source='home'):
        if label not in ('presence', 'camera'):
            raise ValueError('note_values labels presence or camera (health labels come from health tools).')
        t, n = self.now(), 0
        with self.db() as db:
            for v in values:
                key = ('sha:' + v) if label == 'camera' and re.fullmatch(r'[a-f0-9]{64}', str(v)) else norm(v)
                if len(key) < 4:
                    continue
                db.execute('INSERT INTO label_values VALUES(?,?,?,?,?) ON CONFLICT(value, label) DO UPDATE SET expires=excluded.expires',
                           (key, label, str(source)[:40], t, t + TTL))
                n += 1
        return n

    def note_media(self, sha256, label='camera', source='camera'):
        return self.note_values([sha256], label, source)

    def values(self):
        with self.db() as db:
            db.execute('DELETE FROM label_values WHERE expires<?', (self.now(),))
            return [(r['value'], r['label']) for r in db.execute('SELECT value, label FROM label_values')]

    def found(self, text, media=()):
        """{label: count} for the text and media sha256s (health included when the health module is present)."""
        out = {}
        n = ' ' + norm(text) + ' '
        shas = {'sha:' + s for s in media}
        for value, label in self.values():
            if (value.startswith('sha:') and value in shas) or (not value.startswith('sha:') and ' ' + value + ' ' in n):
                out[label] = out.get(label, 0) + 1
        if self.taint is not None:
            try:
                h = len(self.taint._hits(str(text or '')[:60000]))
            except Exception:
                h = 0
            if h:
                out['health'] = h
        return out

    # ---------- the check ----------
    def check(self, text, destination=None, owner_addresses=(), audience=None, media=()):
        text = str(text or '')[:60000]
        found = self.found(text, media)
        if audience in ('public', 'workspace') and AWAY.search(text):
            found['presence'] = found.get('presence', 0) + 1
        if not found:
            return {'tainted': False, 'allowed': True, 'label_kinds': []}
        kinds = sorted(found)
        total = sum(found.values())
        where = {'public': 'anyone on the internet', 'workspace': 'everyone in the workspace or channel'}
        if audience in where:
            return {'tainted': True, 'allowed': False, 'hard_block': True, 'labels': total, 'label_kinds': kinds,
                    'reason': ('The text carries %s details, and this is a %s destination (%s). Labeled details never go there, whatever the grants say. '
                               'Nothing was posted or saved.') % (' and '.join(kinds), audience, where[audience])}
        if 'health' in found and self.taint is not None:
            r = self.taint.check(text, destination, owner_addresses)
            if not r.get('allowed'):
                return {**r, 'label_kinds': kinds}
        if {'presence', 'camera'} & set(found):
            mine = set()
            for a in owner_addresses:
                mine |= {k for k in (self.taint.recipients(a) if self.taint else {'email:' + str(a).lower()}) if k.startswith('email:')}
            dest = self.taint.recipients(destination) if self.taint else {'email:' + str(d).lower() for d in (destination or [])}
            if audience != 'self' and not (dest and dest <= mine):
                other = [k for k in kinds if k in ('presence', 'camera')]
                return {'tainted': True, 'allowed': False, 'labels': total, 'label_kinds': kinds,
                        'reason': ('The text carries %s details (whether anyone is home, where the owner is, or camera images). Those go only to their own '
                                   'address. Nothing was sent or saved.') % ' and '.join(other)}
        return {'tainted': True, 'allowed': True, 'labels': total, 'label_kinds': kinds}
