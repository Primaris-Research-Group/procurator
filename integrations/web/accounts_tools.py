"""Model-facing Apple account tools on top of accounts.py / icloud_dav.py (web 0.29.0).

  accounts_list                what is connected (provider, email, which services are live). Never a password.
  calendar_apple_events        events from the connected iCloud account for a date range (default: the next 7 days), optionally one calendar.
  calendar_apple_create_event  one event in a named iCloud calendar; no attendees (an invite to someone else is a mail step with its own approval).
  contacts_apple_search        name / phone / email search over the iCloud address books (cached for ten minutes).

Everything that came from a calendar or address book is text other people may have written (an invitation title, a contact note), so
non-privileged callers get it wrapped as untrusted. Dates are ISO 8601; naive times are read in the owner's time zone.
"""
import datetime as dt
import os
import re
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from accounts import AccountError

TZ_NAME = os.environ.get('OPENCLAW_TZ', 'America/New_York')


def owner_tz():
    try:
        return ZoneInfo(TZ_NAME) if ZoneInfo else dt.timezone.utc
    except Exception:
        return dt.timezone.utc


def parse_when(value, name, all_day=False):
    s = str(value or '').strip()
    if not s:
        raise ValueError('%s is required (ISO 8601, e.g. 2026-11-20T14:00:00-05:00 or 2026-11-20 for a whole day).' % name)
    if all_day or re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            raise ValueError('%s is not a valid date.' % name) from None
    try:
        t = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('%s is not a valid ISO 8601 time.' % name) from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=owner_tz())
    return t


class AppleTools:
    def __init__(self, store, wrap=None, now=time.time, cache_ttl=600):
        self.store, self.wrap, self.now, self.ttl = store, wrap or (lambda text, source: text), now, cache_ttl
        self._contacts = {}

    def _account(self, service):
        a = self.store.for_service(service, 'icloud')
        if not a:
            raise AccountError('No Apple account with %s is connected. Connect one on the dashboard\'s Accounts tab (one email + one app-specific password).' % service)
        return a

    def call(self, action, a, privileged=False):
        a = a if isinstance(a, dict) else {}
        if action == 'accounts_list':
            return self.list()
        if action == 'calendar_apple_events':
            return self.events(a, privileged)
        if action == 'calendar_apple_create_event':
            return self.create_event(a)
        if action == 'contacts_apple_search':
            return self.contacts_search(a, privileged)
        raise ValueError('Unknown accounts action.')

    def list(self):
        rows = self.store.list()['accounts']
        return {'accounts': [{'provider': r['provider'], 'title': r['title'], 'email': r['email'], 'status': r['status'],
                              'services': {k: v.get('state') for k, v in r['services'].items()}} for r in rows],
                'note': 'Reminders, Notes and iMessage need the Mac companion; Google Calendar/Contacts/Tasks come through the Google connection (Settings > Connections).'}

    def _wrap(self, text, source, privileged):
        return text if privileged or not text else self.wrap(text, source)

    def events(self, a, privileged):
        acct = self._account('calendar')
        client = self.store.dav(acct['id'], 'calendar')
        tz = owner_tz()
        start = parse_when(a.get('start'), 'start') if a.get('start') else dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
            start = dt.datetime.combine(start, dt.time(), tzinfo=tz)
        days = int(a['days']) if str(a.get('days', '')).isdigit() and 1 <= int(a['days']) <= 60 else 7
        end = parse_when(a.get('end'), 'end') if a.get('end') else start + dt.timedelta(days=days)
        if isinstance(end, dt.date) and not isinstance(end, dt.datetime):
            end = dt.datetime.combine(end, dt.time(), tzinfo=tz) + dt.timedelta(days=1)
        if end <= start:
            raise ValueError('end must be after start.')
        if (end - start).days > 92:
            raise ValueError('Ask for at most three months at a time.')
        cals = client.calendars()
        want = str(a.get('calendar') or '').strip().lower()
        chosen = [c for c in cals if not want or c['name'].lower() == want]
        if want and not chosen:
            raise ValueError('No calendar named "%s". Calendars: %s.' % (want[:60], ', '.join(c['name'] for c in cals)))
        out = []
        for c in chosen:
            for e in client.events(c['url'], start, end, limit=200):
                out.append({'calendar': c['name'], 'summary': self._wrap(e.get('summary', ''), 'icloud-calendar', privileged), 'start': e.get('start'), 'end': e.get('end'),
                            'all_day': e.get('all_day', False), 'location': self._wrap(e.get('location'), 'icloud-calendar', privileged) if e.get('location') else None,
                            'recurring': bool(e.get('recurring')), 'uid': e.get('uid')})
        out.sort(key=lambda e: e['start'] or '')
        return {'account': acct['email'], 'calendars': [c['name'] for c in cals], 'from': start.isoformat(), 'to': end.isoformat(), 'events': out[:100], 'count': len(out)}

    def create_event(self, a):
        acct = self._account('calendar')
        client = self.store.dav(acct['id'], 'calendar')
        summary = str(a.get('summary') or '').strip()
        if not 1 <= len(summary) <= 200:
            raise ValueError('summary is required (1-200 characters).')
        all_day = bool(a.get('all_day'))
        start = parse_when(a.get('start'), 'start', all_day)
        end = parse_when(a.get('end'), 'end', all_day) if a.get('end') else None
        if all_day:
            if not isinstance(start, dt.date):
                raise ValueError('start must be a date for an all-day event.')
            end = end or start + dt.timedelta(days=1)
            if isinstance(end, dt.datetime):
                end = end.date()
            if end <= start:
                end = start + dt.timedelta(days=1)
        else:
            if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
                raise ValueError('start needs a time (or set all_day).')
            end = end or start + dt.timedelta(hours=1)
            if isinstance(end, dt.date) and not isinstance(end, dt.datetime):
                raise ValueError('end needs a time.')
            if end <= start:
                raise ValueError('end must be after start.')
        cals = client.calendars()
        if not cals:
            raise ValueError('The Apple account has no event calendar.')
        want = str(a.get('calendar') or '').strip().lower()
        cal = next((c for c in cals if c['name'].lower() == want), None) if want else cals[0]
        if not cal:
            raise ValueError('No calendar named "%s". Calendars: %s.' % (want[:60], ', '.join(c['name'] for c in cals)))
        r = client.create_event(cal['url'], summary, start, end, location=str(a.get('location') or '')[:300] or None,
                                description=str(a.get('description') or '')[:2000] or None, all_day=all_day)
        return {'created': True, 'account': acct['email'], 'calendar': cal['name'], 'uid': r['uid'], 'summary': summary,
                'start': start.isoformat(), 'end': end.isoformat(), 'all_day': all_day,
                'note': 'Created in their own iCloud calendar with no attendees. Inviting someone else is a separate mail step with its own approval.'}

    def contacts_search(self, a, privileged):
        acct = self._account('contacts')
        q = re.sub(r'\s+', ' ', str(a.get('query') or '').strip().lower())
        if len(q) < 2:
            raise ValueError('query needs at least two characters (a name, part of a phone number or an email).')
        cards = self._all_contacts(acct)
        digits = re.sub(r'\D', '', q)
        hits = []
        for c in cards:
            hay = ' '.join([c.get('name', ''), c.get('org', '')] + [e['address'] for e in c.get('emails', [])]).lower()
            phones = ''.join(re.sub(r'\D', '', p['number']) for p in c.get('phones', []))
            if q in hay or (len(digits) >= 4 and digits in phones):
                hits.append({'name': self._wrap(c.get('name', ''), 'icloud-contacts', privileged), 'org': self._wrap(c.get('org'), 'icloud-contacts', privileged) if c.get('org') else None,
                             'phones': c.get('phones', [])[:4], 'emails': c.get('emails', [])[:4], 'birthday': c.get('birthday')})
        return {'account': acct['email'], 'query': q, 'matches': hits[:20], 'count': len(hits), 'searched': len(cards)}

    def _all_contacts(self, acct):
        c = self._contacts.get(acct['id'])
        if c and self.now() - c['at'] < self.ttl:
            return c['cards']
        client = self.store.dav(acct['id'], 'contacts')
        cards = []
        for book in client.addressbooks():
            cards.extend(client.contacts(book['url']))
        self._contacts[acct['id']] = {'at': self.now(), 'cards': cards}
        return cards
