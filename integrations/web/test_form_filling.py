"""Signed-in form filling (Nov 5, Leaveco absence): one task, the bot fills the form, the owner approves the final submit."""
import json
import tempfile
import unittest
from pathlib import Path
from broker import Broker, page_warnings
from task_policy import autonomous_decision, free_value_ok, normalize_task, preview
from test_broker import Provider, control
from test_tasks import CONTEXT

LEAVECO = 'https://services.leaveco.example'
FORM = LEAVECO + '/CLMSTTS/absence/absenceentry.aspx?claimId=1000'
TOOL_CONTEXT = {k: v for k, v in CONTEXT.items() if k != 'accountId'}   # the assistant's tool call may not name the account


def button(ref, label, kind='submit'):
    return control(ref, kind, label) | {'tag': 'button', 'role': '', 'name': '', 'id': '', 'form_action': ''}


def spec(**extra):
    return {'mode': 'autonomous', 'site_login': 'leaveco', 'facts': [], 'fields': [], 'options': [], 'spend_limit_usd': None, 'fill_form': True} | extra


def fill(label, value, url=FORM):
    return {'url': url, 'steps': [{'kind': 'fill', 'control': control('e1', 'text', label), 'value': value}], 'page_errors': [], 'detected_total': None}


class PolicyTests(unittest.TestCase):
    def test_owner_answers_are_typed_without_a_review(self):
        for label, value in [('Begin time', '08:00'), ('End Time', '17:00'), ('Duration of your absence in hours', '9'), ('Comments', 'Episode of incapacity')]:
            self.assertEqual(autonomous_decision(spec(), fill(label, value)), (True, 'routine'), label)
    def test_without_form_filling_unlisted_values_still_stop(self):
        self.assertEqual(autonomous_decision(spec(fill_form=False), fill('Begin time', '08:00'))[1], 'value_not_approved')
    def test_secrets_ids_cards_long_text_and_account_settings_still_stop(self):
        for value in ['123-45-6789', '123456789', '4111 1111 1111 1111', 'my password is x', 'y' * 301]:
            self.assertFalse(free_value_ok(value), value)
            self.assertEqual(autonomous_decision(spec(), fill('Notes', value))[1], 'value_not_approved', value)
        self.assertEqual(autonomous_decision(spec(), fill('Password', 'abc'))[1], 'account_protected')
        self.assertEqual(autonomous_decision(spec(), fill('Direct deposit routing', '061000052'))[1], 'account_protected')
        self.assertEqual(autonomous_decision(spec(), fill('Phone number', '7065550100', LEAVECO + '/profile/contact'))[1], 'account_protected')
    def test_final_submit_still_stops(self):
        submit = {'url': FORM, 'steps': [{'kind': 'click', 'control': button('e9', 'Submit Absence')}], 'page_errors': [], 'detected_total': None}
        self.assertEqual(autonomous_decision(spec(), submit), (False, 'final'))
    def test_form_filling_defaults_on_for_signed_in_sites_only(self):
        args = {'request_id': 'r' * 8, 'session_id': 's1', 'title': 'Leaveco absence', 'goal': 'Report the Nov 5 absence.', 'mode': 'autonomous'}
        ok = lambda v, n=500: v if isinstance(v, str) else ''
        signed = {'id': 's1', 'origin': LEAVECO, 'practice': 0, 'timeout_seconds': 7200, 'site_login': 'leaveco'}
        public = {'id': 's1', 'origin': LEAVECO, 'practice': 0, 'timeout_seconds': 7200}
        s = normalize_task(args, signed, lambda u: (u, LEAVECO), ok)
        self.assertTrue(s['fill_form']); self.assertIn('FORM FILLING', preview(s)); self.assertIn('every entry', preview(s))
        self.assertFalse(normalize_task(args, public, lambda u: (u, LEAVECO), ok)['fill_form'])
        self.assertFalse(normalize_task(args | {'fill_form': False}, signed, lambda u: (u, LEAVECO), ok)['fill_form'])
        with self.assertRaises(ValueError): normalize_task(args | {'fill_form': 'yes'}, signed, lambda u: (u, LEAVECO), ok)
        with self.assertRaises(ValueError): normalize_task({k: v for k, v in args.items() if k != 'mode'} | {'fill_form': True}, signed, lambda u: (u, LEAVECO), ok)
    def test_page_warnings(self):
        text = 'Absence entry\nWarning: The same date and time were already reported under leave 2000.\nGo Back\nSubmit'
        self.assertEqual(page_warnings(text), ['Warning: The same date and time were already reported under leave 2000.'])
        self.assertEqual(page_warnings('Absence entry\nBegin date\nSubmit Absence'), [])


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Provider(); self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', self.p)
        self.s = self.b.call('open', {'request_id': 'leaveco-open-1', 'url': 'practice'})
        with self.b.db() as db:
            db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (LEAVECO, FORM, self.s['id']))
            db.execute("INSERT INTO site_logins(name,start_url,origin,context_id,status,updated,session_id,sequence) VALUES('leaveco','https://services.leaveco.example/',?,'ctx-1','owner_connected',0,NULL,0)", (LEAVECO,))
            db.execute('INSERT INTO site_sessions VALUES(?,?)', (self.s['id'], 'leaveco'))
        self.p.snap.update({'url': FORM, 'title': 'MyLeaveco for Members', 'text': 'Absence entry',
                            'controls': [control('e1', 'text', 'Begin time'), control('e2', 'text', 'End Time'), control('e3', 'text', 'Duration of your absence in hours'),
                                         button('e9', 'Submit Absence')]})
    def prepare(self, rid='leaveco-task-1'):
        return self.b.call('task_prepare', {'request_id': rid, 'session_id': self.s['id'], 'title': 'Leaveco absence', 'goal': 'Report the Nov 5 absence, 8 AM to 5 PM, 9 hours.', 'mode': 'autonomous'})
    def step(self, t, rid, steps):
        return self.b.call('task_step', {'id': t, 'runtime_context': TOOL_CONTEXT, 'request_id': 'leaveco-' + rid, 'steps': steps})
    def test_standing_permission_starts_the_task_and_only_the_submit_is_reviewed(self):
        with self.assertRaisesRegex(ValueError, 'Only the owner'): self.b.call('autostart_logins', {'site': 'leaveco', 'on': True})
        with self.assertRaisesRegex(ValueError, 'No saved login'): self.b.call('autostart_logins', {'site': 'chase', 'on': True}, True)
        self.assertEqual(self.b.call('autostart_logins', {'site': 'leaveco', 'on': True}, True)['task_autostart'], ['leaveco'])
        t = self.prepare()
        r = self.b.call('task_review', {'id': t['id'], 'runtime_context': TOOL_CONTEXT})
        self.assertTrue(r['autostarted']); self.assertEqual(r['status'], 'active')
        self.assertIn('BROWSER TASK STARTED', self.p.messages[-1]); self.assertNotIn('/webtask ', self.p.messages[-1])
        out = self.step(t['id'], 'fill-1', [{'kind': 'fill', 'ref': 'e1', 'value': '08:00'}, {'kind': 'fill', 'ref': 'e2', 'value': '17:00'}, {'kind': 'fill', 'ref': 'e3', 'value': '9'}])
        self.assertEqual(out['status'], 'completed')
        stop = self.step(t['id'], 'submit-1', [{'kind': 'click', 'ref': 'e9'}])
        self.assertEqual((stop['status'], stop['stop_reason']), ('needs_separate_review', 'final'))
        # The owner's /webapprove runs with the Telegram command's context (which names the account).
        self.b.call('review', {'id': stop['action_id']}); plan = self.b.plan(stop['action_id'])
        self.assertEqual(self.b.call('execute', {'id': plan['id'], 'hash': plan['hash'][:16], 'context': CONTEXT}, True)['status'], 'completed')
        # Duplicate warning page: one more review, and it quotes the warning.
        self.p.snap.update({'text': 'Warning: the same date and time were already reported under leave 2000.\nGo Back\nSubmit',
                            'controls': [button('e1', 'Go Back', 'button'), button('e2', 'Submit')]})
        stop = self.step(t['id'], 'submit-2', [{'kind': 'click', 'ref': 'e2'}])
        self.assertEqual(stop['stop_reason'], 'final')
        self.b.call('review', {'id': stop['action_id']})
        self.assertIn('⚠ The page says: Warning: the same date and time were already reported', self.p.messages[-1])
        self.b.call('autostart_logins', {'site': 'leaveco', 'on': False}, True)
        self.assertEqual(self.b.call('autostart_logins', {})['task_autostart'], [])
    def test_without_standing_permission_review_still_needs_webtask(self):
        t = self.prepare('leaveco-task-2')
        r = self.b.call('task_review', {'id': t['id'], 'runtime_context': TOOL_CONTEXT})
        self.assertEqual(r['status'], 'reviewed'); self.assertNotIn('autostarted', r)
        self.assertIn('/webtask ' + t['id'], self.p.messages[-1]); self.assertIn('FORM FILLING', self.p.messages[-1])


if __name__ == '__main__':
    unittest.main()
