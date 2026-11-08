"""Fill/submit journal and verify-before-retry (web 0.17.0, PROCURATOR browsing plan Phase 1, §4.1).

Every browser act is journaled as nav | fill | select | click | submit. A submit is a click the broker classed
final or commit when it prepared the step. A submit is never pressed twice for the same target on the same page
unless a read-only check of the live page PROVES the earlier press failed (an error that was not on the page before
that press) AND the owner approved it again after the earlier press (a fresh /webapprove of a new review).
0.19.1 (Nov 6 QA, T3-04): "the button is still there and no confirmation is shown" is NOT proof of failure (single-page
forms keep their button after a successful submit); it is unknown, and unknown blocks. The owner can look and press it
themselves with /webtakeover.
"""
import hashlib
import json
import re
import time
import urllib.parse

from task_policy import classify_target, space

CONFIRMED = re.compile(r"\b(thank you(?! for (?:visiting|shopping|choosing|your interest))|order (?:number|#|no\.?)|confirmation (?:number|#|code)|reference (?:number|#|no\.?)|(?:order|booking|reservation|appointment|visit|vaccination|absence|request|application|claim|report|leave) (?:is |was |has been )?(?:confirmed|placed|received|booked|scheduled|submitted|filed)|(?:you'?re|you are|is|are) (?:all set|booked|confirmed|scheduled)|successfully (?:submitted|sent|filed|booked|scheduled|placed))\b", re.I)
# New lines after a press (not on the page before it) that read as success or as failure.
SUCCESS_WORDS = re.compile(r'\b(confirmed|submitted|success(?:ful(?:ly)?)?|received|completed?|scheduled|booked|placed|saved|sent|thank you)\b', re.I)
FAILED = re.compile(r"(\berror\b|\bfailed\b|could not be (?:submitted|processed|completed|saved|sent)|\bunable to\b|\btry again\b|please (?:correct|fix|re-?enter|check)|\bis required\b|\bare required\b|\binvalid\b|\bnot (?:been )?(?:submitted|saved|processed|sent)\b|went wrong)", re.I)

def line_hashes(text, limit=600):
    """Fingerprints of the page's lines (no text kept), so a later check can tell which lines are new since a press."""
    out = []
    for ln in (text or '').splitlines():
        ln = ' '.join(ln.split())
        if ln:
            h = hashlib.sha1(ln.lower().encode()).hexdigest()[:12]
            if h not in out:
                out.append(h)
        if len(out) >= limit:
            break
    return out

ALREADY = re.compile(r'\b(already (?:been )?(?:submitted|reported|received|filed|entered|scheduled|booked|requested|placed|exists?|on file)|duplicate (?:request|entry|submission|absence|claim|order)|previously (?:submitted|reported|filed))\b', re.I)

def page_key(url):
    p = urllib.parse.urlsplit(url or '')
    return p.scheme + '://' + (p.hostname or '') + p.path

def step_kind(step, plan):
    if step['kind'] == 'navigate':
        return 'nav'
    if step['kind'] in ('fill', 'select', 'upload'):
        return step['kind']
    if step['kind'] == 'click_xy':
        return 'click'           # position clicks are never final/commit (refused at prepare)
    cls = (step.get('target_class') or classify_target(step['control'], {'url': plan.get('url'), 'total': plan.get('detected_total')}))['class']
    return 'submit' if cls in ('final', 'commit') else 'click'

def idempotency_key(scope, step_no, step, plan):
    """sha256(task, step number in the plan, target, page). The target is its label/type/form, not the snapshot ref,
    so a reload that renumbers refs still lands on the same key."""
    c = step.get('control') or {}
    target = '|'.join([space(c.get('label', '')), c.get('type') or '', c.get('tag') or '', page_key(c.get('form_action') or '')]) if c else (
        'xy|' + space((step.get('probe') or {}).get('label', '')) if step.get('kind') == 'click_xy' else step.get('url', ''))
    return hashlib.sha256('\n'.join([scope, str(step_no), target, page_key(plan.get('url'))]).encode()).hexdigest()

class Journal:
    def __init__(self, broker):
        self.b = broker
        with broker.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS task_journal(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,session_id TEXT,
              plan_id TEXT,step_no INTEGER,kind TEXT,cls TEXT,label TEXT,url TEXT,idempotency_key TEXT,ts REAL,submitted_at REAL,
              verify_result TEXT,approved REAL)''')
            db.execute('CREATE INDEX IF NOT EXISTS task_journal_key ON task_journal(idempotency_key)')
            cols = [r['name'] for r in db.execute('PRAGMA table_info(task_journal)')]
            for col in ('screenshot_hash', 'page_sig', 'value_key', 'tag', 'ctype'):
                if col not in cols:
                    db.execute('ALTER TABLE task_journal ADD COLUMN %s TEXT' % col)

    def prior_submit(self, key):
        with self.b.db() as db:
            r = db.execute("SELECT * FROM task_journal WHERE idempotency_key=? AND kind='submit' AND submitted_at IS NOT NULL ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return dict(r) if r else None

    def verify_submitted(self, s, prior, completion_text=''):
        """Read the live page (no reload: reloading a posted page can send it again) and decide
        submitted | not_submitted | unknown. not_submitted means PROVEN failed (a new error since the press); only that
        lets a freshly approved re-press through. Everything unclear is unknown and blocks."""
        try:
            snap = self.b.snapshot(s)
        except Exception:
            return 'unknown', 'the page could not be read'
        text = snap.get('text') or ''
        if completion_text and completion_text in text:
            return 'submitted', 'the task\'s confirmation text is on the page'
        m = ALREADY.search(text) or CONFIRMED.search(text)
        if m:
            line = next((ln for ln in text.splitlines() if m.group(0) in ln), m.group(0))
            return 'submitted', 'the page says: "' + ' '.join(line.split())[:160] + '"'
        before_plan = {}
        try:
            before_plan = json.loads(self.b.plan(prior['plan_id'])['plan'])
        except Exception:
            pass
        before = set(before_plan.get('text_lines') or [])
        lines = [' '.join(ln.split()) for ln in text.splitlines() if ln.strip()]
        new = [ln for ln in lines if hashlib.sha1(ln.lower().encode()).hexdigest()[:12] not in before] if before else []
        ok_line = next((ln for ln in (new if before else lines) if SUCCESS_WORDS.search(ln)), None)
        if ok_line:
            return 'submitted', 'the page now says: "' + ok_line[:160] + '"'
        if page_key(snap.get('url')) != page_key(prior['url']):
            return 'unknown', 'the page moved on since the earlier press (' + page_key(snap.get('url'))[:120] + ')'
        # 0.21.0 (health foundation; Nov 6 review finding): only an error BOUND TO THE SUBMITTED FORM proves the press failed —
        # an alert inside that form or one of its fields marked invalid, new since the press. A page-wide error (a banner, a
        # sidebar widget, a cookie notice) says nothing about the submission and never unlocks a second press.
        pressed = next((st for st in before_plan.get('steps') or [] if st.get('kind') == 'click'), {}) or {}
        form = str((pressed.get('control') or {}).get('form_action') or '')
        if 'form_errors' not in snap:
            return 'unknown', 'this browser driver does not report form-bound errors, so the earlier press cannot be proven failed'
        old_form = {(e.get('form'), e.get('text')) for e in before_plan.get('form_errors') or [] if isinstance(e, dict)}
        fresh = [e for e in snap.get('form_errors') or [] if isinstance(e, dict) and (e.get('form'), e.get('text')) not in old_form]
        if form:
            fresh = [e for e in fresh if str(e.get('form') or '') == form or form.endswith(str(e.get('form') or '#')) or str(e.get('form') or '#').endswith(form)]
        if fresh:
            return 'not_submitted', 'the submitted form shows an error since the earlier press: "' + ' '.join(str(fresh[0].get('text')).split())[:160] + '"'
        if any(e for e in snap.get('page_errors') or [] if isinstance(e, str) and e not in set(before_plan.get('page_errors') or [])):
            return 'unknown', 'a new message appeared on the page but it is not attached to the submitted form, so the press may still have gone through'
        return 'unknown', 'no confirmation and no error on the page since the earlier press, so it may have gone through'

    def check(self, p, approved):
        """Before an act runs. Returns None to proceed, or a refusal {reason, verify, detail}."""
        plan = json.loads(p['plan'])
        scope = p.get('task_id') or p['session_id']
        for i, step in enumerate(plan['steps']):
            if step_kind(step, plan) != 'submit':
                continue
            key = idempotency_key(scope, i, step, plan)
            prior = self.prior_submit(key)
            if not prior:
                continue
            completion = ''
            if p.get('task_id'):
                try:
                    completion = json.loads(self.b.tasks.row(p['task_id'])['spec']).get('completion_text') or ''
                except Exception:
                    pass
            verdict, detail = self.verify_submitted(self.b.session(p['session_id']), prior, completion)
            with self.b.db() as db:
                db.execute('UPDATE task_journal SET verify_result=? WHERE id=?', (verdict + ': ' + detail, prior['id']))
            if verdict != 'not_submitted':
                return {'reason': 'already_submitted' if verdict == 'submitted' else 'unclear', 'verify': verdict, 'detail': detail, 'label': prior['label'], 'at': prior['submitted_at']}
            fresh = approved and p.get('reviewed') and p['reviewed'] > prior['submitted_at']
            if not fresh:
                return {'reason': 'fresh_approval_needed', 'verify': verdict, 'detail': detail, 'label': prior['label'], 'at': prior['submitted_at']}
        return None

    def before_offer(self, scope, step, url, s, completion=''):
        """At prepare: a final/commit button already pressed on this page is not offered for approval again unless the
        page proves the earlier press failed. Returns None or the refusal text."""
        key = idempotency_key(scope, 0, step, {'url': url})
        prior = self.prior_submit(key)
        if not prior:
            return None
        verdict, detail = self.verify_submitted(s, prior, completion)
        if verdict == 'not_submitted':
            return None
        when = time.strftime('%H:%M', time.localtime(prior['submitted_at']))
        return ('"%s" was already pressed on this page at %s and %s. It is not offered for approval again. Read the page, '
                'report the result (web_task_complete if this was the final step) and tell the owner; if the owner believes it did not go '
                'through the owner can check and press it themselves with /webtakeover %s.') % (prior['label'][:80], when,
                detail if verdict == 'unknown' else 'it went through (' + detail + ')', s['id'])

    def record(self, p, approved):
        plan = json.loads(p['plan'])
        scope = p.get('task_id') or p['session_id']
        now = time.time()
        rows = []
        with self.b.db() as db:
            for i, step in enumerate(plan['steps']):
                kind = step_kind(step, plan)
                c = step.get('control') or {}
                cls = (step.get('target_class') or {}).get('class') if c else None
                label = space(c.get('label', '')) if c else (space(step.get('description', '')) if step.get('kind') == 'click_xy' else step.get('url', ''))
                if step.get('kind') == 'upload':
                    label = label + ' ← ' + step['file']['name']
                if step.get('kind') == 'click_xy':
                    cls = (step.get('target_class') or {}).get('class')
                vk = re.fullmatch(r'\{\{\s*(owner\.[a-z0-9_.]+)\s*\}\}', str(step.get('value') or '').strip()) if kind == 'fill' else None
                cur = db.execute('INSERT INTO task_journal(task_id,session_id,plan_id,step_no,kind,cls,label,url,idempotency_key,ts,submitted_at,verify_result,approved,screenshot_hash,page_sig,value_key,tag,ctype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (p.get('task_id'), p['session_id'], p['id'], i, kind, cls, label, plan.get('url', ''),
                     idempotency_key(scope, i, step, plan), now, None, None, p.get('reviewed') if approved else None, step.get('screenshot_sha'),
                     json.dumps(plan.get('page_sig') or []), vk.group(1) if vk else None, c.get('tag') if c else None, c.get('type') if c else None))
                rows.append((cur.lastrowid, kind))
        return rows

    def settle(self, rows, status):
        """A press counts as made unless the driver proved nothing was dispatched."""
        if status in ('not_dispatched', 'stale', 'refused_duplicate'):
            return
        with self.b.db() as db:
            for rid, kind in rows:
                if kind == 'submit':
                    db.execute('UPDATE task_journal SET submitted_at=? WHERE id=?', (time.time(), rid))

    def submits(self, task_id):
        with self.b.db() as db:
            rows = db.execute("SELECT label,url,submitted_at,verify_result FROM task_journal WHERE task_id=? AND kind='submit' AND submitted_at IS NOT NULL ORDER BY id", (task_id,)).fetchall()
        return [{'button': r['label'], 'page': page_key(r['url']), 'pressed_at': r['submitted_at'], 'last_check': r['verify_result']} for r in rows]
