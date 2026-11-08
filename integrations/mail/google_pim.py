"""Google Calendar and Google Tasks for the owner's own account (via the pinned gog CLI).

Reads are free. Creating or changing the owner's OWN events and tasks is immediate (reversible,
nobody else is notified). Anything that emails other people (events with guests) and any event
deletion waits for the owner's exact Telegram approval (/calapprove ID CODE).
Only fixed gog arguments are built here; model text never becomes a command-line flag.
"""
import hashlib
import datetime as dt
import json
import re
import time
import uuid

RFC3339 = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})')
DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
EVENT_ID = re.compile(r'[A-Za-z0-9_@.-]{5,1024}')
CAL_ID = re.compile(r'primary|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+|[A-Za-z0-9._-]{3,200}')
LIST_ID = re.compile(r'@default|[A-Za-z0-9_-]{5,200}')
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")
REMINDER = re.compile(r'(popup|email):\d{1,4}[mhd]')
TZ = 'America/New_York'


def clean(value, name, limit, required=False):
    if value is None or value == '':
        if required:
            raise ValueError(f'{name} is required.')
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(f'{name} must be text up to {limit} characters.')
    if value.startswith('-'):
        value = ' ' + value  # never let text look like a command-line flag
    return value


def when(value, name, all_day=False):
    if not isinstance(value, str) or not (DATE.fullmatch(value) if all_day else RFC3339.fullmatch(value)):
        raise ValueError(f'{name} must be ' + ('a date YYYY-MM-DD for all-day events.' if all_day else
                         'a full date-time with time zone, e.g. 2026-11-13T15:00:00-05:00.'))
    return value


def pick(node, *keys):
    for k in keys:
        if isinstance(node, dict) and node.get(k) not in (None, ''):
            return node[k]
    return None


def slim_event(e):
    if not isinstance(e, dict):
        return None
    start, end = e.get('start') or {}, e.get('end') or {}
    attendees = [a.get('email') for a in e.get('attendees') or [] if isinstance(a, dict) and not a.get('self')]
    return {'id': e.get('id'), 'summary': (e.get('summary') or '')[:200],
            'start': start.get('dateTime') or start.get('date') if isinstance(start, dict) else start,
            'end': end.get('dateTime') or end.get('date') if isinstance(end, dict) else end,
            'location': (e.get('location') or '')[:200] or None, 'status': e.get('status'),
            'guests': [a for a in attendees if a][:20], 'organizer': (e.get('organizer') or {}).get('email'),
            'calendar': e.get('calendarId') or e.get('calendar'), 'link': e.get('htmlLink')}


def events_from(result):
    items = pick(result, 'events', 'items') if isinstance(result, dict) else result
    return [x for x in (slim_event(e) for e in items or []) if x]


def slim_task(t):
    if not isinstance(t, dict):
        return None
    return {'id': t.get('id'), 'title': (t.get('title') or '')[:300], 'notes': (t.get('notes') or '')[:500] or None,
            'due': t.get('due'), 'status': t.get('status'), 'completed': t.get('completed'), 'parent': t.get('parent')}


class GooglePIM:
    def __init__(self, broker):
        self.b = broker
        with broker.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS pim_requests(request_id TEXT PRIMARY KEY,kind TEXT,result TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS cal_approvals(id TEXT PRIMARY KEY,hash TEXT,op TEXT,args TEXT,status TEXT,created REAL,result TEXT);''')
            # 0.23.0 (health foundation): a reused request_id must carry the same content, not just the same kind.
            if 'payload_hash' not in [r[1] for r in db.execute('PRAGMA table_info(pim_requests)')]:
                db.execute('ALTER TABLE pim_requests ADD COLUMN payload_hash TEXT')

    # --- helpers -----------------------------------------------------------------------------
    def ready(self):
        if not self.b.provider.calendar_ready():
            raise ValueError('Google Calendar and Tasks are not connected yet. The owner runs, in the droplet console: '
                             'python3 /opt/procurator/bin/connect-google.py --start --force-consent --all '
                             'and then --finish (one Google consent). Nothing was changed.')

    def gog(self, args, write=False):
        prefix = [] if write else ['--readonly']
        return self.b.provider.gog(prefix + args)

    @staticmethod
    def payload_hash(payload):
        if payload is None:
            return None
        clean_payload = {k: v for k, v in payload.items() if k not in ('request_id',) and v not in (None, '', [])}
        return hashlib.sha256(json.dumps(clean_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

    def once(self, request_id, kind, run, payload=None):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise ValueError('Use a stable request_id of 8–100 letters, digits, underscores or hyphens; reuse it on retries.')
        h = self.payload_hash(payload)
        with self.b.db() as db:
            old = db.execute('SELECT * FROM pim_requests WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['kind'] != kind:
                raise ValueError('That request_id was used for a different action.')
            if h and old['payload_hash'] and old['payload_hash'] != h:
                raise ValueError('That request_id was already used with different details; use a new request_id for a different event.')
            return {**json.loads(old['result']), 'replayed': True}
        result = run()
        with self.b.db() as db:
            db.execute('INSERT INTO pim_requests(request_id,kind,result,created,payload_hash) VALUES(?,?,?,?,?)', (request_id, kind, json.dumps(result), time.time(), h))
        return result

    def event_args(self, a, update=False):
        out = []
        all_day = a.get('all_day') is True
        if a.get('all_day') not in (None, True, False):
            raise ValueError('all_day must be true or false.')
        for key, flag in (('summary', '--summary'), ('location', '--location'), ('description', '--description')):
            v = clean(a.get(key), key, 300 if key != 'description' else 4000, required=(key == 'summary' and not update))
            if v is not None:
                out += [flag, v]
        if a.get('start') is not None or not update:
            out += ['--from', when(a.get('start'), 'start', all_day)]
        if a.get('end') is not None or not update:
            out += ['--to', when(a.get('end'), 'end', all_day)]
        if all_day:
            out.append('--all-day')
        elif not update:
            out += ['--timezone', TZ]
        else:
            # gog 0.47.1 'calendar update' has no --timezone flag (only create does); it takes one per end.
            if a.get('start') is not None:
                out += ['--start-timezone', TZ]
            if a.get('end') is not None:
                out += ['--end-timezone', TZ]
        reminders = a.get('reminders')
        if reminders is not None:
            if not isinstance(reminders, list) or len(reminders) > 5 or not all(isinstance(r, str) and REMINDER.fullmatch(r) for r in reminders):
                raise ValueError('reminders: up to 5 like popup:30m or email:1d.')
            for r in reminders:
                out += ['--reminder', r]
        return out

    def guests(self, a):
        g = a.get('guests') or []
        if not isinstance(g, list) or len(g) > 20 or not all(isinstance(x, str) and EMAIL.fullmatch(x) for x in g):
            raise ValueError('guests must be up to 20 plain email addresses.')
        return list(dict.fromkeys(g))

    def cal(self, a):
        c = a.get('calendar_id') or 'primary'
        if not isinstance(c, str) or not CAL_ID.fullmatch(c):
            raise ValueError('Use a calendar_id from calendar_calendars, or omit it for the main calendar.')
        return c

    def event(self, cal, event_id):
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise ValueError('Use an event id from calendar_events.')
        out = self.gog(['calendar', 'event', cal, event_id])
        return slim_event(pick(out, 'event') or out) or {}

    def review(self, op, args, lines):
        identifier = uuid.uuid4().hex[:16]
        h = hashlib.sha256(json.dumps([op, args], sort_keys=True).encode()).hexdigest()[:16]
        with self.b.db() as db:
            db.execute('INSERT INTO cal_approvals VALUES(?,?,?,?,?,?,NULL)', (identifier, h, op, json.dumps(args), 'pending', time.time()))
        self.b.provider.telegram('CALENDAR APPROVAL — ' + identifier + '\n\n' + '\n'.join(lines) +
                                 '\n\nApprove within 30 minutes with the ENTIRE line:\n/calapprove ' + identifier + ' ' + h)
        return {'status': 'awaiting_owner_approval', 'approval_id': identifier,
                'note': 'Sent to the owner in Telegram. Nothing changes until the owner approves; do not ask again.'}

    # --- actions -------------------------------------------------------------------------------
    def call(self, action, a, privileged):
        if action == 'cal_approve':
            return self.approve(a, privileged)
        self.ready()
        if action == 'cal_calendars':
            r = self.gog(['calendar', 'calendars'])
            items = pick(r, 'calendars', 'items') or []
            return {'calendars': [{'id': c.get('id'), 'name': c.get('summary'), 'primary': bool(c.get('primary')),
                                   'access': c.get('accessRole')} for c in items if isinstance(c, dict)][:50]}
        if action == 'cal_events':
            args = ['calendar', 'events']
            if a.get('all_calendars') is True:
                args.append('--all')
            else:
                args.append(self.cal(a))
            if a.get('start'):
                args += ['--from', when(a['start'], 'start') if 'T' in a['start'] else when(a['start'], 'start', True)]
            if a.get('end'):
                args += ['--to', when(a['end'], 'end') if 'T' in a['end'] else when(a['end'], 'end', True)]
            days = a.get('days')
            if days is not None:
                if type(days) is not int or not 1 <= days <= 31:
                    raise ValueError('days must be 1–31.')
                args += ['--days', str(days)]
            q = clean(a.get('query'), 'query', 200)
            if q:
                args += ['--query', q]
            mx = a.get('max', 25)
            if type(mx) is not int or not 1 <= mx <= 100:
                raise ValueError('max must be 1–100.')
            args += ['--max', str(mx), '--timezone', TZ]
            if a.get('all_calendars') is True:
                args += ['--sort', 'start']
            return {'events': events_from(self.gog(args)), 'timezone': TZ}
        if action == 'cal_freebusy':
            args = ['calendar', 'freebusy', '--all', '--from', when(a.get('start'), 'start'), '--to', when(a.get('end'), 'end')]
            return {'busy': self.gog(args), 'timezone': TZ,
                    'note': 'Busy blocks across all calendars; propose times outside them.'}
        if action == 'cal_create':
            guests = self.guests(a)
            cal = self.cal(a)
            base = ['calendar', 'create', cal] + self.event_args(a)
            if guests:
                # Invites email other people: owner approval first.
                return self.review('create', {'cmd': base + ['--attendees', ','.join(guests), '--send-updates', 'all'], 'request_id': a.get('request_id')},
                                   ['Create an event on your calendar and email invitations to:', ', '.join(guests),
                                    'Title: ' + str(a.get('summary')), 'When: ' + str(a.get('start')) + ' → ' + str(a.get('end')),
                                    'Where: ' + str(a.get('location') or '(none)')])
            return self.once(a.get('request_id'), 'cal_create',
                             lambda: {'created': slim_event(pick(self.gog(base + ['--send-updates', 'none'], True), 'event') or {}), 'guests_notified': False},
                             payload={k: a.get(k) for k in ('summary', 'start', 'end', 'all_day', 'location', 'description', 'calendar_id')})
        if action == 'cal_reminder':
            # A reminder is a short event at the reminder time whose phone notification fires at that minute
            # (Google Tasks due dates carry no time and no alert, so they are not used for reminders).
            text = clean(a.get('text'), 'text', 200, required=True)
            start = when(a.get('at'), 'at')
            begin = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
            minutes = a.get('minutes', 15)
            if type(minutes) is not int or not 5 <= minutes <= 240:
                raise ValueError('minutes must be 5–240.')
            end = (begin + dt.timedelta(minutes=minutes)).isoformat()
            lead = a.get('alert_minutes_before', 0)
            if type(lead) is not int or not 0 <= lead <= 1440:
                raise ValueError('alert_minutes_before must be 0–1440.')
            reminders = ['popup:%dm' % lead] + (['email:%dm' % lead] if a.get('email') is True else [])
            args = {'request_id': a.get('request_id'), 'summary': 'Reminder: ' + text, 'start': start, 'end': end,
                    'description': a.get('notes'), 'reminders': reminders}
            base = ['calendar', 'create', self.cal(a)] + self.event_args(args)
            return self.once(a.get('request_id'), 'cal_reminder',
                             lambda: {'created': slim_event(pick(self.gog(base + ['--send-updates', 'none'], True), 'event') or {}),
                                      'alert': reminders, 'note': 'Google Calendar will notify their phone at that time.'})
        if action == 'cal_update':
            cal = self.cal(a)
            current = self.event(cal, a.get('event_id'))
            add = self.guests(a)
            base = ['calendar', 'update', cal, a['event_id']] + self.event_args(a, update=True)
            if len(base) == 4 and not add:
                raise ValueError('Give at least one field to change.')
            others = current.get('guests') or []
            if others or add:
                cmd = base + (['--add-attendee', ','.join(add)] if add else []) + ['--send-updates', 'all']
                return self.review('update', {'cmd': cmd},
                                   ['Change "' + str(current.get('summary')) + '" (' + str(current.get('start')) + ') and notify guests:',
                                    ', '.join(others + add)] + [f'{k}: {a[k]}' for k in ('summary', 'start', 'end', 'location') if a.get(k)])
            return self.once(a.get('request_id'), 'cal_update',
                             lambda: {'updated': slim_event(pick(self.gog(base + ['--send-updates', 'none'], True), 'event') or {})},
                             payload={k: a.get(k) for k in ('event_id', 'summary', 'start', 'end', 'location', 'description', 'calendar_id')})
        if action == 'cal_delete_request':
            cal = self.cal(a)
            current = self.event(cal, a.get('event_id'))
            notify = bool(current.get('guests'))
            return self.review('delete', {'cmd': ['--force', 'calendar', 'delete', cal, a['event_id'], '--send-updates', 'all' if notify else 'none']},
                               ['Delete this event' + (' and notify its guests' if notify else '') + ':',
                                str(current.get('summary')), str(current.get('start')) + ' → ' + str(current.get('end'))])
        if action == 'tasks_lists':
            r = self.gog(['tasks', 'lists'])
            items = pick(r, 'tasklists', 'items', 'lists') or []
            return {'lists': [{'id': t.get('id'), 'title': t.get('title')} for t in items if isinstance(t, dict)]}
        if action == 'tasks_list':
            lid = a.get('list_id') or '@default'
            if not LIST_ID.fullmatch(lid):
                raise ValueError('Use a list_id from tasks_lists, or omit it for My Tasks.')
            r = self.gog(['tasks', 'list', lid, '--max', '100'])
            items = [x for x in (slim_task(t) for t in (pick(r, 'tasks', 'items') or [])) if x]
            if a.get('include_completed') is not True:
                items = [t for t in items if t.get('status') != 'completed']
            return {'tasks': items}
        if action in ('tasks_add', 'tasks_update', 'tasks_complete'):
            lid = a.get('list_id') or '@default'
            if not LIST_ID.fullmatch(lid):
                raise ValueError('Use a list_id from tasks_lists, or omit it for My Tasks.')
            due = a.get('due')
            if due is not None and not (isinstance(due, str) and (DATE.fullmatch(due) or RFC3339.fullmatch(due))):
                raise ValueError('due must be YYYY-MM-DD.')
            if action == 'tasks_add':
                args = ['tasks', 'add', lid, '--title', clean(a.get('title'), 'title', 300, True)]
                notes = clean(a.get('notes'), 'notes', 2000)
                if notes:
                    args += ['--notes', notes]
                if due:
                    args += ['--due', due]
                return self.once(a.get('request_id'), 'tasks_add', lambda: {'added': slim_task(pick(self.gog(args, True), 'task') or {})})
            tid = a.get('task_id')
            if not isinstance(tid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{5,200}', tid):
                raise ValueError('Use a task id from tasks_list.')
            if action == 'tasks_complete':
                return {'completed': slim_task(pick(self.gog(['tasks', 'done', lid, tid], True), 'task') or {}) or {'id': tid}}
            args = ['tasks', 'update', lid, tid]
            for key, flag, limit in (('title', '--title', 300), ('notes', '--notes', 2000)):
                v = clean(a.get(key), key, limit)
                if v:
                    args += [flag, v]
            if due:
                args += ['--due', due]
            if len(args) == 4:
                raise ValueError('Give at least one field to change.')
            return {'updated': slim_task(pick(self.gog(args, True), 'task') or {})}
        raise ValueError('Unknown calendar or tasks operation.')

    def approve(self, a, privileged):
        if not privileged:
            raise ValueError('Only the owner can approve calendar changes (/calapprove).')
        with self.b.db() as db:
            r = db.execute('SELECT * FROM cal_approvals WHERE id=?', (a.get('id'),)).fetchone()
        if not r or a.get('hash') != r['hash']:
            raise ValueError('That approval does not match a pending calendar change.')
        if r['status'] != 'pending':
            return {'id': r['id'], 'status': r['status'], 'replayed': True}
        if time.time() - r['created'] > 1800:
            with self.b.db() as db:
                db.execute("UPDATE cal_approvals SET status='expired' WHERE id=?", (r['id'],))
            raise ValueError('This calendar approval expired. Ask the assistant to prepare it again.')
        with self.b.db() as db:
            db.execute("UPDATE cal_approvals SET status='running' WHERE id=?", (r['id'],))
        stored = json.loads(r['args'])
        try:
            out = self.gog(stored['cmd'], True)
            status, result = 'done', slim_event(pick(out, 'event') or out) if r['op'] != 'delete' else {'deleted': True}
        except Exception:
            status, result = 'uncertain', None
        with self.b.db() as db:
            db.execute('UPDATE cal_approvals SET status=?,result=? WHERE id=?', (status, json.dumps(result), r['id']))
        return {'id': r['id'], 'op': r['op'], 'status': status, 'result': result}
