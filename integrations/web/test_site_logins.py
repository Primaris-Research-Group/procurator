import json
import time
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch
from broker import Broker
from test_project_sources import Cloud
from test_tasks import CONTEXT
from task_policy import autonomous_decision, normalize_task

def origin_of(v):
    p = urllib.parse.urlsplit(v)
    return v, 'https://' + p.netloc

class SiteCloud(Cloud):
    def __init__(self):
        super().__init__(); self.contexts = 0; self.deleted = []
    def api(self, method, path, body=None):
        if path == 'contexts': self.contexts += 1; return {'id': '00000000-0000-4000-8000-0000000000%02d' % self.contexts}
        if method == 'DELETE': self.deleted.append(path); return {}
        return super().api(method, path, body)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = SiteCloud(); self.b = Broker(Path(self.tmp.name) / 'b.db', self.p)
        patcher = patch('broker.url', side_effect=origin_of); patcher.start(); self.addCleanup(patcher.stop)
        self.b.url_validator = origin_of
    def connect(self):
        r = self.b.call('site_connect', {'site': 'leaveco', 'url': 'https://www.leaveco.example/'}, True)
        self.assertIn('live_view_url', r); return r
    def test_only_owner_connects_and_bot_cannot_read_while_signing_in(self):
        for action in ('site_connect', 'site_ready', 'site_disconnect'):
            with self.assertRaisesRegex(ValueError, 'Only the authenticated owner'): self.b.call(action, {'site': 'leaveco', 'url': 'https://www.leaveco.example/'})
        r = self.connect(); self.assertEqual(r['status'], 'awaiting_owner_sign_in'); self.assertEqual(self.p.contexts, 1)
        with self.assertRaisesRegex(ValueError, 'signing in to .* privately'): self.b.call('snapshot', {'id': r['session_id']})
        with self.assertRaisesRegex(ValueError, 'not ready'): self.b.call('open', {'request_id': 'leaveco-open-1', 'url': 'https://www.leaveco.example/', 'site_login': 'leaveco'})
        self.assertEqual(self.b.call('site_ready', {'site': 'leaveco'}, True)['status'], 'owner_connected')
        self.connect(); self.assertEqual(self.p.contexts, 1)  # reconnect reuses the same saved login
        with self.assertRaisesRegex(ValueError, 'already used'): self.b.call('site_connect', {'site': 'leaveco', 'url': 'https://evil.example/'}, True)
        with self.assertRaises(ValueError): self.b.call('site_connect', {'site': 'LEAVECO; rm', 'url': 'https://www.leaveco.example/'}, True)
    def test_signed_in_browser_opens_only_its_site_and_detects_expiry(self):
        r = self.connect(); self.b.call('site_ready', {'site': 'leaveco'}, True)
        self.assertEqual(self.p.context, '00000000-0000-4000-8000-000000000001')
        with self.assertRaisesRegex(ValueError, 'only opens its own website'):
            self.b.call('open', {'request_id': 'leaveco-open-x', 'url': 'https://other.example/', 'site_login': 'leaveco'})
        s = self.b.call('open', {'request_id': 'leaveco-open-2', 'url': 'https://www.leaveco.example/claims', 'site_login': 'leaveco'})
        self.assertEqual(s['site_login'], 'leaveco'); self.assertFalse(s['fresh_login_session'])
        self.p.snap.update({'url': 'https://www.leaveco.example/claims', 'title': 'Claims', 'text': 'Your leave', 'controls': []})
        self.assertNotIn('site_login_status', self.b.call('snapshot', {'id': s['id']})['snapshot'])
        self.p.snap.update({'url': 'https://www.leaveco.example/login', 'controls': [{'ref': 'e1', 'type': 'password', 'label': 'Password', 'tag': 'input'}]})
        snap = self.b.call('snapshot', {'id': s['id']})['snapshot']
        self.assertEqual(snap['site_login_status'], 'signed_out'); self.assertIn('web_send_signin_link with site=leaveco', snap['site_login_guidance'])
        self.assertEqual(self.b.sites.row('leaveco')['status'], 'reauthentication_required')
        self.assertEqual(self.b.call('site_list', {})['sites'][0]['status'], 'reauthentication_required')
    def test_disconnect_deletes_saved_login(self):
        self.connect(); self.b.call('site_ready', {'site': 'leaveco'}, True)
        self.assertEqual(self.b.call('site_disconnect', {'site': 'leaveco'}, True)['status'], 'removed')
        self.assertEqual(self.p.deleted, ['contexts/00000000-0000-4000-8000-000000000001']); self.assertEqual(self.b.call('site_list', {})['sites'], [])

class PolicyTests(unittest.TestCase):
    session = {'id': 's1', 'origin': 'https://www.leaveco.example', 'practice': 0, 'timeout_seconds': 7200, 'site_login': 'leaveco'}
    def norm(self, **a):
        base = {'request_id': 'r' * 8, 'session_id': 's1', 'title': 'leave', 'goal': 'Report an intermittent leave absence for Nov 3.', 'mode': 'autonomous'}
        return normalize_task(base | a, self.session, origin_of, lambda v, n=500: v if isinstance(v, str) else '')
    def test_signed_in_tasks_protect_account_settings_and_keep_final_review(self):
        spec = self.norm(facts=['11/03/2026', '4'])
        self.assertEqual(spec['site_login'], 'leaveco')
        with self.assertRaisesRegex(ValueError, 'every final submission gets an owner review'):
            self.norm(limits={'dates': ['2026-11-03']}, free_booking_auto_confirm=True)
        def click(label, url='https://www.leaveco.example/leave'):
            return {'url': url, 'steps': [{'kind': 'click', 'control': {'ref': 'e1', 'type': 'button', 'tag': 'button', 'label': label, 'role': ''}}], 'page_errors': [], 'detected_total': None}
        self.assertEqual(autonomous_decision(spec, click('Report an absence')), (True, 'routine'))
        for label in ['Update direct deposit', 'Change email address', 'Edit beneficiaries', 'Cancel coverage', 'Change password']:
            self.assertEqual(autonomous_decision(spec, click(label))[1], 'account_protected', label)
        self.assertEqual(autonomous_decision(spec, click('Continue', 'https://www.leaveco.example/profile/bank-account'))[1], 'account_protected')
        self.assertEqual(autonomous_decision(spec, click('Submit absence'))[1], 'final')
        nav = {'url': 'https://www.leaveco.example/leave', 'steps': [{'kind': 'navigate', 'url': 'https://www.leaveco.example/settings/direct-deposit'}], 'page_errors': []}
        self.assertEqual(autonomous_decision(spec, nav)[1], 'account_protected')
        unsigned = dict(spec); unsigned.pop('site_login')
        self.assertEqual(autonomous_decision(unsigned, click('Change email address')), (True, 'routine'))



class Nov4Tests(Tests):
    """Nov 4: owner tapped the example leaveco.example link instead of the private one; portals span subdomains."""
    def test_domain_helpers(self):
        from site_logins import site_domain, in_scope
        self.assertEqual(site_domain('https://services.leaveco.example'), 'leaveco.example'); self.assertEqual(site_domain('https://www.bbc.co.uk'), 'bbc.co.uk')
        s = {'origin': 'https://www.leaveco.example', 'site_domain': 'leaveco.example'}
        self.assertTrue(in_scope(s, 'https://leave.leaveco.example')); self.assertTrue(in_scope(s, 'https://leaveco.example'))
        for bad in ['https://evilleaveco.example', 'http://services.leaveco.example', 'https://leaveco.example.evil.io', None]:
            self.assertFalse(in_scope(s, bad), bad)
        self.assertFalse(in_scope({'origin': 'https://www.cvs.com'}, 'https://m.cvs.com'))  # public browsing stays exact-origin
    def test_name_only_uses_known_start_page_or_asks(self):
        r = self.b.call('site_connect', {'site': 'leaveco'}, True); self.assertEqual(r['start_url'], 'https://www.leaveco.example/')
        with self.assertRaisesRegex(ValueError, 'Which website'): self.b.call('site_connect', {'site': 'aetna'}, True)
    def test_friendly_message_when_connect_was_never_started(self):
        with self.assertRaisesRegex(ValueError, 'web_send_signin_link with site leaveco'): self.b.call('site_ready', {'site': 'leaveco'}, True)
    def test_siteready_checks_sign_in_once_then_trusts_the_owner(self):
        self.connect()
        self.p.snap.update({'url': 'https://www.leaveco.example/login', 'controls': [{'ref': 'e1', 'type': 'password', 'label': 'Password', 'tag': 'input'}]})
        r = self.b.call('site_ready', {'site': 'leaveco'}, True)
        self.assertEqual(r['status'], 'still_signed_out'); self.assertEqual(self.b.sites.row('leaveco')['status'], 'awaiting_owner_sign_in')
        self.assertEqual(self.b.call('site_ready', {'site': 'leaveco'}, True)['status'], 'owner_connected')
    def test_signed_in_browser_follows_the_portal_across_its_subdomains(self):
        self.connect(); self.b.call('site_ready', {'site': 'leaveco'}, True)
        s = self.b.call('open', {'request_id': 'leaveco-open-3', 'url': 'https://services.leaveco.example/leave', 'site_login': 'leaveco'})
        self.assertEqual(self.b.session(s['id'])['site_domain'], 'leaveco.example')
        self.p.snap.update({'url': 'https://services.leaveco.example/leave', 'title': 'Leave', 'text': 'Report time', 'controls': []})
        self.b.call('snapshot', {'id': s['id']})
        p = self.b.call('prepare', {'id': s['id'], 'request_id': 'leaveco-nav-1', 'steps': [{'kind': 'navigate', 'url': 'https://leave.leaveco.example/report'}]})
        self.assertTrue(p['id'])
        with self.assertRaisesRegex(ValueError, 'different website'):
            self.b.call('prepare', {'id': s['id'], 'request_id': 'leaveco-nav-2', 'steps': [{'kind': 'navigate', 'url': 'https://evil.example/x'}]})
        with self.assertRaisesRegex(ValueError, 'only opens its own website'):
            self.b.call('open', {'request_id': 'leaveco-open-4', 'url': 'https://leaveco.example.evil.io/', 'site_login': 'leaveco'})


class LinkRequestTests(Tests):
    """Nov 4 evening: owner-typed /siteconnect and /webtakeover did not reach the commands; a
    /siteready after the sign-in window closed was saved as ready without a sign-in."""
    def test_bot_can_have_a_signin_link_sent_to_the_owner_but_never_sees_it(self):
        r = self.b.call('site_signin_request', {'site': 'leaveco'})
        self.assertTrue(r['sent']); self.assertNotIn('signin', json.dumps(r).replace('sign-in', ''))
        self.assertNotIn('assistant.example.com', json.dumps(r))
        self.assertIn('/assistant/signin/', self.p.messages[-1]); self.assertIn('iPhone', self.p.messages[-1])
        again = self.b.call('site_signin_request', {'site': 'leaveco'})
        self.assertFalse(again['sent']); self.assertEqual(sum('/assistant/signin/' in m for m in self.p.messages), 1)
        with self.assertRaises(ValueError): self.b.call('site_signin_request', {'site': 'LEAVECO; rm'})
    def test_closed_signin_window_is_never_saved_as_ready(self):
        r = self.connect()
        with self.b.db() as db: db.execute("UPDATE sessions SET status='closed' WHERE id=?", (r['session_id'],))
        with self.assertRaisesRegex(ValueError, 'already closed'): self.b.call('site_ready', {'site': 'leaveco'}, True)
        self.assertEqual(self.b.sites.row('leaveco')['status'], 'reauthentication_required')
        with self.assertRaisesRegex(ValueError, 'no sign-in in progress'): self.b.call('site_ready', {'site': 'leaveco'}, True)
        self.assertEqual(self.b.sites.row('leaveco')['status'], 'reauthentication_required')
    def test_takeover_link_goes_to_the_owner_not_the_bot(self):
        s = self.b.call('open', {'request_id': 'plain-open-1', 'url': 'https://www.cvs.com/'})
        r = self.b.call('takeover_request', {'id': s['id']})
        self.assertTrue(r['sent']); self.assertNotIn('live_view', json.dumps(r)); self.assertNotIn('browserbase', json.dumps(r))
        self.assertIn('Take over the browser ' + s['id'], self.p.messages[-1])

    def test_signin_closes_an_idle_earlier_browser(self):
        old = self.b.call('open', {'request_id': 'old-browser-1', 'url': 'https://www.cvs.com/'})
        r = self.b.call('site_signin_request', {'site': 'leaveco'})
        self.assertTrue(r['sent'])
        self.assertEqual(self.b.session(old['id'])['status'], 'closed')

    def test_a_link_made_elsewhere_never_blocks_the_bot_from_sending_a_working_one(self):
        """Nov 4, 7 pm: a terminal-made link (not in Telegram) made the bot say 'use the link I sent earlier'."""
        first = self.connect(); old_token = first['signin_url'].rsplit('/', 1)[1]
        r = self.b.call('site_signin_request', {'site': 'leaveco'})
        self.assertTrue(r['sent']); msg = self.p.messages[-1]; self.assertIn('/assistant/signin/', msg)
        new_token = msg.split('/assistant/signin/')[1].split()[0]
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(self.b.sites.row('leaveco')['session_id'], first['session_id'])      # same open browser, nothing lost
        self.assertEqual(self.b.session(first['session_id'])['status'], 'open')
        with self.assertRaisesRegex(ValueError, 'expired or was already used'): self.b.sites.signin(old_token)
        self.assertEqual(self.b.sites.signin(new_token)[0]['name'], 'leaveco')
        self.assertFalse(self.b.call('site_signin_request', {'site': 'leaveco'})['sent'])     # no spam within 90 s
        self.b.sites._sent_at['leaveco'] -= 120
        self.assertTrue(self.b.call('site_signin_request', {'site': 'leaveco'})['sent'])
        self.assertEqual(sum('/assistant/signin/' in m for m in self.p.messages), 2)
    def test_a_nearly_expired_signin_starts_fresh(self):
        first = self.connect()
        with self.b.db() as db: db.execute('UPDATE site_logins SET signin_expires=? WHERE name=?', (time.time() + 300, 'leaveco'))
        self.assertTrue(self.b.call('site_signin_request', {'site': 'leaveco'})['sent'])
        self.assertNotEqual(self.b.sites.row('leaveco')['session_id'], first['session_id'])
        self.assertIn('45 minutes', self.p.messages[-1])
    def test_bot_facing_errors_never_tell_him_to_type_commands(self):
        r = self.connect()
        with self.assertRaises(ValueError) as e: self.b.call('snapshot', {'id': r['session_id']})
        self.assertIn('web_send_signin_link', str(e.exception)); self.assertNotIn('/siteready', str(e.exception))

    def test_a_booking_site_may_move_between_its_own_hosts(self):
        """Nov 4 Mabella's: Toast Tables moved from tables.toasttab.com to another toasttab.com host after the security check."""
        from site_logins import in_scope, browse_domain
        s = self.b.call('open', {'request_id': 'toast-open-1', 'url': 'https://tables.toasttab.com/restaurants/abc/findTime'})
        live = self.b.session(s['id'])
        self.assertEqual(live['site_domain'], 'toasttab.com')
        self.assertTrue(in_scope(live, 'https://www.toasttab.com'))
        self.assertFalse(in_scope(live, 'https://toasttab.com.evil.io'))
        self.assertFalse(in_scope(live, 'http://www.toasttab.com'))
        self.assertIsNone(browse_domain('https://shop-a.myshopify.com'))       # other owners' shops stay out
        self.assertEqual(browse_domain('https://www.cvs.com'), 'cvs.com')

    def test_a_fresh_browser_follows_the_site_to_its_new_address_but_never_after_acting(self):
        """Nov 4 Mabella's: tables.toasttab.com sent the browser on to toast.app; the bot stopped."""
        original = self.p.run
        def moved_once(s, op, **kw):
            if op == 'snapshot' and s['origin'] == 'https://tables.toasttab.com':
                return {'ok': False, 'kind': 'origin_changed', 'host': 'www.toast.app', 'url': 'https://www.toast.app/mabellas-uptown/reserve'}
            return original(s, op, **kw)
        self.p.run = moved_once
        s = self.b.call('open', {'request_id': 'toast-open-2', 'url': 'https://tables.toasttab.com/restaurants/abc/findTime'})
        self.b.call('snapshot', {'id': s['id']})
        live = self.b.session(s['id'])
        self.assertEqual(live['origin'], 'https://www.toast.app'); self.assertEqual(live['site_domain'], 'toast.app')
        # A browser where the bot already acted is never re-anchored; the bot is told where the page went.
        self.b.call('close', {'id': s['id']})
        t = self.b.call('open', {'request_id': 'toast-open-3', 'url': 'https://tables.toasttab.com/restaurants/abc/findTime'})
        with self.b.db() as db:
            db.execute("INSERT INTO plans(id,request_id,session_id,hash,plan,status) VALUES('p1','r1',?,'h','{}','completed')", (t['id'],))
        with self.assertRaisesRegex(ValueError, 'toast.app/mabellas-uptown/reserve'):
            self.b.call('snapshot', {'id': t['id']})
        self.assertEqual(self.b.session(t['id'])['origin'], 'https://tables.toasttab.com')


if __name__ == '__main__':
    unittest.main()


class SignupTests(Tests):
    """Nov 6 Publix: the owner has no account and asked the assistant to create one. The assistant fills the ordinary fields in a
    browser it can see; password, codes and the create button are their, in that same browser."""

    def test_signup_closes_a_stuck_private_signin_and_opens_a_page_the_bot_can_work_in(self):
        stuck = self.b.call('site_connect', {'site': 'publix'}, True)  # yesterday's flow: private sign-in browser, bot blocked
        self.assertEqual(stuck['start_url'], 'https://www.publix.com/')
        with self.assertRaisesRegex(ValueError, 'web_site_signup site=publix'):
            self.b.call('snapshot', {'id': stuck['session_id']})
        with self.assertRaisesRegex(ValueError, "private publix sign-in.*web_site_signup"):
            self.b.call('open', {'request_id': 'other-site-1', 'url': 'https://www.example.com/'})
        r = self.b.call('site_signup', {'site': 'publix'})  # not owner-only: the owner asked in chat
        self.assertEqual(r['status'], 'signing_up'); self.assertIn('web_send_signin_link site=publix', r['note'])
        self.assertEqual(self.b.session(stuck['session_id'])['status'], 'closed')
        self.assertEqual(self.p.contexts, 1, 'the saved-login storage is reused')
        s = self.b.session(r['browser'])
        self.assertTrue(s['signup']); self.assertEqual(s['site_login'], 'publix'); self.assertEqual(s['timeout_seconds'], 7200)
        self.p.snap.update({'url': 'https://www.publix.com/account/create', 'title': 'Create account', 'text': 'Create your account', 'controls': []})
        self.assertIn('snapshot', self.b.call('snapshot', {'id': s['id']}), 'the assistant can see the sign-up page')
        again = self.b.call('site_signup', {'site': 'publix'})
        self.assertTrue(again['reused']); self.assertEqual(again['browser'], s['id'])
        reopened = self.b.call('open', {'request_id': 'publix-open-1', 'url': 'https://www.publix.com/', 'site_login': 'publix'})
        self.assertEqual(reopened['id'], s['id'])

    def test_he_finishes_password_and_create_in_the_same_browser_then_the_login_is_saved(self):
        r = self.b.call('site_signup', {'site': 'publix'})
        sent = self.b.call('site_signin_request', {'site': 'publix'})
        self.assertEqual(sent['browser'], r['browser'])
        msg = self.p.messages[-1]
        self.assertIn('Finish creating your publix account', msg); self.assertIn('Create account / Sign up button yourself', msg)
        with self.assertRaisesRegex(ValueError, 'signing in to publix privately'):
            self.b.call('snapshot', {'id': r['browser']})
        self.p.snap.update({'url': 'https://www.publix.com/account', 'title': 'My account', 'text': 'Welcome, the owner', 'controls': []})
        self.assertEqual(self.b.call('site_ready', {'site': 'publix'}, True)['status'], 'owner_connected')
        self.assertNotIn('signup', self.b.session(r['browser']))
        with self.assertRaisesRegex(ValueError, 'already a saved publix login'):
            self.b.call('site_signup', {'site': 'publix'})

    def test_signup_refuses_never_sites_and_needs_an_address_for_unknown_sites(self):
        with self.assertRaisesRegex(ValueError, 'official sign-up page'):
            self.b.call('site_signup', {'site': 'aetna'})
        with patch.object(self.b, 'site_rule', return_value={'mode': 'never', 'domain': 'wellsfargo.com', 'reason': 'Banks are never automated.'}):
            with self.assertRaisesRegex(ValueError, 'No account creation on wellsfargo.com'):
                self.b.call('site_signup', {'site': 'wf', 'url': 'https://www.wellsfargo.com/'})

    def test_signup_field_rules(self):
        r = self.b.call('site_signup', {'site': 'publix'})
        spec = normalize_task({'title': 'Create Publix pharmacy account', 'goal': 'Fill the sign-up form', 'mode': 'autonomous', 'fill_form': True,
                               'facts': ['The owner', 'owner@example.com']}, self.b.session(r['browser']), origin_of, lambda v, *a: v)
        self.assertTrue(spec['signup'])
        page = 'https://www.publix.com/account/create'
        def step(kind, label, value=None, ctype='text', tag='input', url=page, before=()):
            st = {'kind': kind, 'control': {'ref': 'e9', 'type': ctype, 'tag': tag, 'label': label, 'role': '', 'value': ''}}
            if value is not None: st['value'] = value
            prior = [{'kind': 'fill', 'control': {'ref': 'e%d' % i, 'type': 'text', 'tag': 'input', 'label': 'First name', 'role': '', 'value': ''}, 'value': 'The owner'} for i in range(len(before))]
            return {'url': url, 'steps': prior + [st], 'page_errors': [], 'detected_total': None, 'cookie_notice': False}
        # ordinary sign-up fields are filled by the assistant (these stop on an existing account)
        for label, value in [('Email address', 'owner@example.com'), ('Phone number', '813-555-0142'), ('Home address', '100 Main St'),
                             ('Date of birth', '01/15/1985'), ('ZIP code', '33612'), ('First name', 'The owner')]:
            self.assertEqual(autonomous_decision(spec, step('fill', label, value)), (True, 'routine'), label)
        # the private part is their
        for label, ctype in [('Password', 'password'), ('Create a password', 'text'), ('Security question answer', 'text'), ('Verification code', 'text'), ('Social Security number', 'text')]:
            self.assertEqual(autonomous_decision(spec, step('fill', label, 'x1', ctype))[1], 'signup_credentials', label)
        # moving to the form is fine; the create button is their
        self.assertEqual(autonomous_decision(spec, step('click', 'Create account', tag='a', url='https://www.publix.com/'))[0], True)
        self.assertEqual(autonomous_decision(spec, step('click', 'Sign up', ctype='button', tag='button', url='https://www.publix.com/'))[0], True)
        self.assertEqual(autonomous_decision(spec, step('click', 'Create account', ctype='submit', tag='button'))[1], 'signup_credentials')
        self.assertEqual(autonomous_decision(spec, step('click', 'Create account', ctype='button', tag='button', url='https://www.publix.com/', before=[1]))[1], 'signup_credentials')
        self.assertEqual(autonomous_decision(spec, {'url': 'https://www.publix.com/', 'steps': [{'kind': 'navigate', 'url': 'https://www.publix.com/register'}], 'page_errors': []})[0], True)
        self.assertEqual(autonomous_decision(spec, step('click', 'I agree to the terms', ctype='checkbox', tag='input'))[1], 'signup_credentials')  # consent is their
        # outside sign-up mode the account protections are unchanged
        existing = dict(spec); existing.pop('signup')
        self.assertEqual(autonomous_decision(existing, step('fill', 'Email address', 'owner@example.com'))[1], 'account_protected')
