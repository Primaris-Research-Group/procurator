"""Activity event stream (web 0.28.0, plan v2 B10): one event shape for every broker, for the Live tab and the later phone app.

  {id, ts, source, task_id, packet_id, kind, summary, labels, lane}
  source  web | mail | voice | orchestrator | builder | home | health | composer
  kind    step_start | tool | waiting_approval | call_state | browser_nav | done | error

Summaries name the tool and the target, never contents: no mail bodies, transcripts, page text, typed values or health values.
emit() enforces that: summaries are cut to 140 characters, newlines removed, and any summary that carries a labeled value
(labels.py) is replaced by "<kind> (<label> details withheld)". Mail and voice brokers emit over the web broker's tools socket
(stream_emit). Kept 7 days. Served as GET /api/v1/events (server-sent events, resume with Last-Event-ID).
"""
import json
import re
import sqlite3
import time
from contextlib import contextmanager

SOURCES = ('web', 'mail', 'voice', 'orchestrator', 'builder', 'home', 'health', 'composer')
KINDS = ('step_start', 'tool', 'waiting_approval', 'call_state', 'browser_nav', 'done', 'error')
KEEP = 7 * 86400
CONTROL = re.compile(r'[\x00-\x1f\x7f‪-‮⁦-⁩]')


class Stream:
    def __init__(self, path, labels=None, now=time.time):
        self.path, self.labels, self.now = str(path), labels, now
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS stream(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, source TEXT, task_id TEXT, packet_id TEXT, kind TEXT, summary TEXT, labels TEXT, lane TEXT)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def emit(self, source, kind, summary, task_id=None, packet_id=None, lane=None, labels=()):
        if source not in SOURCES or kind not in KINDS:
            raise ValueError('stream: unknown source or kind')
        text = CONTROL.sub(' ', str(summary or ''))[:140].strip()
        found = list(labels or [])
        if self.labels is not None and text:
            try:
                found = sorted(set(found) | set(self.labels.found(text)))
            except Exception:
                found = sorted(set(found) | {'unchecked'})
        if found:
            text = '%s (%s details withheld)' % (kind.replace('_', ' '), ' and '.join(x for x in found if x != 'unchecked') or 'labeled')
        clip = lambda v, n: (CONTROL.sub('', str(v))[:n] if v else None)
        with self.db() as db:
            cur = db.execute('INSERT INTO stream(ts,source,task_id,packet_id,kind,summary,labels,lane) VALUES(?,?,?,?,?,?,?,?)',
                             (self.now(), source, clip(task_id, 40), clip(packet_id, 40), kind, text, json.dumps(found), clip(lane, 40)))
            if cur.lastrowid % 200 == 0:
                db.execute('DELETE FROM stream WHERE ts<?', (self.now() - KEEP,))
            return cur.lastrowid

    def since(self, last_id=0, limit=100, task_id=None):
        q, args = 'SELECT * FROM stream WHERE id>?', [int(last_id or 0)]
        if task_id:
            q += ' AND task_id=?'
            args.append(str(task_id))
        with self.db() as db:
            rows = db.execute(q + ' ORDER BY id LIMIT ?', args + [max(1, min(500, limit))]).fetchall()
        return [{'id': r['id'], 'ts': r['ts'], 'source': r['source'], 'task_id': r['task_id'], 'packet_id': r['packet_id'], 'kind': r['kind'],
                 'summary': r['summary'], 'labels': json.loads(r['labels'] or '[]'), 'lane': r['lane']} for r in rows]

    def recent(self, limit=50):
        with self.db() as db:
            top = db.execute('SELECT MAX(id) m FROM stream').fetchone()['m'] or 0
        return self.since(max(0, top - limit), limit)


# Model-socket actions worth a stream line (the action name is the summary; arguments are never copied in).
TOOL_KINDS = {'browser_open': 'browser_nav', 'browser_navigate': 'browser_nav', 'task_prepare': 'step_start', 'task_step': 'tool',
              'social_post_review': 'waiting_approval', 'home_command': 'tool'}


def summary_for(action, args):
    """A content-free line for a model-socket action: its name, plus a domain for browser navigation."""
    a = args if isinstance(args, dict) else {}
    target = ''
    url = a.get('url') if isinstance(a.get('url'), str) else ''
    m = re.match(r'^https?://([^/:?#]+)', url)
    if m:
        target = ' ' + m.group(1).lower()[:60]
    return action.replace('_', ' ') + target
