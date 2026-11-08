"""Phase 1 (0.17.0): fill/submit journal, verify-before-retry and idempotency keys."""
import json
import tempfile
import unittest
from pathlib import Path
from broker import Broker
from journal import idempotency_key, step_kind
from test_broker import Provider, control

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.provider = Provider()
        self.b = Broker(Path(self.tmp.name) / 't.sqlite3', self.provider)
        self.s = self.b.call('open', {'request_id': 'practice-journal', 'url': 'practice'})
    def tearDown(self): self.tmp.cleanup()
    def approve(self, rid, steps=None):
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': rid, 'steps': steps or [{'kind': 'click', 'ref': 'e1'}]})
        self.b.call('review', {'id': p['id']}); r = self.b.plan(p['id'])
        return self.b.call('execute', {'id': p['id'], 'hash': r['hash'][:16]}, True)

    def test_submit_is_journaled_and_fills_are_not_submits(self):
        self.approve('fill-0001', [{'kind': 'fill', 'ref': 'e0', 'value': 'Practice Visitor'}])
        self.assertEqual(self.approve('submit-0001')['status'], 'completed')
        with self.b.db() as db:
            rows = [dict(r) for r in db.execute('SELECT kind,cls,label,submitted_at FROM task_journal ORDER BY id')]
        self.assertEqual([r['kind'] for r in rows], ['fill', 'submit'])
        self.assertEqual(rows[1]['cls'], 'final'); self.assertIsNotNone(rows[1]['submitted_at'])
        self.assertIsNone(rows[0]['submitted_at'])

    def prepare(self, rid):
        return self.b.call('prepare', {'id': self.s['id'], 'request_id': rid, 'steps': [{'kind': 'click', 'ref': 'e1'}]})

    def test_second_press_refused_when_the_page_shows_it_went_through(self):
        early = self.prepare('submit-early')               # prepared before the first press (defence at execution)
        self.approve('submit-0001'); self.assertEqual(self.provider.acts, 1)
        self.provider.snap['text'] = 'Practice form\nThis absence has already been reported for Nov 3.'
        with self.assertRaisesRegex(ValueError, 'SECOND PRESS NOT OFFERED.*went through'):
            self.prepare('submit-0002')
        out = self.b.perform(self.b.plan(early['id']), approved=True)
        self.assertEqual(out['status'], 'refused_duplicate'); self.assertIn('Do NOT press it again', out['refused'])
        self.assertEqual(self.provider.acts, 1)
        self.assertIn('SECOND PRESS BLOCKED', self.provider.messages[-2] if 'Live page' in self.provider.messages[-1] else self.provider.messages[-1])
        # A restart (deploy, crash, /webcontinue) does not forget it.
        b2 = Broker(self.b.path, self.provider)
        with self.assertRaisesRegex(ValueError, 'SECOND PRESS NOT OFFERED'):
            b2.call('prepare', {'id': self.s['id'], 'request_id': 'submit-0003', 'steps': [{'kind': 'click', 'ref': 'e1'}]})
        self.assertEqual(self.provider.acts, 1)

    def test_confirmation_number_also_counts_as_submitted(self):
        self.approve('submit-0001')
        self.provider.snap['text'] = 'Thank you. Your confirmation number is A-12345.'
        with self.assertRaises(ValueError): self.prepare('submit-0002')
        self.assertEqual(self.provider.acts, 1)

    def test_qa_t3_04_practice_confirmation_with_the_button_still_there(self):
        # Nov 6 QA: the practice page keeps its Submit button after a successful press.
        early = self.prepare('submit-early')
        self.approve('submit-0001')
        self.provider.snap['text'] = 'Practice appointment form\nPRACTICE CONFIRMED: Deploy Test / afternoon'
        with self.assertRaisesRegex(ValueError, 'not offered for approval again'):
            self.prepare('submit-0002')
        self.assertEqual(self.b.perform(self.b.plan(early['id']), approved=True)['status'], 'refused_duplicate')
        self.assertEqual(self.provider.acts, 1)

    def test_no_confirmation_and_no_error_is_unknown_and_blocks(self):
        early = self.prepare('submit-early')
        self.approve('submit-0001')
        # Same page, button still there, nothing new on the page: it may well have gone through.
        with self.assertRaisesRegex(ValueError, 'may have gone through'):
            self.prepare('submit-0002')
        out = self.b.perform(self.b.plan(early['id']), approved=True)
        self.assertEqual(out['status'], 'refused_duplicate'); self.assertIn('/webtakeover', out['refused'])
        self.assertEqual(self.provider.acts, 1)

    def test_a_new_error_after_the_press_allows_a_freshly_approved_retry_only(self):
        early = self.prepare('submit-early')
        self.approve('submit-0001')
        # 0.21.0: the error must be bound to the submitted form (an alert inside it or a field marked invalid).
        self.provider.snap['text'] = 'Practice form\nError: that time slot was just taken. Please try again.'
        self.provider.snap['form_errors'] = [{'form': 'about:blank', 'text': 'Error: that time slot was just taken. Please try again.'}]
        out = self.b.perform(self.b.plan(early['id']), approved=False)       # a task's earlier grant is not enough
        self.assertEqual(out['status'], 'refused_duplicate'); self.assertIn('NEW owner review', out['refused'])
        self.assertEqual(self.approve('submit-0002')['status'], 'completed'); self.assertEqual(self.provider.acts, 2)

    def test_an_error_that_was_already_on_the_page_proves_nothing(self):
        self.provider.snap['text'] = 'Practice form\nFields marked * are required.'
        self.provider.snap['form_errors'] = [{'form': 'about:blank', 'text': 'Fields marked * are required.'}]
        self.approve('submit-0001')
        with self.assertRaisesRegex(ValueError, 'may have gone through'):
            self.prepare('submit-0002')

    def test_a_page_wide_error_unrelated_to_the_form_never_unlocks_a_second_press(self):
        # Nov 6 review finding (build-plans/health/README.md): a new page error allowed a fresh retry. Now only a form-bound error does.
        early = self.prepare('submit-early')
        self.approve('submit-0001')
        self.provider.snap['text'] = 'Practice form\nError: the weather widget could not load. Please try again later.'
        self.provider.snap['page_errors'] = ['Error: the weather widget could not load.']
        with self.assertRaisesRegex(ValueError, 'not attached to the submitted form'):
            self.prepare('submit-0002')
        self.assertEqual(self.b.perform(self.b.plan(early['id']), approved=True)['status'], 'refused_duplicate')
        self.assertEqual(self.provider.acts, 1)
        # An older driver without form_errors can never prove a failure either.
        del self.provider.snap['form_errors']
        with self.assertRaisesRegex(ValueError, 'cannot be proven failed'):
            self.prepare('submit-0003')

    def test_old_approval_is_never_reused(self):
        p = self.b.call('prepare', {'id': self.s['id'], 'request_id': 'submit-0001', 'steps': [{'kind': 'click', 'ref': 'e1'}]})
        self.b.call('review', {'id': p['id']}); h = self.b.plan(p['id'])['hash'][:16]
        self.b.call('execute', {'id': p['id'], 'hash': h}, True)
        self.assertEqual(self.b.call('execute', {'id': p['id'], 'hash': h}, True)['status'], 'completed')
        self.assertEqual(self.provider.acts, 1)

    def test_idempotency_key_survives_ref_renumbering_but_not_another_page(self):
        plan = {'url': 'https://services.leaveco.example/absence/review?x=1#top', 'steps': []}
        a = {'kind': 'click', 'control': control('e1', 'submit', 'Submit') | {'tag': 'button', 'form_action': 'https://services.leaveco.example/absence'}}
        b = {'kind': 'click', 'control': control('e9', 'submit', 'Submit') | {'tag': 'button', 'form_action': 'https://services.leaveco.example/absence'}}
        self.assertEqual(idempotency_key('t1', 0, a, plan), idempotency_key('t1', 0, b, plan))
        self.assertNotEqual(idempotency_key('t1', 0, a, plan), idempotency_key('t1', 0, a, {**plan, 'url': 'https://services.leaveco.example/absence/other'}))
        self.assertNotEqual(idempotency_key('t1', 0, a, plan), idempotency_key('t2', 0, a, plan))
        self.assertEqual(step_kind(a, plan), 'submit')
        self.assertEqual(step_kind({'kind': 'click', 'control': control('e2', 'button', 'Next') | {'tag': 'button'}}, plan), 'click')

    def test_not_dispatched_press_does_not_count(self):
        original = self.provider.run
        def covered(s, op, **kw):
            if op == 'act':
                self.provider.acts += 1
                return {'ok': False, 'kind': 'not_dispatched', 'reason': 'control_not_clickable'}
            return original(s, op, **kw)
        self.provider.run = covered
        self.assertEqual(self.approve('submit-0001')['status'], 'not_dispatched')
        self.provider.run = original
        self.assertEqual(self.approve('submit-0002')['status'], 'completed')

if __name__ == '__main__':
    unittest.main()
