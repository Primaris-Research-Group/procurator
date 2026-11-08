"""Per-site trajectory hints (web 0.18.0, PROCURATOR browsing plan Phase 4, §7.2).

After a task completes, the broker keeps the order of controls that worked on that site (labels and kinds, never
the values typed) plus how many approvals it took. When a new task is prepared on the same site, up to 15 lines are
offered: "Last time on leaveco.example (Nov 5) these steps worked: …". A trajectory expires after 60 days, or as soon
as a later run on the site gets stuck at one of its steps. These are hints only; every gate still applies.
"""
import json
import re
import sqlite3
import time
from contextlib import contextmanager

TTL_DAYS = 60
MAX_LINES = 15

class Memory:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS trajectories(id INTEGER PRIMARY KEY AUTOINCREMENT,site TEXT,intent TEXT,steps TEXT,
              approvals INTEGER,ts REAL,task_id TEXT UNIQUE,valid INTEGER DEFAULT 1,invalid_reason TEXT)''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def steps_from_journal(rows):
        """Ordered (kind, label) without values; consecutive repeats folded; at most 30."""
        out = []
        for r in rows:
            kind, label = r['kind'], (r['label'] or '').strip()
            if kind == 'nav':
                continue
            if not label or len(label) > 80:
                continue
            item = {'kind': kind, 'label': label}
            if out and out[-1] == item:
                continue
            out.append(item)
        return out[:30]

    def record(self, site, intent, rows, approvals, task_id):
        steps = self.steps_from_journal(rows)
        if len(steps) < 2:
            return False
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO trajectories(site,intent,steps,approvals,ts,task_id) VALUES(?,?,?,?,?,?)',
                       (site, (intent or '')[:160], json.dumps(steps), int(approvals), time.time(), task_id))
        return True

    def invalidate(self, site, label, reason):
        """A later run on the site got stuck at this control: trajectories that rely on it are retired."""
        if not label:
            return 0
        n = 0
        with self.db() as db:
            for r in db.execute('SELECT id,steps FROM trajectories WHERE site=? AND valid=1', (site,)).fetchall():
                if any(s['label'] == label for s in json.loads(r['steps'])):
                    db.execute('UPDATE trajectories SET valid=0,invalid_reason=? WHERE id=?', (reason[:200], r['id']))
                    n += 1
        return n

    def hints(self, site, intent='', now=None):
        now = now or time.time()
        with self.db() as db:
            rows = db.execute('SELECT * FROM trajectories WHERE site=? AND valid=1 AND ts>? ORDER BY ts DESC LIMIT 5', (site, now - TTL_DAYS * 86400)).fetchall()
        if not rows:
            return None
        words = set(w for w in (intent or '').lower().split() if len(w) > 3)
        best = max(rows, key=lambda r: (len(words & set(r['intent'].lower().split())), r['ts']))
        steps = json.loads(best['steps'])
        verb = {'fill': 'type into', 'select': 'choose in', 'click': 'click', 'submit': 'press (final, needs approval)', 'upload': 'upload into', 'click_xy': 'click (by position)'}
        lines = ['%d. %s "%s"' % (i + 1, verb.get(s['kind'], s['kind']), s['label']) for i, s in enumerate(steps[:MAX_LINES - 1])]
        day = time.strftime('%b %-d', time.localtime(best['ts']))
        return {'text': 'Last time on %s (%s, "%s") these steps worked, with %d approval%s:\n%s' % (site, day, best['intent'][:80], best['approvals'], '' if best['approvals'] == 1 else 's', '\n'.join(lines)),
                'from_task': best['task_id'], 'note': 'Hints only: the page may have changed; read each snapshot and follow the gates as usual.'}

# ---------- Phase 7 (web 0.19.0): playbooks with per-step healing and demotion ----------
# A site with at least 3 completed runs of a similar errand gets a playbook: the ordered steps of the latest successful
# run, each with its kind, label, control tag/type, the broker's class and the page fingerprint (control kinds and labels
# only). Replay (Tasks 'task_playbook') re-finds each step's control BY LABEL on a page whose fingerprint matches (no cached
# selector is trusted), runs it through the normal task step (every gate applies), and stops at anything gated or anything
# that needs a value from this task. Values are never stored: a fill is replayable only when it was an owner fact
# ({{owner.KEY}}), and only the key name is kept. A completed run rebuilds the playbook (healing); a playbook whose replayed
# steps fail more than 20% of the time (after 5 tries) is demoted to hints only.
PLAYBOOK_RUNS = 3
SIMILAR = 0.6
DEMOTE_RATE = 0.2
DEMOTE_MIN = 5

def words(text):
    return {w for w in re.findall(r'[a-z0-9]+', (text or '').lower()) if len(w) > 3}

def jaccard(a, b):
    a, b = set(a or []), set(b or [])
    return len(a & b) / len(a | b) if a and b else 0.0

class Playbooks:
    def __init__(self, memory):
        self.m = memory
        with memory.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS playbooks(id INTEGER PRIMARY KEY AUTOINCREMENT,site TEXT,intent TEXT,steps TEXT,runs INTEGER,
              updated REAL,hits INTEGER DEFAULT 0,fails INTEGER DEFAULT 0,demoted INTEGER DEFAULT 0,demoted_reason TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS playbook_cursor(task_id TEXT PRIMARY KEY,playbook_id INTEGER,idx INTEGER)')

    @staticmethod
    def steps_from_rows(rows):
        out = []
        for r in rows:
            if r['kind'] == 'nav' or not (r['label'] or '').strip():
                continue
            item = {'kind': r['kind'], 'label': r['label'].strip(), 'tag': r['tag'], 'type': r['ctype'], 'cls': r['cls'],
                    'sig': json.loads(r['page_sig'] or '[]'), 'value_key': r['value_key']}
            if out and {k: v for k, v in out[-1].items() if k != 'sig'} == {k: v for k, v in item.items() if k != 'sig'}:
                out[-1] = item
                continue
            out.append(item)
        return out[:40]

    def learn(self, site, intent, rows, now=None):
        """After a completed task: (re)build the site's playbook when 3+ similar runs succeeded within 60 days."""
        now = now or time.time()
        with self.m.db() as db:
            similar = [r for r in db.execute('SELECT intent FROM trajectories WHERE site=? AND ts>?', (site, now - TTL_DAYS * 86400)).fetchall()
                       if jaccard(words(r['intent']), words(intent)) >= 0.5]
        if len(similar) < PLAYBOOK_RUNS:
            return None
        steps = self.steps_from_rows(rows)
        if len(steps) < 2:
            return None
        with self.m.db() as db:
            for r in db.execute('SELECT id,intent FROM playbooks WHERE site=?', (site,)).fetchall():
                if jaccard(words(r['intent']), words(intent)) >= 0.5:
                    db.execute('UPDATE playbooks SET steps=?,runs=?,updated=?,intent=? WHERE id=?', (json.dumps(steps), len(similar), now, intent[:160], r['id']))
                    return r['id']
            return db.execute('INSERT INTO playbooks(site,intent,steps,runs,updated) VALUES(?,?,?,?,?)', (site, intent[:160], json.dumps(steps), len(similar), now)).lastrowid

    def find(self, site, intent):
        with self.m.db() as db:
            rows = db.execute('SELECT * FROM playbooks WHERE site=? ORDER BY updated DESC', (site,)).fetchall()
        best = max(rows, key=lambda r: jaccard(words(r['intent']), words(intent)), default=None)
        if not best or jaccard(words(best['intent']), words(intent)) < 0.5:
            return None
        return dict(best, steps=json.loads(best['steps']))

    def cursor(self, task_id, pid):
        with self.m.db() as db:
            r = db.execute('SELECT * FROM playbook_cursor WHERE task_id=?', (task_id,)).fetchone()
        return r['idx'] if r and r['playbook_id'] == pid else 0

    def advance(self, task_id, pid, idx):
        with self.m.db() as db:
            db.execute('INSERT OR REPLACE INTO playbook_cursor VALUES(?,?,?)', (task_id, pid, idx))

    def note(self, pid, ok):
        with self.m.db() as db:
            db.execute('UPDATE playbooks SET %s=%s+1 WHERE id=?' % (('hits', 'hits') if ok else ('fails', 'fails')), (pid,))
            r = db.execute('SELECT hits,fails FROM playbooks WHERE id=?', (pid,)).fetchone()
            total = r['hits'] + r['fails']
            if total >= DEMOTE_MIN and r['fails'] / total > DEMOTE_RATE:
                db.execute("UPDATE playbooks SET demoted=1,demoted_reason=? WHERE id=?", ('replayed steps failed %d of %d times' % (r['fails'], total), pid))

    def match(self, playbook, start, controls, sig):
        """First step at or after start whose page fingerprint matches the live page and whose control is found by label."""
        for j in range(start, len(playbook['steps'])):
            st = playbook['steps'][j]
            if jaccard(st['sig'], sig) < SIMILAR:
                continue
            found = [c for c in controls if ' '.join(str(c.get('label') or '').split()) == st['label'] and c.get('tag') == st['tag']
                     and (st['type'] is None or (c.get('type') or '') == (st['type'] or ''))]
            if len(found) == 1:
                return j, st, found[0]
        return None

    def stats(self):
        with self.m.db() as db:
            return [{'site': r['site'], 'intent': r['intent'], 'runs': r['runs'], 'steps': len(json.loads(r['steps'])), 'hits': r['hits'], 'fails': r['fails'],
                     'hit_rate': round(r['hits'] / (r['hits'] + r['fails']), 3) if r['hits'] + r['fails'] else None, 'demoted': bool(r['demoted']),
                     'demoted_reason': r['demoted_reason']} for r in db.execute('SELECT * FROM playbooks ORDER BY site')]
