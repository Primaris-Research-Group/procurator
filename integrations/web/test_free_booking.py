"""Free bookings inside owner limits: one task approval, no per-click or final approvals.

Modeled on the Nov 4 CVS flu-shot run: several stores each offer "2:30 PM"; the page's
live region announces "5 of 18 location(s) near 31901, GA" (a notice, not an error).
"""
import tempfile
import unittest
from task_policy import _clock
from pathlib import Path
from broker import Broker
from task_policy import autonomous_decision, limits_check, normalize_task
from test_broker import Provider, control
from test_tasks import CONTEXT

ORIGIN = 'https://www.cvs.com'
SCHED = ORIGIN + '/scheduling/scheduler?lob=rximz&flow=vaccine'
REVIEW = ORIGIN + '/scheduling/review'
LIMITS = {'dates': ['2026-11-04', '2026-11-05'], 'time_from': '12:00', 'time_to': '17:00', 'places': ['Macon Rd']}


def button(ref, label, kind='button', **extra):
    return control(ref, kind, label) | {'tag': 'button', 'role': '', 'form_action': '', 'name': '', 'id': ''} | extra


def radio(ref, label, near):
    return control(ref, 'radio', label) | {'tag': 'input', 'role': '', 'form_action': '', 'name': 'slot', 'id': ref, 'near': near}


def spec(**extra):
    return {'mode': 'autonomous', 'origin': ORIGIN, 'fields': [], 'options': [], 'facts': ['31901'], 'spend_limit_usd': None,
            'read_urls': [], 'clicks': [], 'limits': LIMITS, 'free_booking_auto_confirm': True} | extra


CONFIRM_PAGE = 'Review your appointment\nFlu vaccine\nWednesday, November 4, 2026 at 2:30 PM\nCVS Pharmacy, 3127 Macon Rd, Columbus, GA\nStore hours 8:00 AM - 9:00 PM'


def final_plan(text=CONFIRM_PAGE, **extra):
    return {'url': REVIEW, 'steps': [{'kind': 'click', 'control': button('e9', 'Confirm appointment', 'submit')}], 'page_errors': [],
            'cookie_notice': False, 'detected_total': None, 'max_amount_on_page': None, 'payment_controls': [],
            'limits_check': limits_check(LIMITS, text)} | extra


class LimitsTests(unittest.TestCase):
    def test_reads_dates_times_and_places_from_the_page(self):
        c = limits_check(LIMITS, CONFIRM_PAGE)
        self.assertTrue(c['within']); self.assertIn('2026-11-04', c['dates_seen']); self.assertIn('14:30', c['times_seen'])
        self.assertTrue(limits_check(LIMITS, '11/05/2026 3:15 pm, 3127 MACON RD')['within'])
    def test_outside_or_missing_evidence_is_not_within(self):
        self.assertFalse(limits_check(LIMITS, 'November 6, 2026 at 2:30 PM, Macon Rd')['within'])      # date
        self.assertFalse(limits_check(LIMITS, 'November 4, 2026 at 5:30 PM, Macon Rd. Open 8:00 AM')['within'])  # time
        self.assertFalse(limits_check(LIMITS, 'November 4, 2026 at 2:30 PM, Wynnton Rd')['within'])  # place
        self.assertFalse(limits_check(LIMITS, 'Review your appointment')['within'])


class PolicyTests(unittest.TestCase):
    def test_free_final_inside_limits_is_preapproved(self):
        self.assertEqual(autonomous_decision(spec(), final_plan()), (True, 'final_preapproved'))
    def test_price_payment_outside_limits_error_or_no_policy_still_stop(self):
        for plan in [final_plan(detected_total=49.99), final_plan(max_amount_on_page=25.0), final_plan(payment_controls=['Card number']),
                     final_plan(text=CONFIRM_PAGE.replace('2:30 PM', '6:30 PM'))]:
            self.assertEqual(autonomous_decision(spec(), plan), (False, 'final'))
        self.assertEqual(autonomous_decision(spec(), final_plan(page_errors=['Please select a time']))[0], False)
        self.assertEqual(autonomous_decision(spec(free_booking_auto_confirm=False), final_plan()), (False, 'final'))
        consent = final_plan(); consent['steps'] = [{'kind': 'click', 'control': button('e9', 'Agree and confirm', 'submit')}]
        self.assertEqual(autonomous_decision(spec(), consent), (False, 'final'))
    def test_slot_radio_is_routine(self):
        p = {'url': SCHED, 'steps': [{'kind': 'click', 'control': radio('e3', '2:30 PM', 'Macon Rd 2.2 mi')}], 'page_errors': [], 'detected_total': None}
        self.assertEqual(autonomous_decision(spec(), p), (True, 'routine'))


class NormalizeTests(unittest.TestCase):
    session = {'id': 's1', 'origin': ORIGIN, 'practice': 0, 'timeout_seconds': 7200}
    def norm(self, **args):
        base = {'request_id': 'r' * 8, 'session_id': 's1', 'title': 'Flu shot', 'goal': 'Book a flu shot at CVS near 31901.', 'mode': 'autonomous'}
        return normalize_task(base | args, self.session, lambda u: (u, ORIGIN), lambda v, n=500: v if isinstance(v, str) else '')
    def test_limits_and_policy_validation(self):
        s = self.norm(limits=LIMITS, free_booking_auto_confirm=True)
        self.assertEqual(s['limits']['time_from'], '12:00'); self.assertTrue(s['free_booking_auto_confirm'])
        for bad in [{'free_booking_auto_confirm': True}, {'limits': {'places': ['Macon Rd']}, 'free_booking_auto_confirm': True},
                    {'limits': LIMITS, 'free_booking_auto_confirm': True, 'spend_limit_usd': 10}, {'limits': {'dates': ['Nov 4']}},
                    {'limits': {'time_from': '17:00', 'time_to': '12:00'}}, {'limits': {'time_from': '12:00'}}, {'free_booking_auto_confirm': 'yes', 'limits': LIMITS}]:
            with self.assertRaises(ValueError, msg=bad): self.norm(**bad)
        with self.assertRaises(ValueError):
            normalize_task({'request_id': 'r' * 8, 'session_id': 's1', 'title': 'x', 'goal': 'y', 'limits': LIMITS}, self.session, lambda u: (u, ORIGIN), lambda v, n=500: v)


class FlowTests(unittest.TestCase):
    """CVS: approve once; pick store/slot and confirm a free shot without further approvals."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = Provider(); self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        self.s = self.b.call('open', {'request_id': 'cvs-open-1', 'url': 'practice'})
        with self.b.db() as db: db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (ORIGIN, SCHED, self.s['id']))
    def tearDown(self): self.tmp.cleanup()
    def page(self, url, text, controls, errors=()):
        self.provider.snap.update({'url': url, 'title': 'Find Care | CVS Health', 'text': text, 'controls': controls, 'page_errors': list(errors)})
    def task(self, rid='cvs-task-1', limits=LIMITS):
        t = self.b.call('task_prepare', {'request_id': rid, 'session_id': self.s['id'], 'title': 'Flu shot', 'goal': 'Book a flu shot at CVS Macon Rd.',
                                         'mode': 'autonomous', 'facts': ['31901'], 'limits': limits, 'free_booking_auto_confirm': True})
        self.b.call('task_review', {'id': t['id']})
        msg = self.provider.messages[-1]
        self.assertIn('FREE BOOKING', msg); self.assertIn(_clock(limits['time_from']) + '–' + _clock(limits['time_to']), msg); self.assertIn('no further approval', msg); self.assertIn('Spending limit: none — free bookings only', msg)
        r = self.b.tasks.row(t['id']); self.b.call('task_activate', {'id': t['id'], 'hash': r['hash'][:16], 'context': CONTEXT}, True)
        return t['id']
    def step(self, t, rid, steps): return self.b.call('task_step', {'id': t, 'runtime_context': CONTEXT, 'request_id': 'cvs-' + rid, 'steps': steps})
    def test_one_approval_books_a_free_shot_and_reports_it(self):
        t = self.task(); before = len(self.provider.messages)
        self.page(SCHED, '5 of 18 location(s) near 31901, GA\nMacon Rd 2.2 mi\nWynnton Rd 3.1 mi',
                  [radio('e1', '2:30 PM', 'CVS 3127 Macon Rd 2.2 mi'), radio('e2', '2:30 PM', 'CVS 2401 Wynnton Rd 3.1 mi'), button('e3', 'Continue')])
        self.assertEqual(self.step(t, 'slot', [{'kind': 'click', 'ref': 'e1'}])['status'], 'completed')
        self.assertEqual(self.step(t, 'next', [{'kind': 'click', 'ref': 'e3'}])['status'], 'completed')
        self.page(REVIEW, CONFIRM_PAGE, [button('e9', 'Confirm appointment', 'submit')])
        done = self.step(t, 'confirm', [{'kind': 'click', 'ref': 'e9'}])
        self.assertEqual(done['status'], 'completed'); self.assertTrue(done['final_preapproved'])
        self.assertIn('FREE BOOKING CONFIRMED UNDER YOUR TASK APPROVAL', self.provider.messages[-1])
        self.assertEqual(len(self.provider.messages), before + 1)  # no review messages, only the confirmation notice
        self.provider.snap['text'] = 'You\'re all set! Your appointment is confirmed. Confirmation number 88213.'
        self.assertEqual(self.b.call('task_complete', {'id': t, 'runtime_context': CONTEXT})['status'], 'complete')
    def test_paid_or_outside_limit_final_comes_back_for_one_review_with_store_context(self):
        t = self.task()
        self.page(REVIEW, CONFIRM_PAGE.replace('2:30 PM', '6:30 PM'), [button('e9', 'Confirm appointment', 'submit')])
        stop = self.step(t, 'late', [{'kind': 'click', 'ref': 'e9'}])
        self.assertEqual((stop['status'], stop['stop_reason']), ('needs_separate_review', 'final')); self.assertEqual(self.provider.acts, 0)
        self.b.call('review', {'id': stop['action_id']}); self.assertIn('Your limits: NOT all confirmed', self.provider.messages[-1])
    def test_newer_review_supersedes_older_and_newer_task_replaces_older(self):
        t = self.task()
        self.page(REVIEW, CONFIRM_PAGE + '\nCopay $25.00', [button('e9', 'Confirm appointment', 'submit')])
        a = self.step(t, 'paid-1', [{'kind': 'click', 'ref': 'e9'}]); self.b.call('review', {'id': a['action_id']})
        old = self.b.plan(a['action_id'])
        self.page(SCHED, 'Pick a time', [radio('e1', '2:30 PM', 'Macon Rd'), radio('e2', '2:30 PM', 'Wynnton Rd')])
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'manual-slot-1', 'steps': [{'kind': 'click', 'ref': 'e2'}]})
        self.b.call('review', {'id': p['id']}); self.assertIn('(in "Wynnton Rd")', self.provider.messages[-1])
        self.assertEqual(self.b.plan(old['id'])['status'], 'superseded')
        with self.assertRaisesRegex(ValueError, 'replaced by a newer review'):
            self.b.call('execute', {'id': old['id'], 'hash': old['hash'][:16], 'context': CONTEXT}, True)
        t2 = self.task('cvs-task-2', {'dates': ['2026-11-04'], 'time_from': '14:00', 'time_to': '15:00'})
        self.assertEqual(self.b.tasks.row(t)['status'], 'cancelled'); self.assertEqual(self.b.tasks.row(t2)['status'], 'active')
    def test_status_announcements_are_not_page_errors_in_reviews(self):
        self.page(SCHED, 'x', [button('e3', 'Continue')], errors=[])
        self.provider.snap['page_notices'] = ['5 of 18 location(s) near 31901, GA']
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'notice-1', 'steps': [{'kind': 'click', 'ref': 'e3'}]})
        self.b.call('review', {'id': p['id']}); self.assertIn('Site errors: none', self.provider.messages[-1])


if __name__ == '__main__':
    unittest.main()
