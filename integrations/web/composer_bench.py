"""Packet Composer bench (web 0.24.1; PACKET_COMPOSER_BUILD_PLAN.md phase 3).

Eight scripted owner personas talk to the Composer. A cheap model plays the owner and answers ONLY from the persona's facts;
the Composer runs with the owner's chosen Composer model. Nothing is saved to the packet library: each run uses a throw-away
Composer store, and the real catalog (saved logins, builders, health grants) so the checks are honest.

Graded by code, per persona:
  ready        the draft passes the packet validator and has no "missing" coverage gaps
  facts        the persona's key facts appear in the packet (dates, names, amounts)
  types        the step types the task needs are there (e.g. a call for a phone-only clinic)
  forbidden    nothing that must not be there (an invented saved login, a "done" claim)
  turns, questions, cost
PASS = ready + facts ≥ 80% + types present + nothing forbidden; WARN = ready but something is off; FAIL = not ready after the
turn limit. Run it with /composerbench (all eight, about $1–4) or /composerbench quick (three), or on the server:
  python3 composer_bench.py [--quick]
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request

import composer
from composer import Composer

OWNER_MODEL = 'claude-haiku-5-5'
OWNER_PRICE = (1.0, 5.0)
MAX_TURNS = 7
OWNER_TOKENS = (1024, 2048)         # the simulated owner's answer budget; the second is the retry after an empty answer
OWNER_FALLBACK = 'I have no preference; use a sensible default.'

PERSONAS = [
    {'id': 'P1-leaveco', 'quick': True, 'opening': 'I need to report an absence on Leaveco for my intermittent leave.',
     'facts': ['The absence is on 2026-11-20, a full day (8 hours).', 'Use the saved Leaveco login.', 'Reason category: my own condition (intermittent leave).',
               'If the portal asks for anything else, ask me; never call Leaveco or my employer.', 'Budget $5.'],
     'expect_facts': ['2026-11-20', 'leaveco'], 'expect_types': ['browse'], 'forbid': [r'(?i)\bdone\b.*already']},
    {'id': 'P2-dentist-call', 'quick': True, 'opening': 'Book me a dental cleaning at Bright Smile Dental. They only take bookings by phone.',
     'facts': ['Bright Smile Dental, phone 404-555-0142.', 'I am an existing patient.', 'Any weekday morning in the next 4 weeks.', 'Do not accept a charge over $50 without asking.',
               'Put the visit on my calendar.', 'Budget $5.'],
     # 0.24.3 (B08): a dental booking is a health appointment; by phone it runs through health_call_brief → phone_prepare_call,
     # so an appointment step that books by phone satisfies "call".
     'expect_facts': ['404-555-0142', 'Bright Smile'], 'expect_types': [('call', 'appointment'), 'calendar'], 'forbid': [r'(?i)\bbrowse\b.*bright smile']},
    {'id': 'P3-bill-dispute', 'quick': False, 'opening': 'Comcast charged me $89.99 twice on my October bill. Get it refunded.',
     'facts': ['The duplicate charge is $89.99 on the October 2026 statement.', 'Account holder: me.', 'Call Comcast at 1-800-934-6489 first; if they refuse, draft a written complaint for me to send.',
               'Never agree to a new plan or contract.', 'Budget $8.'],
     'expect_facts': ['89.99', '1-800-934-6489'], 'expect_types': ['call'], 'forbid': [r'(?i)accept (a )?new plan']},
    {'id': 'P4-records-legal', 'quick': False, 'opening': 'Draft a request for my complete training file from my former employer.',
     'facts': ['The program coordinator email is coordinator@example-hospital.org.', 'Ask for evaluations, remediation notes and the probation letter from August 2026.',
               'I review every email before it goes out; nothing is filed anywhere.', 'Find whether the institution has a policy deadline for answering.', 'Budget $15.'],
     'expect_facts': ['coordinator@example-hospital.org', 'August 2026'], 'expect_types': ['mail'], 'forbid': [r'(?i)file (it )?with the (court|board)']},
    {'id': 'P5-research', 'quick': True, 'opening': 'Research home battery backup systems for my house and write me a comparison.',
     'facts': ['House is about 2,000 sq ft in Orlando, Florida.', 'Compare three systems: price installed, capacity, warranty.', 'Put the memo in my Google Drive.', 'Budget $8; no deadline.'],
     'expect_facts': ['Orlando', ('three', '3 systems', '3 home', '3 battery', '3 options')], 'expect_types': ['research', 'write'], 'forbid': []},
    {'id': 'P6-coding', 'quick': False, 'opening': 'Add a dark-mode toggle to my sandbox repo.',
     'facts': ['Repo example-org/procurator-sandbox.', 'Use any builder and the claude agent.', 'Done when the draft PR has the toggle and tests pass.', 'Budget $10.'],
     'expect_facts': ['procurator-sandbox'], 'expect_types': ['build'], 'forbid': []},
    {'id': 'P7-refill', 'quick': False, 'opening': 'Refill my Atorvastatin at the pharmacy.',
     'facts': ['Atorvastatin 20 mg, the prescription at my usual store.', 'Pickup, not delivery.', 'If it needs a renewal, tell me; do not contact my doctor.', 'Budget $3.',
               'If you need a grant or connection id, use whatever the assistant shows; if there is none, say I will set it up.'],
     'expect_facts': ['Atorvastatin'], 'expect_types': ['refill'], 'forbid': []},
    {'id': 'P8-dmv', 'quick': False, 'opening': 'Renew my car registration on the Florida DMV site.',
     'facts': ['Florida, plate number on file at the DMV; renewal due 2026-12-15.', 'There is no saved login for the DMV; the public renewal page is fine.',
               'Pay only the standard renewal fee (about $45); ask me before paying more.', 'Budget $5.'],
     'expect_facts': ['2026-12-15'], 'expect_types': ['browse'], 'forbid': [r'(?i)sites:\s*\S*dmv']},
]

OWNER_PROMPT = '''You are role-playing the OWNER of a personal assistant, talking to its packet-building chat. You want: "{opening}".
You know ONLY these facts:
{facts}
Answer the assistant's latest message in 1–3 short sentences, only from these facts. If it asks something the facts do not cover,
say you have no preference and it should use a sensible default. Never invent passwords, codes or numbers. If it says the packet
is ready to save, reply exactly: SAVE'''


def owner_call(key, persona, transcript, timeout=40):
    msgs = []
    for role, text in transcript:
        r = 'assistant' if role == 'owner' else 'user'       # the simulator IS the owner
        if msgs and msgs[-1]['role'] == r:
            msgs[-1]['content'] += '\n\n' + text
        else:
            msgs.append({'role': r, 'content': text})
    while msgs and msgs[0]['role'] != 'user':
        msgs.pop(0)
    system = OWNER_PROMPT.format(opening=persona['opening'], facts='\n'.join('- ' + f for f in persona['facts']))
    cost = 0.0
    # Nov 8: the first quick run failed two personas with "Type a message." — the simulated owner came back with no text
    # (a 300-token cap can be spent before any text, e.g. on thinking). Room to answer, one retry, then a neutral line.
    for max_tokens in OWNER_TOKENS:
        body = {'model': OWNER_MODEL, 'max_tokens': max_tokens, 'system': system, 'messages': msgs or [{'role': 'user', 'content': 'What do you need?'}]}
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps(body).encode(),
                                     headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
        u = out.get('usage') or {}
        cost += (int(u.get('input_tokens') or 0) * OWNER_PRICE[0] + int(u.get('output_tokens') or 0) * OWNER_PRICE[1]) / 1e6
        text = ' '.join(c.get('text', '') for c in out.get('content') or [] if c.get('type') == 'text').strip()
        if text:
            return text, cost
    return OWNER_FALLBACK, cost


def label(x):
    return x if isinstance(x, str) else '/'.join(x)


def has_fact(fact, text):
    """A fact counts if any alternative appears; phone numbers and amounts also match by their digits ("(404) 555-0142")."""
    low, digits = text.lower(), re.sub(r'\D', '', text)
    for alt in ((fact,) if isinstance(fact, str) else fact):
        if alt.lower() in low:
            return True
        d = re.sub(r'\D', '', alt)
        if len(d) >= 7 and d in digits:
            return True
    return False


def grade(persona, comp, sid, turns, owner_cost):
    g = comp.get(sid)
    text = '\n'.join(g['files'].values())
    facts_found = [f for f in persona['expect_facts'] if has_fact(f, text)]
    types = {s['type'] for s in g['spec'].get('steps') or []}
    missing_types = [label(t) for t in persona['expect_types'] if not ({t} if isinstance(t, str) else set(t)) & types]
    forbidden = [rx for rx in persona['forbid'] if re.search(rx, text)]
    questions = sum(len(m.get('questions') or []) for m in g['messages'] if m['role'] == 'assistant')
    fact_ratio = len(facts_found) / max(1, len(persona['expect_facts']))
    if not g['ready']:
        verdict = 'FAIL'
    elif fact_ratio >= 0.8 and not missing_types and not forbidden:
        verdict = 'PASS'
    else:
        verdict = 'WARN'
    why = []
    if not g['ready']:
        why.append('not ready: ' + '; '.join(g['errors'][:2] + [x['text'] for x in g['gaps'] if x['level'] == 'missing'][:2]))
    if fact_ratio < 0.8:
        why.append('facts missing: ' + ', '.join(label(f) for f in persona['expect_facts'] if f not in facts_found))
    if missing_types:
        why.append('step types missing: ' + ', '.join(missing_types))
    if forbidden:
        why.append('contains something it must not')
    return {'id': persona['id'], 'verdict': verdict, 'turns': turns, 'questions': questions, 'facts': '%d/%d' % (len(facts_found), len(persona['expect_facts'])),
            'steps': sorted(types), 'composer_cost': round(g['cost_usd'], 4), 'owner_cost': round(owner_cost, 4), 'model': g['model'], 'why': '; '.join(why)}


def run_persona(persona, make_composer, owner_key, owner=owner_call):
    comp = make_composer()
    sid = comp.new()['id']
    transcript, owner_cost, turns = [], 0.0, 0
    say = persona['opening']
    try:
        while turns < MAX_TURNS:
            turns += 1
            transcript.append(('owner', say))
            out = comp.message(sid, say)
            g = comp.get(sid)
            last = g['messages'][-1]
            transcript.append(('assistant', last['text'] + (('\n' + '\n'.join('- ' + q for q in last['questions'])) if last.get('questions') else '')))
            # Done only when the Composer says ready AND is not still asking something (it used to stop at turn 1 with 3 open questions).
            if out.get('error') or (g['ready'] and out.get('status') == 'draft_ready' and not last.get('questions')):
                break
            say, c = owner(owner_key, persona, transcript)
            owner_cost += c
            say = (say or '').strip() or OWNER_FALLBACK
            if say.upper().startswith('SAVE'):
                break
    except Exception as e:
        try:
            spent = comp.get(sid)['cost_usd']
        except Exception:
            spent = 0
        return failed(persona, 'turn %d: %s: %s' % (turns, type(e).__name__, str(e)[:160]), turns, spent, owner_cost)
    return grade(persona, comp, sid, turns, owner_cost)


def failed(persona, why, turns=0, composer_cost=0, owner_cost=0):
    return {'id': persona['id'], 'verdict': 'FAIL', 'turns': turns, 'questions': 0, 'facts': '-', 'steps': [], 'composer_cost': round(composer_cost or 0, 4),
            'owner_cost': round(owner_cost or 0, 4), 'model': '-', 'why': 'error at ' + why}


def report(results, model_label, started):
    n = {v: sum(r['verdict'] == v for r in results) for v in ('PASS', 'WARN', 'FAIL')}
    cost = sum(r['composer_cost'] + r['owner_cost'] for r in results)
    lines = ['Packet Composer bench · %s · %s' % (model_label, time.strftime('%Y-%m-%d %H:%M', time.localtime(started))),
             '%d PASS · %d WARN · %d FAIL · about $%.2f · %d s' % (n['PASS'], n['WARN'], n['FAIL'], cost, time.time() - started), '']
    for r in results:
        lines.append('%s %s — %d turns, %d questions, facts %s, steps %s, $%.2f%s' % (r['verdict'], r['id'], r['turns'], r['questions'], r['facts'], ','.join(r['steps']) or '—',
                                                                                  r['composer_cost'] + r['owner_cost'], (' — ' + r['why']) if r['why'] else ''))
    return '\n'.join(lines)


# 0.24.3 (B08): the bench measures the Composer's conversation, not this server's live prerequisites. Health packets need a
# connection and an owner-approved grant before they can be ready, and the live server may have none, so the bench adds
# SYNTHETIC ones (ids starting "be0") for its two health personas. Nothing is saved; no grant exists outside the bench.
BENCH_HEALTH = {
    'connections': [{'id': 'be0c000000000001', 'provider': 'clinic_phone', 'institution': 'Bright Smile Dental', 'environment': 'fake', 'status': 'connected'},
                    {'id': 'be0c000000000002', 'provider': 'mock_pharmacy', 'institution': 'Cornerstone Pharmacy (usual store)', 'environment': 'fake', 'status': 'connected'}],
    'grants': [{'id': 'be0a000000000001', 'title': 'Dental cleaning at Bright Smile Dental by phone', 'connection': 'be0c000000000001',
                'actions': ['appointment.search', 'appointment.book'], 'limits': {'provider': 'Bright Smile Dental', 'visit_type': 'cleaning', 'time_of_day': 'morning', 'spend_cap_usd': 50},
                'valid_to': None},
               {'id': 'be0a000000000002', 'title': 'Atorvastatin 20 mg refill, pickup', 'connection': 'be0c000000000002', 'actions': ['rx.status', 'rx.refill'],
                'limits': {'prescriptions': ['rx-bench-atorva20'], 'pharmacy': 'usual store', 'fulfillment': ['pickup']}, 'valid_to': None}]}


class BenchCatalog:
    def __init__(self, live):
        self.live = live

    def build(self):
        cat = dict(self.live.build()) if self.live else {}
        h = dict(cat.get('health') or {})
        h['connections'] = list(h.get('connections') or []) + BENCH_HEALTH['connections']
        h['grants'] = list(h.get('grants') or []) + BENCH_HEALTH['grants']
        cat['health'] = h
        return cat


class Bench:
    """Runs the bench in the background from the broker (/composerbench). One run at a time; the report goes to Telegram."""
    def __init__(self, broker, owner=owner_call):
        self.b, self.owner, self.running = broker, owner, False

    def make(self, tmp):
        live = self.b.composer
        return lambda: Composer(os.path.join(tmp, 'bench-%d.sqlite3' % time.time_ns()), missions=None, catalog=BenchCatalog(live.catalog), key_reader=live.key_reader,
                                caller=live.caller, checks=live.checks_fn, background=False, openai_reader=live.openai_reader, openai_caller=live.openai_caller,
                                settings=live.settings, main_model=live.main_model)

    def start(self, quick=False):
        if self.running:
            raise ValueError('A Composer bench is already running; its report will arrive here.')
        key = self.b.composer.key_reader() if self.b.composer.key_reader else None
        if not key:
            raise ValueError('The bench needs the Anthropic key on the server (the simulated owner runs on Claude Haiku).')
        personas = [p for p in PERSONAS if p['quick']] if quick else PERSONAS
        self.running = True

        def go():
            started, results = time.time(), []
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    make = self.make(tmp)
                    for p in personas:
                        try:
                            results.append(run_persona(p, make, key, self.owner))
                        except Exception as e:
                            results.append(failed(p, 'start: %s: %s' % (type(e).__name__, str(e)[:160])))
                name, _ = self.b.composer.resolve(None)
                text = report(results, composer.MODELS[name][3], started)
                try:
                    self.b.provider.telegram(text[:3800])
                except Exception:
                    pass
                self.last = {'at': started, 'results': results, 'text': text}
            finally:
                self.running = False
        threading.Thread(target=go, daemon=True).start()
        return {'started': True, 'personas': len(personas)}


if __name__ == '__main__':  # pragma: no cover - manual run on the server
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from broker import anthropic_key, openai_key
    key = anthropic_key()
    if not key:
        sys.exit('No ANTHROPIC_API_KEY in the server .env')
    personas = [p for p in PERSONAS if p['quick']] if '--quick' in sys.argv else PERSONAS
    with tempfile.TemporaryDirectory() as tmp:
        make = lambda: Composer(os.path.join(tmp, 'bench-%d.sqlite3' % time.time_ns()), key_reader=anthropic_key, openai_reader=openai_key, background=False)
        started = time.time()
        print(report([run_persona(p, make, key) for p in personas], 'Claude Sonnet 5.5 (default)', started))
