"""Cancel / reschedule through the portal, the phone pathway, owner confirmation and calendar reconciliation (web 0.23.0;
build-plans/health/02-appointment-scheduling.md). Synthetic EHR on loopback."""
import json
import urllib.request

from health_scheduling import Scheduling, localize
from test_health_portal import Base


class Sched(Base):
    def setUp(self):
        super().setUp()
        self.s = Scheduling(self.h)
        for p in ('health_calendar_', 'health_call_', 'health_bookings'):
            self.h.attach(p, self.s)

    def slot(self, c, g, **f):
        return self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-rivera', 'service_type': 'follow-up', **f})['items'][0]

    def payload(self, s, **extra):
        return {'clinician': s['clinician'], 'location': s['location'], 'date': s['date'], 'time': s['time'], 'weekday': s['weekday'],
                'time_of_day': s['time_of_day'], 'visit_type': s['visit_type'], 'cost_usd': 0, 'slot_id': s['slot_id'], **extra}

    def booked(self, c, g, rid='visit-book-0001', **f):
        s = self.slot(c, g, **f)
        e = self.h.call('health_effect_begin', {'request_id': rid, 'action': 'appointment.book', 'connection': c['id'], 'grant': g['id'], 'payload': self.payload(s)})
        out = self.h.call('health_portal_book', {'effect': e['id']})
        self.assertEqual(out['effect']['state'], 'confirmed', out)
        return out['effect']

    def effect(self, c, g, action, payload, rid):
        return self.h.call('health_effect_begin', {'request_id': rid, 'action': action, 'connection': c['id'], 'grant': g['id'], 'payload': payload})

    def appt(self, eid_or_ext):
        aid = eid_or_ext.replace('Appointment/', '')
        return self.ehr.inst['riverside']['appts'][aid]

    def full_grant(self, c, **k):
        return self.grant(c, actions=('appointment.search', 'appointment.book', 'appointment.cancel', 'appointment.reschedule', 'portal.read'),
                          cancellation_policy='no_fee_only', reschedule_order='book_then_cancel', **k)


class CancelTests(Sched):
    def test_cancel_by_effect_reference_and_the_slot_frees_for_a_new_booking(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 0}, 'visit-cancel-0001')
        self.assertEqual(e['state'], 'authorized', e)
        out = self.h.call('health_portal_cancel', {'effect': e['id']})
        self.assertEqual((out['cancelled'], out['effect']['state']), (True, 'confirmed'))
        self.assertEqual(self.appt(b['external_id'])['status'], 'cancelled')
        # The cancelled booking no longer blocks booking that visit again.
        again = self.booked(c, g, rid='visit-book-0002')
        self.assertEqual(again['state'], 'confirmed')

    def test_a_cancel_with_full_visit_details_is_not_mistaken_for_a_duplicate_booking(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        p = dict(b['payload'], appointment_id=b['external_id'].split('/')[1], fee_usd=0)
        e = self.effect(c, g, 'appointment.cancel', p, 'visit-cancel-0002')
        self.assertEqual(e['state'], 'authorized', e)
        dup = self.effect(c, g, 'appointment.cancel', dict(p, note_x=1), 'visit-cancel-0003')
        self.assertEqual(dup['state'], 'awaiting_owner')            # but two cancels of the same appointment are

    def test_unknown_fee_or_a_fee_stops_a_fee_free_only_grant(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        self.assertEqual(self.effect(c, g, 'appointment.cancel', {'of_effect': b['id']}, 'visit-cancel-0004')['state'], 'awaiting_owner')
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 25}, 'visit-cancel-0005')
        self.assertEqual(e['state'], 'awaiting_owner'); self.assertIn('$25.00', e['note'])

    def test_a_lost_answer_after_the_portal_cancelled_is_read_back(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 0}, 'visit-cancel-0006')
        self.fault('riverside', cancel_commit_then_504=1)
        out = self.h.call('health_portal_cancel', {'effect': e['id']})
        self.assertEqual(out['effect']['state'], 'confirmed'); self.assertEqual(out['effect']['source'], 'portal read')
        self.assertEqual(self.appt(b['external_id'])['status'], 'cancelled')

    def test_a_concurrent_change_at_the_portal_is_refused_not_overwritten(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 0}, 'visit-cancel-0007')
        orig = self.p.target_appointment
        def stale(*a, **k):
            appt, etag = orig(*a, **k)
            self.appt(appt['id'])['meta']['versionId'] = str(int(self.appt(appt['id'])['meta']['versionId']) + 1)   # clinic staff edited it meanwhile
            return appt, etag
        self.p.target_appointment = stale
        out = self.h.call('health_portal_cancel', {'effect': e['id']})
        self.assertEqual((out['cancelled'], out['effect']['state']), (False, 'failed')); self.assertIn('changed at the portal', out['effect']['note'])
        self.assertEqual(self.appt(b['external_id'])['status'], 'booked')

    def test_already_cancelled_is_reported_as_done(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        self.appt(b['external_id'])['status'] = 'cancelled'
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 0}, 'visit-cancel-0008')
        out = self.h.call('health_portal_cancel', {'effect': e['id']})
        self.assertEqual((out['cancelled'], out['effect']['state']), (True, 'confirmed')); self.assertIn('already', out['note'])

    def test_a_grant_without_cancel_authority_never_reaches_the_portal(self):
        c = self.link(); g = self.grant(c)
        b = self.booked(c, g)
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 0}, 'visit-cancel-0009')
        self.assertEqual(e['state'], 'awaiting_owner'); self.assertIn('does not allow appointment.cancel', e['note'])
        self.assertEqual(self.h.call('health_portal_cancel', {'effect': e['id']})['error'], 'bad_state')
        self.assertEqual(self.appt(b['external_id'])['status'], 'booked')


class RescheduleTests(Sched):
    def test_reschedule_needs_the_approved_order(self):
        c = self.link()
        g = self.grant(c, actions=('appointment.search', 'appointment.book', 'appointment.reschedule', 'portal.read'), cancellation_policy='no_fee_only')
        b = self.booked(c, g)
        s2 = self.slot(c, g, **{'from': '2026-11-17'})
        e = self.effect(c, g, 'appointment.reschedule', self.payload(s2, of_effect=b['id'], fee_usd=0), 'visit-move-0001')
        self.assertEqual(e['state'], 'awaiting_owner'); self.assertIn('book_then_cancel', e['note'])

    def test_book_the_new_time_then_cancel_the_old_and_the_calendar_event_moves(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        plan = self.h.call('health_calendar_plan', {'effect': b['id']})
        self.h.call('health_calendar_record', {'effect': b['id'], 'event_id': 'evt_original01'})
        s2 = self.slot(c, g, **{'from': '2026-11-17'})
        e = self.effect(c, g, 'appointment.reschedule', self.payload(s2, of_effect=b['id'], fee_usd=0), 'visit-move-0002')
        self.assertEqual(e['state'], 'authorized', e)
        out = self.h.call('health_portal_reschedule', {'effect': e['id']})
        self.assertEqual((out['rescheduled'], out['effect']['state']), (True, 'confirmed'))
        self.assertEqual(self.appt(b['external_id'])['status'], 'cancelled')
        self.assertEqual(self.appt(out['effect']['external_id'])['status'], 'booked')
        mv = self.h.call('health_calendar_plan', {'effect': e['id']})
        self.assertEqual((mv['calendar'], mv['tool'], mv['args']['event_id']), ('update', 'calendar_update_event', 'evt_original01'))
        self.assertTrue(mv['args']['start'].startswith(s2['date'])); self.assertNotEqual(mv['args']['start'], plan['args']['start'])
        self.h.call('health_calendar_record', {'effect': e['id'], 'event_id': 'evt_original01', 'op': 'updated'})
        rows = {r['effect']: r for r in self.h.call('health_bookings', {})['bookings']}
        self.assertEqual(rows[e['id']]['calendar_event'], 'evt_original01')

    def test_a_reschedule_to_the_same_day_is_not_blocked_by_the_booking_it_replaces(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        later = [x for x in self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-rivera',
                                                             'service_type': 'follow-up', 'from': b['payload']['date']})['items']
                 if x['date'] == b['payload']['date'] and x['time'] != b['payload']['time']]
        self.assertTrue(later)
        e = self.effect(c, g, 'appointment.reschedule', self.payload(later[0], of_effect=b['id'], fee_usd=0), 'visit-move-0003')
        self.assertEqual(e['state'], 'authorized', e)

    def test_when_the_old_cancel_fails_both_are_named_and_nothing_is_retried(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        s2 = self.slot(c, g, **{'from': '2026-11-17'})
        e = self.effect(c, g, 'appointment.reschedule', self.payload(s2, of_effect=b['id'], fee_usd=0), 'visit-move-0004')
        orig = self.p.cancel_write
        self.p.cancel_write = lambda *a, **k: ('refused', 'the clinic does not allow online cancellation within 24 hours')
        out = self.h.call('health_portal_reschedule', {'effect': e['id']})
        self.p.cancel_write = orig
        self.assertEqual(out['rescheduled'], 'partial'); self.assertEqual(len(out['double_booked']), 2)
        self.assertIn('OLD NOT CANCELLED', out['effect']['provider_state'])
        self.assertEqual(self.appt(b['external_id'])['status'], 'booked')
        self.assertIn('both appointments exist', out['next'])

    def test_a_request_only_clinic_keeps_the_old_appointment(self):
        c = self.link('northgate', 'jordan', 'northgate-test')
        g = self.full_grant(c, clinician='Okafor')
        first = self.h.call('health_portal_read', {'connection': c['id'], 'grant': g['id'], 'what': 'slots', 'practitioner': 'pr-okafor'})['items']
        e0 = self.effect(c, g, 'appointment.book', self.payload(first[0]), 'ng-book-0001')
        self.h.call('health_portal_book', {'effect': e0['id']})
        aid = self.h.public_effect_by_id(e0['id'])['external_id'].split('/')[1]
        urllib.request.urlopen(urllib.request.Request(self.ehr.public + '/_admin/northgate/confirm', data=json.dumps({'appointment': aid}).encode(),
                                                      headers={'X-Admin-Token': self.ehr.admin_token}, method='POST')).read()
        self.h.call('health_portal_reconcile', {'effect': e0['id']})
        other = [x for x in first if x['date'] != first[0]['date']] or first[1:]
        e = self.effect(c, g, 'appointment.reschedule', self.payload(other[0], appointment_id=aid, fee_usd=0), 'ng-move-0001')
        out = self.h.call('health_portal_reschedule', {'effect': e['id']})
        self.assertEqual((out['rescheduled'], out.get('requested'), out['effect']['state']), (False, True, 'uncertain'))
        self.assertEqual(self.ehr.inst['northgate']['appts'][aid]['status'], 'booked')
        self.assertEqual(self.h.call('health_calendar_plan', {'effect': e['id']})['calendar'], 'not_yet')

    def test_the_new_slot_gone_keeps_the_old_appointment(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        s2 = self.slot(c, g, **{'from': '2026-11-17'})
        self.ehr.inst['riverside']['slots'][s2['slot_id']]['status'] = 'busy'
        e = self.effect(c, g, 'appointment.reschedule', self.payload(s2, of_effect=b['id'], fee_usd=0), 'visit-move-0005')
        out = self.h.call('health_portal_reschedule', {'effect': e['id']})
        self.assertEqual((out['rescheduled'], out['effect']['state']), (False, 'failed'))
        self.assertEqual(self.appt(b['external_id'])['status'], 'booked')


class PhoneAndOwnerTests(Sched):
    def test_a_call_brief_is_built_from_the_grant_and_the_transcript_settles_as_requested(self):
        c = self.link()
        g = self.grant(c, weekdays=['Tuesday', 'Thursday'], time_of_day='afternoon')
        s = self.slot(c, g)
        e = self.effect(c, g, 'appointment.book', self.payload(s, channel='phone'), 'visit-phone-0001')
        brief = self.h.call('health_call_brief', {'effect': e['id']})
        b = brief['brief']
        self.assertEqual(brief['for_tool'], 'phone_prepare_call')
        self.assertIn('with Rivera', b['allowed_commitments']); self.assertIn('Tuesday/Thursday', b['allowed_commitments'])
        self.assertIn('Accept no charge', b['allowed_commitments']); self.assertIn('repeat them back', b['success_criteria'])
        self.h.call('health_effect_dispatch', {'effect': e['id']})
        out = self.h.call('health_effect_settle', {'effect': e['id'], 'outcome': 'confirmed', 'source': 'phone transcript',
                                                    'detail': 'Receptionist: you are all set for Tuesday at 2:30 with Dr. Rivera'})
        self.assertEqual(out['state'], 'uncertain'); self.assertTrue(out['provider_state'].startswith('requested'))
        self.assertEqual(self.h.call('health_calendar_plan', {'effect': e['id']})['calendar'], 'not_yet')
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_effect_owner_confirm', {'effect': e['id']})
        done = self.h.call('health_effect_owner_confirm', {'effect': e['id'], 'external_id': 'RIV-55812'}, True)
        self.assertEqual((done['state'], done['source'], done['external_id']), ('confirmed', 'owner', 'RIV-55812'))
        self.assertEqual(self.h.call('health_calendar_plan', {'effect': e['id']})['calendar'], 'create')

    def test_no_call_brief_outside_the_grant(self):
        c = self.link(); g = self.grant(c, weekdays=['Thursday'])
        e = self.effect(c, g, 'appointment.book', self.payload(self.slot(c, g)), 'visit-phone-0002')
        with self.assertRaisesRegex(ValueError, 'awaiting_owner'):
            self.h.call('health_call_brief', {'effect': e['id']})

    def test_a_portal_booking_after_a_phone_request_is_stopped_as_equivalent(self):
        c = self.link(); g = self.grant(c)
        s = self.slot(c, g)
        e = self.effect(c, g, 'appointment.book', self.payload(s, channel='phone'), 'visit-phone-0003')
        self.h.call('health_effect_dispatch', {'effect': e['id']})
        self.h.call('health_effect_settle', {'effect': e['id'], 'outcome': 'requested', 'source': 'phone call', 'detail': 'they will call back to confirm'})
        e2 = self.effect(c, g, 'appointment.book', self.payload(s), 'visit-web-0003')
        self.assertEqual(e2['state'], 'awaiting_owner'); self.assertEqual(e2['duplicate_of'], e['id'])


class CalendarTests(Sched):
    def test_only_confirmed_bookings_and_minimal_text_by_default(self):
        c = self.link(); g = self.grant(c)
        b = self.booked(c, g)
        plan = self.h.call('health_calendar_plan', {'effect': b['id']})
        a = plan['args']
        self.assertEqual((plan['calendar'], plan['tool'], a['summary'], a['request_id']), ('create', 'calendar_create_event', 'Appointment', 'health-cal-' + b['id']))
        self.assertNotIn('location', a); self.assertNotIn('Rivera', json.dumps(a)); self.assertNotIn('follow', json.dumps(a).lower())
        self.assertIn(b['id'], a['description'])
        self.assertEqual(plan, self.h.call('health_calendar_plan', {'effect': b['id']}))       # the same plan on retry: same request id
        self.h.call('health_calendar_record', {'effect': b['id'], 'event_id': 'evt_abc123'})
        self.assertEqual(self.h.call('health_calendar_plan', {'effect': b['id']})['calendar'], 'done')

    def test_detail_levels_follow_the_policy(self):
        c = self.link(); g = self.grant(c)
        b = self.booked(c, g)
        self.h.call('health_policy', {'set': {'calendar_detail': 'clinic'}}, True)
        a = self.h.call('health_calendar_plan', {'effect': b['id']})['args']
        self.assertTrue(a['summary'].startswith('Appointment — ')); self.assertIn('location', a); self.assertNotIn('Rivera', a['summary'])
        self.h.call('health_policy', {'set': {'calendar_detail': 'full'}}, True)
        self.assertIn('Rivera', self.h.call('health_calendar_plan', {'effect': b['id']})['args']['summary'])
        with self.assertRaises(ValueError):
            self.h.call('health_policy', {'set': {'calendar_detail': 'everything'}}, True)

    def test_a_cancelled_booking_plans_a_deletion_request(self):
        c = self.link(); g = self.full_grant(c)
        b = self.booked(c, g)
        self.h.call('health_calendar_record', {'effect': b['id'], 'event_id': 'evt_tocancel1'})
        e = self.effect(c, g, 'appointment.cancel', {'of_effect': b['id'], 'fee_usd': 0}, 'visit-cancel-0010')
        self.h.call('health_portal_cancel', {'effect': e['id']})
        plan = self.h.call('health_calendar_plan', {'effect': e['id']})
        self.assertEqual((plan['calendar'], plan['tool'], plan['args']), ('delete', 'calendar_delete_request', {'event_id': 'evt_tocancel1'}))

    def test_times_carry_the_offset_of_that_date(self):
        self.assertEqual(localize('2026-11-10', '14:30')[0], '2026-11-10T14:30:00-05:00')
        self.assertEqual(localize('2026-07-10', '09:00', minutes=45), ('2026-07-10T09:00:00-04:00', '2026-07-10T09:45:00-04:00'))
        with self.assertRaises(ValueError):
            localize('11/10/2026', '2pm')

    def test_a_bad_event_id_is_refused(self):
        c = self.link(); g = self.grant(c)
        b = self.booked(c, g)
        with self.assertRaisesRegex(ValueError, 'calendar event id'):
            self.h.call('health_calendar_record', {'effect': b['id'], 'event_id': 'x y<script>'})


if __name__ == '__main__':
    import unittest
    unittest.main()
