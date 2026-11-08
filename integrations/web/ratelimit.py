"""Per-connector, per-account rate limits (web 0.27.0).

Social platforms ban accounts that post too often, and a loop in a packet can send the same kind of message many times.
Manifests declare limits (connectors.py: a connector-wide rate_limit and an optional per-tool rate_limit, each
{per_hour, per_day, cooldown_s}); the broker enforces them where the outbound action actually runs (the publish step after
the owner's approval, and again when a review is requested so the owner is not asked to approve something that would be refused).

Keys are "<connector>:<account>" for connector limits and "<connector>:<account>:<tool>" for tool limits. Only actions
that ran (or may have run: an uncertain outcome counts) are recorded; a refused or failed-before-sending action is not.
"""
import sqlite3
import time
from contextlib import contextmanager


def _fmt(seconds):
    seconds = int(max(1, seconds))
    if seconds < 90:
        return '%d seconds' % seconds
    if seconds < 5400:
        return '%d minutes' % round(seconds / 60)
    return '%.1f hours' % (seconds / 3600)


class RateLimiter:
    def __init__(self, path, now=time.time):
        self.path, self.now = str(path), now
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rate_events(key TEXT, at REAL, ref TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS rate_events_key ON rate_events(key, at)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def check(self, limits):
        """limits: [(key, spec)]. None when allowed; otherwise {'reason', 'retry_after_s'} for the tightest limit hit."""
        t = self.now()
        worst = None
        with self.db() as db:
            db.execute('DELETE FROM rate_events WHERE at<?', (t - 86400 * 2,))
            for key, spec in limits:
                rows = [r['at'] for r in db.execute('SELECT at FROM rate_events WHERE key=? AND at>? ORDER BY at', (key, t - 86400))]
                hits = []
                if spec.get('cooldown_s') and rows and t - rows[-1] < spec['cooldown_s']:
                    hits.append((rows[-1] + spec['cooldown_s'] - t, 'a %s cooldown between posts' % _fmt(spec['cooldown_s'])))
                hour = [a for a in rows if a > t - 3600]
                if spec.get('per_hour') and len(hour) >= spec['per_hour']:
                    hits.append((hour[len(hour) - spec['per_hour']] + 3600 - t, 'at most %d an hour' % spec['per_hour']))
                if spec.get('per_day') and len(rows) >= spec['per_day']:
                    hits.append((rows[len(rows) - spec['per_day']] + 86400 - t, 'at most %d a day' % spec['per_day']))
                for wait, what in hits:
                    if worst is None or wait > worst[0]:
                        worst = (wait, what, key)
        if worst is None:
            return None
        wait, what, key = worst
        return {'reason': 'Rate limit for %s (%s): try again in about %s. Nothing was sent.' % (key.split(':', 1)[0] + ' ' + key.split(':')[1], what, _fmt(wait)),
                'retry_after_s': int(wait) + 1, 'key': key}

    def record(self, keys, ref=''):
        t = self.now()
        with self.db() as db:
            for key in keys:
                db.execute('INSERT INTO rate_events VALUES(?,?,?)', (key, t, str(ref)[:64]))

    def usage(self, key):
        t = self.now()
        with self.db() as db:
            rows = [r['at'] for r in db.execute('SELECT at FROM rate_events WHERE key=? AND at>?', (key, t - 86400))]
        return {'last_hour': sum(1 for a in rows if a > t - 3600), 'last_day': len(rows), 'last_at': max(rows) if rows else None}


def keys_for(registry, connector, account, tool=None):
    """[(key, spec)] for one action, from the manifest registry."""
    out = []
    for scope, spec in (registry.limits(connector, tool) if registry else []):
        out.append(('%s:%s' % (connector, account) + (':' + tool if scope == 'tool' else ''), spec))
    return out
