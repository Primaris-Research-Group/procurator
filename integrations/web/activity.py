"""Activity log (Nov 5 2026): one durable record of everything the assistant did for the owner.

Entries come from three places:
- the services themselves (system records): browser tasks and approved browser actions (this broker),
  emails sent / moved to Trash and calendar or task changes (mail broker), phone calls (voice plugin);
- the assistant's own after-task reports (activity_report), marked as written by the assistant.
System entries are written by service code only (no model tool can create or edit them) and are
upserted by a stable key, so a retry or a later summary updates the same entry instead of duplicating.
The owner reads the log in Telegram (/activity) or on a private read-only page; the assistant reads it
with activity_search / activity_get.
"""
import datetime as dt
import hashlib
import html
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo('America/New_York')
except Exception:  # pragma: no cover
    EASTERN = dt.timezone(dt.timedelta(hours=-5))

KINDS = ('call', 'browser_task', 'browser_action', 'purchase', 'booking', 'email', 'email_trash', 'calendar', 'task_list', 'signin', 'mission', 'report', 'other')
STATUSES = ('completed', 'in_progress', 'failed', 'uncertain', 'cancelled', 'paused', 'recorded')
SOURCES = ('web', 'mail', 'voice', 'assistant')
GROUPS = ('Waiting for you', 'In progress', 'Scheduled')
KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{2,99}$')
CONTROL = re.compile('[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f‪-‮⁦-⁩]')
LABEL = {'call': 'Phone call', 'browser_task': 'Browser task', 'browser_action': 'Browser action', 'purchase': 'Purchase', 'booking': 'Booking',
         'email': 'Email', 'email_trash': 'Email cleanup', 'calendar': 'Calendar', 'task_list': 'Google Tasks', 'signin': 'Saved login',
         'mission': 'Mission', 'report': 'Report', 'other': 'Other'}
ICON = {'completed': '✅', 'in_progress': '⏳', 'failed': '❌', 'uncertain': '⚠️', 'cancelled': '⏹', 'paused': '⏸', 'recorded': '📝'}
VIEW_MINUTES = 45
RETAIN_DAYS = 400


def clean(value, key, maximum, required=True):
    if value is None and not required:
        return ''
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError('%s is required.' % key)
    return CONTROL.sub('', value).strip()[:maximum]


class Activity:
    def __init__(self, path, now=time.time):
        self.path, self.now, self.lock = path, now, threading.RLock()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT UNIQUE,source TEXT,kind TEXT,
              title TEXT,status TEXT,details TEXT,refs TEXT,mission TEXT,started REAL,updated REAL,versions INTEGER DEFAULT 1);
            CREATE INDEX IF NOT EXISTS activity_updated ON activity(updated);
            CREATE TABLE IF NOT EXISTS activity_views(hash TEXT PRIMARY KEY,expires REAL);
            CREATE TABLE IF NOT EXISTS upcoming_push(id INTEGER PRIMARY KEY CHECK(id=1),items TEXT,at REAL);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def when(t):
        return dt.datetime.fromtimestamp(t, EASTERN).strftime('%a %b %d, %I:%M %p').replace(' 0', ' ') if t else ''

    def record(self, entry, source):
        """Create or update one entry by its key. Service records and assistant reports never overwrite each other."""
        if source not in SOURCES:
            raise ValueError('Unknown activity source.')
        if not isinstance(entry, dict):
            raise ValueError('Activity entry must be an object.')
        key = entry.get('key')
        if not isinstance(key, str) or not KEY.match(key):
            raise ValueError('Activity key must be 3–100 plain characters.')
        key = source + ':' + key
        kind, status = entry.get('kind'), entry.get('status', 'recorded')
        if kind not in KINDS:
            raise ValueError('kind must be one of: ' + ', '.join(KINDS))
        if status not in STATUSES:
            raise ValueError('status must be one of: ' + ', '.join(STATUSES))
        refs = entry.get('refs') or []
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError('refs must be a list of at most 8 references.')
        refs = [clean(r, 'ref', 300) for r in refs if isinstance(r, str) and r.strip()]
        title = clean(entry.get('title'), 'title', 200)
        details = clean(entry.get('details'), 'details', 6000, required=False)
        mission = clean(entry.get('mission'), 'mission', 24, required=False).lower()
        now = self.now()
        with self.lock, self.db() as db:
            old = db.execute('SELECT * FROM activity WHERE key=?', (key,)).fetchone()
            if old:
                if (old['title'], old['status'], old['details'], json.loads(old['refs'] or '[]')) == (title, status, details, refs):
                    return {'id': old['id'], 'unchanged': True}
                db.execute('UPDATE activity SET kind=?,title=?,status=?,details=?,refs=?,mission=COALESCE(NULLIF(?,""),mission),updated=?,versions=versions+1 WHERE id=?',
                           (kind, title, status, details, json.dumps(refs), mission, now, old['id']))
                return {'id': old['id'], 'updated': True}
            cur = db.execute('INSERT INTO activity(key,source,kind,title,status,details,refs,mission,started,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (key, source, kind, title, status, details, json.dumps(refs), mission, now, now))
            if cur.lastrowid % 200 == 0:
                db.execute('DELETE FROM activity WHERE updated<?', (now - RETAIN_DAYS * 86400,))
            return {'id': cur.lastrowid, 'created': True}

    def public(self, r, full=False):
        out = {'id': r['id'], 'when': self.when(r['started']), 'kind': r['kind'], 'title': r['title'], 'status': r['status'],
               'by': 'assistant report' if r['source'] == 'assistant' else 'system record (' + r['source'] + ')'}
        if r['updated'] - r['started'] > 60:
            out['last_update'] = self.when(r['updated'])
        if r['mission']:
            out['mission'] = r['mission']
        if full:
            out['details'] = r['details']
            out['refs'] = json.loads(r['refs'] or '[]')
        return out

    def search(self, args):
        days = args.get('days', 7)
        if not isinstance(days, int) or not 1 <= days <= 400:
            raise ValueError('days must be 1–400.')
        limit = args.get('limit', 25)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            limit = 25
        where, params = ['updated>=?'], [self.now() - days * 86400]
        for field in ('kind', 'status', 'mission'):
            if args.get(field):
                where.append(field + '=?'); params.append(str(args[field]).lower() if field == 'mission' else args[field])
        if args.get('source') in ('assistant', 'system'):
            where.append('source' + ('=?' if args['source'] == 'assistant' else '<>?')); params.append('assistant')
        q = args.get('query')
        if isinstance(q, str) and q.strip():
            where.append('(title LIKE ? OR details LIKE ? OR refs LIKE ?)'); params += ['%' + q.strip()[:80] + '%'] * 3
        with self.db() as db:
            rows = db.execute('SELECT * FROM activity WHERE ' + ' AND '.join(where) + ' ORDER BY started DESC LIMIT ?', params + [limit]).fetchall()
        return {'entries': [self.public(r) for r in rows], 'days': days,
                'note': 'System records come from the services; assistant reports are the assistant\'s own account. Use activity_get for details.'}

    def get(self, ident):
        if not isinstance(ident, int):
            raise ValueError('Use an entry id from activity_search.')
        with self.db() as db:
            r = db.execute('SELECT * FROM activity WHERE id=?', (ident,)).fetchone()
        if not r:
            raise ValueError('No activity entry %d.' % ident)
        return self.public(r, full=True)

    def telegram_list(self, days=3, kind=None):
        found = self.search({'days': days, 'limit': 15, **({'kind': kind} if kind else {})})['entries']
        if not found:
            return 'Nothing recorded in the last %d day%s.' % (days, '' if days == 1 else 's')
        lines = ['Activity, last %d day%s (newest first):' % (days, '' if days == 1 else 's')]
        for e in found:
            lines.append('%s #%d · %s\n   %s: %s' % (ICON.get(e['status'], '•'), e['id'], e['when'], LABEL.get(e['kind'], e['kind']), e['title']))
        lines.append('\nOne entry in full: /activity %d\nMore days: /activity days 7 · Only calls: /activity days 7 calls\nEverything on one page: /activity page' % found[0]['id'])
        return '\n'.join(lines)

    def telegram_entry(self, ident):
        e = self.get(ident)
        out = ['%s %s — %s' % (ICON.get(e['status'], '•'), LABEL.get(e['kind'], e['kind']), e['title']), 'When: ' + e['when'] + ((' (updated ' + e['last_update'] + ')') if e.get('last_update') else ''),
               'Status: ' + e['status'].replace('_', ' '), 'Recorded by: ' + e['by']]
        if e.get('mission'):
            out.append('Mission: ' + e['mission'])
        if e['details']:
            out += ['', e['details'][:3000]]
        if e['refs']:
            out += ['', 'References:'] + ['• ' + r for r in e['refs']]
        return '\n'.join(out)

    # ---------- upcoming: what is waiting for the owner, running, or scheduled (Nov 5) ----------
    def push(self, items):
        """Items from the gateway side (mail approvals, calls, reminders, morning brief), refreshed every few minutes."""
        if not isinstance(items, list) or len(items) > 60:
            raise ValueError('items must be a list of at most 60.')
        out = []
        for x in items:
            if not isinstance(x, dict) or x.get('group') not in GROUPS:
                continue
            out.append({'group': x['group'], 'kind': x.get('kind') if x.get('kind') in KINDS else 'other', 'what': clean(x.get('what'), 'what', 200),
                        'when': x['when'] if isinstance(x.get('when'), (int, float)) else None, 'how': clean(x.get('how'), 'how', 200, required=False), 'source': 'gateway'})
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO upcoming_push VALUES(1,?,?)', (json.dumps(out), self.now()))
        return {'stored': len(out)}

    def upcoming(self, local):
        with self.db() as db:
            r = db.execute('SELECT items,at FROM upcoming_push WHERE id=1').fetchone()
        items = list(local)
        fresh = bool(r and self.now() - r['at'] <= 20 * 60)
        if fresh:
            items += json.loads(r['items'])
        order = {g: i for i, g in enumerate(GROUPS)}
        items.sort(key=lambda x: (order.get(x['group'], 9), x.get('when') or 0))
        for x in items:
            if x.get('when'):
                x['when_text'] = self.when(x['when'])
        return {'items': items, 'mail_calls_reminders_checked': self.when(r['at']) if r else None,
                **({} if fresh else {'note': 'Email/calendar approvals, calls and reminders were not refreshed in the last 20 minutes; they may be missing.'})}

    def telegram_upcoming(self, up):
        if not up['items']:
            return 'Nothing is waiting for you, running or scheduled right now.' + ('\n(' + up['note'] + ')' if up.get('note') else '')
        lines, group = [], None
        for x in up['items']:
            if x['group'] != group:
                group = x['group']; lines.append(('\n' if lines else '') + group + ':')
            lines.append('• ' + x['what'] + ((' — ' + x['when_text']) if x.get('when_text') else '') + (('\n   ' + x['how']) if x.get('how') else ''))
        if up.get('note'):
            lines.append('\n' + up['note'])
        lines.append('\nDone so far: /activity · Full page: /activity page')
        return '\n'.join(lines)

    # ---------- private read-only page ----------
    def view_link(self, base):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM activity_views WHERE expires<?', (self.now(),))
            db.execute('INSERT INTO activity_views VALUES(?,?)', (hashlib.sha256(token.encode()).hexdigest(), self.now() + VIEW_MINUTES * 60))
        return base + '/assistant/activity/' + token

    def view_ok(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}', token):
            return False
        with self.db() as db:
            r = db.execute('SELECT expires FROM activity_views WHERE hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return bool(r and r['expires'] >= self.now())

    def page(self, days=30, kind=None, nonce='', upcoming=None):
        days = days if isinstance(days, int) and 1 <= days <= 400 else 30
        kind = kind if kind in KINDS else None
        with self.db() as db:
            rows = db.execute('SELECT * FROM activity WHERE updated>=?' + (' AND kind=?' if kind else '') + ' ORDER BY started DESC LIMIT 300',
                              [self.now() - days * 86400] + ([kind] if kind else [])).fetchall()
        esc = html.escape
        chips = ''.join('<a class="chip%s" href="?days=%d%s">%s</a>' % (' on' if k == kind else '', days, '&amp;kind=' + k if k else '', esc(LABEL.get(k, 'All') if k else 'All'))
                        for k in [None, 'call', 'browser_task', 'browser_action', 'booking', 'purchase', 'email', 'calendar', 'mission', 'report'])
        spans = ''.join('<a class="chip%s" href="?days=%d%s">%s</a>' % (' on' if d == days else '', d, '&amp;kind=' + kind if kind else '', label)
                        for d, label in [(1, 'Today'), (7, '7 days'), (30, '30 days'), (90, '90 days'), (365, 'Year')])
        items = []
        for r in rows:
            refs = json.loads(r['refs'] or '[]')
            items.append('<article><header><span class="st">%s</span><b>%s</b><time>%s</time></header><h2>%s</h2>%s%s%s</article>' % (
                ICON.get(r['status'], '•'), esc(LABEL.get(r['kind'], r['kind'])), esc(self.when(r['started'])), esc(r['title']),
                ('<pre>%s</pre>' % esc(r['details'])) if r['details'] else '',
                ('<ul>%s</ul>' % ''.join('<li>%s</li>' % esc(x) for x in refs)) if refs else '',
                '<p class="by">%s · %s%s</p>' % (esc(r['status'].replace('_', ' ')), 'written by the assistant' if r['source'] == 'assistant' else 'recorded by the ' + esc(r['source']) + ' service',
                                                 (' · mission ' + esc(r['mission'])) if r['mission'] else '')))
        body = ''.join(items) or '<p class="empty">Nothing recorded for this period.</p>'
        up = ''
        if upcoming and upcoming.get('items'):
            groups = []
            for g in GROUPS:
                xs = [x for x in upcoming['items'] if x['group'] == g]
                if xs:
                    groups.append('<h3>%s</h3><ul class="up">%s</ul>' % (esc(g), ''.join('<li><b>%s</b>%s%s</li>' % (
                        esc(x['what']), (' <time>%s</time>' % esc(x['when_text'])) if x.get('when_text') else '', ('<br><small>%s</small>' % esc(x['how'])) if x.get('how') else '') for x in xs)))
            up = '<section class="upcoming"><h2>Waiting, running and scheduled</h2>%s%s</section>' % (''.join(groups), ('<p class="by">%s</p>' % esc(upcoming['note'])) if upcoming.get('note') else '')
        elif upcoming is not None:
            up = '<section class="upcoming"><h2>Waiting, running and scheduled</h2><p class="empty">Nothing right now.</p></section>'
        return PAGE.format(nonce=nonce, chips=chips, spans=spans, count=len(rows), body=up + '<h2 class="done">Done</h2>' + body)


PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Assistant activity</title><style nonce="{nonce}">
:root{{color-scheme:light dark;--bg:#f6f7f9;--fg:#111;--muted:#5a5f69;--card:#fff;--line:#d5d8de;--accent:#0b63ce}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121417;--fg:#eee;--muted:#a6abb5;--card:#1d2025;--line:#333842;--accent:#5aa2ff}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:760px;margin:0 auto;padding:16px 16px 48px}}h1{{font-size:21px;margin:4px 0}}p.sub{{color:var(--muted);margin:0 0 10px}}
nav{{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}}a.chip{{text-decoration:none;color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:14px;background:var(--card)}}
a.chip.on{{background:var(--accent);border-color:var(--accent);color:#fff}}
article{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-top:10px}}
article header{{display:flex;gap:8px;align-items:center;font-size:14px;color:var(--muted)}}article header b{{color:var(--fg)}}article time{{margin-left:auto}}
h2{{font-size:16px;margin:6px 0}}pre{{white-space:pre-wrap;word-break:break-word;font:14px/1.45 -apple-system,system-ui,sans-serif;margin:6px 0;color:var(--fg)}}
section.upcoming{{background:var(--card);border:1px solid var(--accent);border-radius:14px;padding:12px 14px;margin-top:12px}}
section.upcoming h2,h2.done{{font-size:17px;margin:4px 0 6px}}h2.done{{margin-top:18px}}section.upcoming h3{{font-size:14px;color:var(--muted);margin:10px 0 2px;text-transform:uppercase;letter-spacing:.03em}}
ul.up{{padding-left:18px;color:var(--fg);word-break:normal}}ul.up li{{margin:4px 0}}ul.up time{{color:var(--muted);font-size:13px}}ul.up small{{color:var(--muted)}}
ul{{margin:6px 0;padding-left:20px;font-size:14px;color:var(--muted);word-break:break-all}}p.by{{margin:6px 0 0;font-size:13px;color:var(--muted)}}p.empty{{color:var(--muted)}}
</style></head><body><main><h1>Assistant activity</h1><p class="sub">{count} entries · read-only private page · times Eastern</p>
<nav>{spans}</nav><nav>{chips}</nav>{body}</main></body></html>'''
