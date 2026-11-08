"""Appointment scheduling across pathways (web 0.23.0; build-plans/health/02-appointment-scheduling.md).

The booking itself is always ONE effect in health.py, whatever the pathway:
  * portal API  — health_portal_book / _cancel / _reschedule (health_portal.py)
  * clinic site — an autonomous browser task; health_effect_dispatch right before the final press, health_effect_settle after
  * phone       — health_call_brief builds the call from the effect and its grant; a transcript settles as REQUESTED, never confirmed,
                  until the portal/site shows it or the owner confirms it (/healthconfirm EFFECT)
Switching pathways never creates a second booking: the equivalence check in health.py stops it, and the brief/plan here are bound to
the effect.

Calendar reconciliation. A confirmed booking becomes one event on their Google Calendar through the existing calendar tool:
  health_calendar_plan(effect) → the exact calendar_create_event / calendar_update_event / calendar_delete_request arguments, with a
  request_id derived from the effect (health-cal-<effect>) — google_pim binds request ids to their content, so re-running the plan
  after a crash returns the same event instead of creating a second one — then health_calendar_record(effect, event_id).
  Event text follows the policy calendar_detail: minimal ("Appointment"), clinic (adds the clinic) or full (adds clinician and visit
  type). Diagnoses, medications and reasons never go into the calendar. The description carries only the PROCURATOR reference and the
  provider's confirmation id.
"""
import datetime as dt
import json
import re

from health import clean

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

DEFAULT_TZ = 'America/New_York'
CAL_ID = re.compile(r'^[A-Za-z0-9_@.-]{3,1024}$')


def localize(date, time, tz=DEFAULT_TZ, minutes=30):
    """(start, end) RFC3339 with the zone's offset ON THAT DATE (daylight-saving safe)."""
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(date or '')) or not re.fullmatch(r'\d{2}:\d{2}', str(time or '')):
        raise ValueError('The booking needs date YYYY-MM-DD and time HH:MM.')
    zone = ZoneInfo(tz) if ZoneInfo else dt.timezone(dt.timedelta(hours=-5))
    start = dt.datetime.fromisoformat('%sT%s:00' % (date, time)).replace(tzinfo=zone)
    end = start + dt.timedelta(minutes=minutes)
    return start.isoformat(), end.isoformat()


class Scheduling:
    def __init__(self, health):
        self.h = health
        with self.h.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS calendar_links(effect_id TEXT PRIMARY KEY,request_id TEXT,event_id TEXT,calendar_id TEXT,op TEXT,created REAL)')

    def effect(self, eid):
        with self.h.db() as db:
            e = db.execute('SELECT * FROM effects WHERE id=?', (clean(eid, 'effect', 16),)).fetchone()
        if not e:
            raise ValueError('Unknown effect.')
        return dict(e)

    def link_of(self, eid):
        with self.h.db() as db:
            r = db.execute('SELECT * FROM calendar_links WHERE effect_id=?', (eid,)).fetchone()
        return dict(r) if r else None

    def title(self, payload, connection):
        detail = self.h.policy().get('calendar_detail', 'minimal')
        if detail == 'full':
            return ('Appointment — %s%s' % (payload.get('clinician') or connection.get('institution') or '',
                                            (' (%s)' % payload['visit_type']) if payload.get('visit_type') else '')).strip(' —')
        if detail == 'clinic':
            return 'Appointment — %s' % (payload.get('location') or connection.get('institution') or 'clinic')
        return 'Appointment'

    # ---------- calendar ----------
    def calendar_plan(self, eid):
        e = self.effect(eid)
        payload = json.loads(e['payload'])
        with self.h.db() as db:
            conn = self.h.connection(db, e['connection'])
        link = self.link_of(e['id'])
        ref = 'PROCURATOR health %s%s' % (e['id'], (' · provider ref ' + e['external_id']) if e.get('external_id') else '')
        if e['action'] in ('appointment.book', 'appointment.reschedule'):
            if e['state'] != 'confirmed':
                return {'effect': e['id'], 'calendar': 'not_yet', 'why': 'only a CONFIRMED booking goes on the calendar (this one is %s%s)' % (
                        e['state'], ', requested' if (e.get('provider_state') or '').startswith(('requested', 'pending')) else '')}
            start, end = localize(payload.get('date'), payload.get('time'), payload.get('tz') or DEFAULT_TZ, int(payload.get('minutes') or 30))
            args = {'request_id': 'health-cal-' + e['id'], 'summary': self.title(payload, conn), 'start': start, 'end': end,
                    'description': ref, 'reminders': ['popup:1d', 'popup:2h']}
            if self.h.policy().get('calendar_detail') in ('clinic', 'full') and payload.get('location'):
                args['location'] = clean(str(payload['location']), 'location', 300)
            if e['action'] == 'appointment.reschedule':
                prev = self.previous_link(payload)
                if prev and prev.get('event_id'):
                    upd = {k: v for k, v in args.items() if k != 'reminders'}
                    upd['event_id'] = prev['event_id']
                    return {'effect': e['id'], 'calendar': 'update', 'tool': 'calendar_update_event', 'args': upd,
                            'then': 'health_calendar_record with this effect and the same event_id'}
            if link and link.get('event_id'):
                return {'effect': e['id'], 'calendar': 'done', 'event_id': link['event_id']}
            return {'effect': e['id'], 'calendar': 'create', 'tool': 'calendar_create_event', 'args': args,
                    'then': 'health_calendar_record with the event id it returns. If you are unsure whether it ran, call calendar_create_event again with the SAME request_id: it returns the same event.'}
        if e['action'] == 'appointment.cancel':
            if e['state'] != 'confirmed':
                return {'effect': e['id'], 'calendar': 'not_yet', 'why': 'the cancellation is not confirmed yet (%s)' % e['state']}
            prev = self.previous_link(payload)
            if not prev or not prev.get('event_id'):
                return {'effect': e['id'], 'calendar': 'nothing', 'why': 'no calendar event was recorded for the cancelled booking; check their calendar by date if needed'}
            return {'effect': e['id'], 'calendar': 'delete', 'tool': 'calendar_delete_request', 'args': {'event_id': prev['event_id']},
                    'then': 'The owner approves deletions in Telegram; then health_calendar_record with op=deleted.'}
        return {'effect': e['id'], 'calendar': 'nothing', 'why': 'only appointment effects touch the calendar'}

    def previous_link(self, payload):
        """The calendar event of the booking a cancel/reschedule refers to (of_effect, or the effect that created appointment_id)."""
        prev = payload.get('of_effect')
        if not prev and payload.get('appointment_id'):
            with self.h.db() as db:
                r = db.execute("SELECT id FROM effects WHERE external_id=? AND state='confirmed' AND action IN ('appointment.book','appointment.reschedule') ORDER BY created DESC LIMIT 1",
                               ('Appointment/' + str(payload['appointment_id']),)).fetchone()
            prev = r['id'] if r else None
        return self.link_of(prev) if prev else None

    def calendar_record(self, a):
        e = self.effect(a.get('effect'))
        op = a.get('op') or 'created'
        if op not in ('created', 'updated', 'deleted'):
            raise ValueError('op is created, updated or deleted.')
        event_id = clean(a.get('event_id') or '', 'event_id', 1024, required=op != 'deleted')
        if event_id and not CAL_ID.match(event_id):
            raise ValueError('That does not look like a calendar event id.')
        with self.h.db() as db:
            if op == 'updated':
                prev = self.previous_link(json.loads(e['payload']))
                if prev:
                    db.execute('UPDATE calendar_links SET op=? WHERE effect_id=?', ('moved to ' + e['id'], prev['effect_id']))
            db.execute('INSERT OR REPLACE INTO calendar_links VALUES(?,?,?,?,?,?)', (e['id'], 'health-cal-' + e['id'], event_id or None,
                       clean(a.get('calendar_id') or 'primary', 'calendar_id', 200), op, self.h.now()))
        return {'effect': e['id'], 'calendar': op, 'event_id': event_id or None}

    # ---------- phone pathway ----------
    def call_brief(self, eid):
        """The phone_prepare_call fields for a booking/cancel effect, built from its grant. The owner still approves the call (/callapprove)."""
        e = self.effect(eid)
        if e['state'] not in ('authorized', 'queued'):
            raise ValueError('Effect %s is %s; a call brief is only made for an authorized operation.' % (e['id'], e['state']))
        with self.h.db() as db:
            g = self.h.grant_for(db, e['grant_id'], e['action'], e['subject'], e['connection'])
            conn = self.h.connection(db, e['connection'])
        c = json.loads(g['constraints'])
        p = json.loads(e['payload'])
        what = {'appointment.book': 'book', 'appointment.cancel': 'cancel', 'appointment.reschedule': 'move'}.get(e['action'])
        if not what:
            raise ValueError('Call briefs are for appointment bookings, cancellations and reschedules.')
        limits = []
        for k, label in (('clinician', 'with'), ('provider', 'with'), ('specialty', 'specialty'), ('visit_type', 'visit type')):
            if c.get(k):
                limits.append('%s %s' % (label, c[k]))
        if c.get('locations'):
            limits.append('at ' + ' or '.join(map(str, c['locations'])))
        if c.get('weekdays'):
            limits.append('on ' + '/'.join(map(str, c['weekdays'])))
        if c.get('time_of_day'):
            limits.append('in the ' + str(c['time_of_day']))
        if c.get('time_windows'):
            limits.append('between ' + '; '.join('%s and %s' % (w.get('from'), w.get('to')) for w in c['time_windows'] if isinstance(w, dict)))
        cap = c.get('spend_cap_usd')
        target = ('a %s visit' % (p.get('visit_type') or 'routine')) + (' with %s' % p['clinician'] if p.get('clinician') else '')
        when = (' on %s at %s' % (p['date'], p['time'])) if p.get('date') and p.get('time') else ''
        task = {'book': 'Book %s for the owner%s.' % (target, when or ' at the earliest time that fits the limits below'),
                'cancel': 'Cancel the owner\'s appointment%s.' % ((' on %s at %s' % (p['date'], p['time'])) if p.get('date') else ''),
                'move': 'Move the owner\'s appointment to %s.' % (when.strip() or 'a time that fits the limits below')}[what]
        commitments = ('%s only %s.' % ({'book': 'Accept a time', 'cancel': 'Cancel', 'move': 'Accept a new time'}[what], ', '.join(limits) if limits else 'as described') +
                       (' Accept no charge.' if cap in (0, 0.0) else (' Accept charges only up to $%.0f.' % cap if cap is not None else ' Accept no charges; ask what any fee is and report it.')) +
                       ' Agree to nothing else (no other clinician, no new-patient paperwork commitments, no payment details, no cancellation of other visits).')
        disclosures = [d for d in json.loads(g['disclosures'])][:6]
        return {'effect': e['id'], 'for_tool': 'phone_prepare_call',
                'brief': {'task': clean(task, 'task', 400), 'context': 'Calling %s for an existing patient (The owner).' % conn['institution'],
                          'disclosures': ['May share: ' + d for d in disclosures] or ['Share only the owner\'s name and callback number'],
                          'allowed_commitments': clean(commitments, 'allowed_commitments', 400),
                          'success_criteria': 'The receptionist states the date, time, clinician and location' + (' and a confirmation number' if what != 'cancel' else ' of the cancelled visit') +
                                              '; repeat them back word for word. If they only take a request or a message, say so; that is not a booking.'},
                'next': ('Prepare the call with phone_prepare_call using these fields (fill the disclosures with the actual values only for what is listed). '
                         'Call health_effect_dispatch right before the call starts. After the call, health_effect_settle with source "phone transcript" and the '
                         'receptionist\'s exact words: it is recorded as REQUESTED until the portal shows it or the owner confirms it (/healthconfirm %s).' % e['id'])}

    # ---------- view ----------
    def bookings(self, a):
        with self.h.db() as db:
            rows = db.execute("SELECT * FROM effects WHERE action LIKE 'appointment.%' ORDER BY created DESC LIMIT 30").fetchall()
        out = []
        for r in rows:
            p = json.loads(r['payload'])
            link = self.link_of(r['id'])
            out.append({'effect': r['id'], 'action': r['action'], 'state': r['state'], 'provider_state': r['provider_state'], 'date': p.get('date'), 'time': p.get('time'),
                        'clinician': p.get('clinician'), 'external_id': r['external_id'], 'calendar_event': (link or {}).get('event_id'), 'calendar_op': (link or {}).get('op')})
        return {'bookings': out}

    def call(self, action, a, privileged=False):
        if action == 'health_calendar_plan':
            return self.calendar_plan(a.get('effect'))
        if action == 'health_calendar_record':
            return self.calendar_record(a)
        if action == 'health_call_brief':
            return self.call_brief(a.get('effect'))
        if action == 'health_bookings':
            return self.bookings(a)
        raise ValueError('Unknown scheduling action.')
