"""Task-packet orchestrator (Nov 6 2026, phase 3): step routing, the per-packet work-session brief, the
"waiting for" markers with the exact Telegram line, a read-only dry run, and the done-when check.

The orchestrator never performs or approves an external action. It tells the assistant which surface a
step runs on and which native owner approval that surface already requires; the brokers enforce those
approvals. Each work session sees only its own packet: the brief built here is assembled from that
packet's stored metadata and steps, never from another packet.
"""
import datetime as dt
import json
import re

ROUTES = {
    'research': {'surface': 'web search, evidence_search, clinical_calc, drive_read_document / gmail_attachment_read (whole files, paged), mission_read',
                 'gate': 'none (read-only)', 'evidence': 'sources with URL/ID and date (mission_fact / mission_log refs)'},
    'browse': {'surface': 'one autonomous browser task: web_task_prepare (mode autonomous; site_login for a saved login; fill_form) → web_task_review',
               'gate': 'their /webtask approval (none when /siteauto is on for that login) and the final-submit review',
               'evidence': 'confirmation number shown by the site + browser task ID'},
    'mail': {'surface': 'mail_create_draft → mail_request_review', 'gate': 'their /mailsend', 'evidence': 'message ID and thread after the send'},
    'calendar': {'surface': 'calendar_create_event / calendar_update_event / tasks_add', 'gate': '/calapprove for guests or deletions; none for their own events',
                 'evidence': 'event or task ID'},
    'call': {'surface': 'phone_call_status (no other live call) → phone_prepare_call → phone_review_call; afterwards phone_read_call',
             'gate': 'their /callapprove for every call', 'evidence': 'call ID + the other party\'s words from the transcript (never your own recap)'},
    'write': {'surface': 'Google output workspace_create_document (memo, letter, brief, ledger)', 'gate': 'none to create; sending it is a separate mail step',
              'evidence': 'Drive document link'},
    'build': {'surface': 'build_status → build_prepare with the packet\'s Repo/Builder/Agent and mission + step', 'gate': 'their /buildapprove',
              'evidence': 'draft PR link + test result (written back automatically when the build finishes)'},
    'ask': {'surface': 'mission_question (ask) and ONE short Telegram question', 'gate': 'none (the owner answers)', 'evidence': 'their answer, saved as a fact'},
    'wait': {'surface': 'mission_watch for the sender/subject, or a date', 'gate': 'none', 'evidence': 'the email or date that ended the wait'},
    'work': {'surface': 'whatever the step text needs among the surfaces above (read the packet section it refers to)',
             'gate': 'the gate of the surface you use', 'evidence': 'the receipt of that surface'},
    # 0.21.0 (Nov 7): consumer health module. Every consequential health step is ONE effect under a grant the owner approved.
    'appointment': {'surface': 'health_status (grant + connection) → search read-only (browser research task / call brief / portal read) → health_effect_begin → '
                               'health_effect_dispatch → the booking itself (autonomous browser task, call or API) → health_effect_settle; calendar step afterwards',
                    'gate': 'an active health grant naming this clinician/specialty (health_grant_propose if none; the owner approves with /healthgrant); outside its limits: stop and ask',
                    'evidence': 'effect ID + the provider\'s confirmation ID (a "request received" is settled as requested, not confirmed) + receipt ID'},
    'refill': {'surface': 'health_status → rx.status read → health_effect_begin (exact prescription_id) → health_effect_dispatch → the pharmacy operation → health_effect_settle',
               'gate': 'an active health grant naming that exact prescription; renewals (prescriber) are a separate action the owner must grant',
               'evidence': 'effect ID + pharmacy request/confirmation ID; "ready" only with pharmacy evidence'},
    'portal_read': {'surface': 'the connected portal\'s read tools for the record categories the grant allows (portal.read)', 'gate': 'an active grant with portal.read; nothing is written',
                    'evidence': 'source resource IDs with retrieval time (mission_fact), never the record text in Telegram'},
    'health_log': {'surface': 'the nutrition/measurement log tools (health.log)', 'gate': 'none beyond the grant (their own data)', 'evidence': 'log entry IDs'},
}

KIND_RULES = {
    'administrative': 'Administrative packet: complete the errand end to end; use saved logins named in Sites; one approval per outward action.',
    'research': 'Research packet: read-only unless a step says otherwise; cite every source; deliver what Deliverables names.',
    'legal': ('Legal packet: research and drafts only. Every outgoing message is a draft for their review; never file with a court or agency, never sign, '
              'settle, waive, withdraw or consent to arbitration; ask before contacting any party for the first time; never invent a deadline '
              '(save it as unverified and tell them).'),
    'coding': 'Coding packet: work through build steps on their builders; every build is a draft PR the owner reviews; nothing is merged or deployed.',
    'health': ('Health packet: every booking, refill or record read runs under a health grant the owner approved (health_status shows them; health_grant_propose asks '
               'for one). Preserve the provider\'s wording; never assign clinical urgency yourself — route urgent-care instructions to them as written; clinical '
               'questions go to them or the care team, never answered by you. Unknown insurance participation, referral status or cost stays unknown until a '
               'source says otherwise. Health details go in the private receipt and the packet journal only: Telegram gets one neutral line, and nothing health-'
               'related is saved to memory. A provider\'s "request received" is not a confirmation. After an ambiguous result, read the provider\'s state '
               'before any new attempt (health_effect_reconcile).'),
    'engineering': 'Engineering packet: research and calculation notes in Drive, or builds as in coding packets; state assumptions and units.',
}

AWAITING = re.compile(r'^(approval:(call|mail|web|calendar|purchase):[A-Za-z0-9._-]{4,40}|build:[a-f0-9]{16}|merge:[a-f0-9]{16}|owner_answer:[A-Za-z0-9._:-]{1,48}'
                      r'|watch:[A-Za-z0-9._:-]{1,48}|date:\d{4}-\d{2}-\d{2}|builder)$')


def awaiting_line(awaiting):
    """What the owner (or the dashboard) should see for a waiting step. Approval codes are never stored or shown here:
    the full line is in the review message in Telegram."""
    if not awaiting:
        return None
    a = str(awaiting)
    m = re.match(r'^approval:(call|mail|web|calendar|purchase):(.+)$', a)
    if m:
        kind, ident = m.groups()
        cmd = {'call': '/callapprove', 'mail': '/mailsend', 'web': '/webtask or /webapprove', 'calendar': '/calapprove', 'purchase': 'the PURCHASE APPROVAL line'}[kind]
        what = {'call': 'phone call', 'mail': 'email', 'web': 'browser task', 'calendar': 'calendar change', 'purchase': 'purchase'}[kind]
        return 'Approve the %s %s in Telegram: %s %s … (the full line is in its review message)' % (what, ident, cmd, ident)
    if a.startswith('build:'):
        return 'Approve build %s in Telegram: /buildapprove %s … (the full line is in the BUILD REVIEW message)' % (a[6:], a[6:])
    if a.startswith('merge:'):
        return 'Waiting for the pull request of build %s to be merged (merge it on GitHub when you are happy with it)' % a[6:]
    if a.startswith('owner_answer:'):
        return 'Answer question %s in Telegram or on the dashboard' % a[13:]
    if a.startswith('watch:'):
        return 'Waiting for a matching email (watch %s)' % a[6:]
    if a.startswith('date:'):
        return 'Waiting until %s' % a[5:]
    if a == 'builder':
        return 'Waiting for a builder that allows the repo to come online'
    return None


def awaiting_ok(value):
    return value is None or value == '' or bool(AWAITING.match(str(value)))


def date_reached(awaiting, today):
    return awaiting.startswith('date:') and awaiting[5:] <= today


def brief(o, units, meta=None):
    """The work-session brief for ONE packet: identity, goal, done-when, kind rules, the next units with their routing,
    and what is waiting. o = Missions.overview(full=True) of this packet; units = Missions.next_units(); meta = packet meta."""
    meta = meta or o.get('meta') or {}
    lines = ['PACKET %s (%s packet, revision %s): %s' % (o['mission'], o['kind'], o['revision'], o['title']),
             'Goal: ' + (o.get('goal') or '(none stated: read the start file)')[:700]]
    if o.get('done_when'):
        lines.append('Done when (each needs evidence before you propose done):')
        lines += ['  %d. %s' % (i, d[:240]) for i, d in enumerate(o['done_when'], 1)]
    lines.append(KIND_RULES.get(o['kind'], KIND_RULES['administrative']))
    if meta.get('rules'):
        lines.append(meta['rules'])
    extra = []
    if meta.get('sites'):
        extra.append('saved logins for this packet: ' + ', '.join(meta['sites']))
    if meta.get('repo'):
        extra.append('builds: repo %s, builder %s, agent %s' % (meta['repo'], meta.get('builder') or 'any', meta.get('agent') or 'default'))
    if meta.get('deliverables'):
        extra.append('deliverables: ' + '; '.join(meta['deliverables']))
    if meta.get('deadline'):
        extra.append('packet deadline: ' + meta['deadline'])
    if o.get('budget_usd'):
        extra.append('budget $%.2f, spent $%.2f (the packet pauses itself at the budget)' % (o['budget_usd'], o.get('spent_usd') or 0))
    if extra:
        lines.append('Packet settings: ' + ' · '.join(extra))
    elig = units.get('eligible') or []
    if elig:
        lines.append('NEXT ELIGIBLE UNITS (take the first; one running step per packet):')
        for u in elig[:3]:
            r = ROUTES.get(u.get('type') or 'work', ROUTES['work'])
            lines.append('  • %s/%s [%s] %s%s' % (u['thread'], u['step'], u.get('type') or 'work', u['step_title'][:200], (' — ' + u['instruction']) if u.get('instruction') else ''))
            lines.append('      runs on: %s | gate: %s | evidence: %s' % (r['surface'], r['gate'], r['evidence']))
    else:
        lines.append('No eligible unit right now.')
    blocked = units.get('blocked') or []
    if blocked:
        lines.append('WAITING (do not redo these; check whether what they wait for has happened):')
        for b in blocked[:8]:
            lines.append('  • %s/%s %s — %s' % (b['thread'], b['step'], b['step_title'][:120], b.get('awaiting_line') or ', '.join(map(str, b.get('waiting_for') or []))))
    if units.get('unverified_deadlines') or units.get('deadlines_within_7_days'):
        lines.append('Deadline risk: unverified %s; within 7 days %s.' % (', '.join(units.get('unverified_deadlines') or []) or 'none', ', '.join(units.get('deadlines_within_7_days') or []) or 'none'))
    if units.get('open_questions'):
        lines.append('%d open question(s) for the owner are pending; do not ask them again.' % units['open_questions'])
    if not o.get('threads'):
        lines.append('This packet has no threads yet: read the packet (mission_read) and plan its threads with mission_thread (plan) before anything else.')
    return '\n'.join(lines)


ROUTING_RULES = '''HOW TO RUN A STEP
- Mark the step running (mission_thread update status=running) before you start it; only one step per packet runs at a time.
- Use the surface for its type (see "runs on"). The surface's own approval applies; never shortcut or simulate it.
- When you send a review for their approval, mark the step waiting_for_approval with awaiting = approval:call:<call id> | approval:mail:<draft id> | approval:web:<task id> | approval:calendar:<id> | build:<build id>. For a question: waiting_for_owner with awaiting = owner_answer:<question id>. For an email you wait on: add a mission_watch and keep the step pending with awaiting = watch:<watch id>; for a date: awaiting = date:YYYY-MM-DD.
- When it finishes, mark it done with up to 4 evidence references (confirmation number, message ID, call ID, Drive link, PR link).
- If it fails: mission_thread action=fail with the reason; transient=true for a page reload, busy builder or provider 5xx (it is retried once on the next session), false otherwise (it waits for them).
- For a build step, pass mission and step (THREAD/sN) to build_prepare: the PR link is written back to the step automatically. If the packet has a BUILD.md file, pass its full text as markdown (it carries repo, model, review, paths, acceptance); otherwise build repo/title/goal/acceptance from the step.
- Several build steps in one packet (long-horizon work): each build starts from the base branch as it is THEN, so a step that builds on an earlier step's code must wait for that pull request to be merged. When a build step is done (PR open), mark the next build step waiting_for_owner with awaiting = merge:<that build id> unless the packet says the steps are independent; it becomes eligible by itself once the owner merges (the builder reports merges). In that later build's goal, include a short "Previous steps" list: each earlier PR link and what it did, and anything the journal learned that the agent must know.
- When every done-when item has evidence, call mission_propose_done with the evidence for each item; the owner closes the packet.'''


def dry_run(o, units):
    """Read-only plan for Telegram: titles, step goals, surfaces and the approvals the owner will be asked for. Never packet text."""
    lines = ['DRY RUN — packet %s (%s, %s)' % (o['mission'], o['kind'], o['revision']), o['title'], '', 'Goal: ' + (o.get('goal') or '—')[:400]]
    if o.get('done_when'):
        lines += ['', 'Done when:'] + ['• ' + d[:200] for d in o['done_when']]
    approvals = {}
    for t in o.get('threads') or []:
        lines += ['', '%s (%d steps):' % (t['title'], len(t['steps']))]
        for s in t['steps'][:20]:
            ty = s.get('type') or 'work'
            gate = ROUTES.get(ty, ROUTES['work'])['gate']
            lines.append('%s. [%s] %s%s' % (s['id'][1:], ty, s['title'][:140], '' if s['status'] == 'pending' else ' — ' + s['status'].replace('_', ' ')))
            if ty in ('browse', 'mail', 'call', 'calendar', 'build'):
                approvals[ty] = approvals.get(ty, 0) + 1
    if not o.get('threads'):
        lines += ['', 'No steps yet: the first work session reads the packet and plans its threads.']
    names = {'browse': 'browser tasks (/webtask, final submit)', 'mail': 'emails (/mailsend)', 'call': 'phone calls (/callapprove)', 'calendar': 'calendar changes (/calapprove for guests)', 'build': 'builds (/buildapprove)'}
    lines += ['', 'Approvals you will be asked for: ' + (', '.join('%d %s' % (n, names[k]) for k, n in approvals.items()) if approvals else 'none known yet (steps of type work decide as they go)')]
    lines += ['Cadence %d min, hours %02d:00–%02d:00 ET, model %s%s.' % (o['cadence_minutes'], o['hours'][0], o['hours'][1], o.get('model') or 'main',
                                                                         (', budget $%.2f' % o['budget_usd']) if o.get('budget_usd') else ', no budget'),
              'Nothing was done: this is a read-only plan.']
    return '\n'.join(lines)[:3800]


def check_done(done_when, items):
    """items: [{'item': n (1-based), 'refs': [...]}]. Returns (ok, missing item numbers, normalized items)."""
    covered = {}
    if not isinstance(items, list):
        raise ValueError('evidence must be a list of {item, refs}.')
    for x in items:
        if not isinstance(x, dict):
            raise ValueError('each evidence entry is {item, refs}.')
        n = x.get('item')
        refs = x.get('refs')
        if not isinstance(n, int) or not 1 <= n <= max(1, len(done_when)):
            raise ValueError('item must be a done-when number from 1 to %d.' % max(1, len(done_when)))
        if not isinstance(refs, list) or not refs or len(refs) > 4 or not all(isinstance(r, str) and 2 <= len(r.strip()) <= 300 for r in refs):
            raise ValueError('refs for item %d must be 1–4 references.' % n)
        covered[n] = [r.strip() for r in refs]
    missing = [i for i in range(1, len(done_when) + 1) if i not in covered]
    return (not missing and bool(done_when)), missing, [{'item': k, 'refs': v} for k, v in sorted(covered.items())]


def today(now, tz):
    return dt.datetime.fromtimestamp(now, tz).strftime('%Y-%m-%d')
