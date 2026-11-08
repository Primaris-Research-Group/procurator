"""Task packet format v1 (Nov 6 2026): front matter, kinds, step types, validation and the credential scan.

A packet is one or more Markdown files the owner writes and hands to PROCURATOR. The start file opens with
plain **Key:** value lines (the same style the mission store already reads for **Revision:**). Two shapes
are accepted:

- short packets that list typed steps ("1. type: browse: open the absence form …"), and
- long packets like the legal-matter example one: several files, a Mission section, "Done when" or a
  completion gate, and a table of journals ("| BG-NJ-r1 | step; step; step |") instead of typed steps.

Nothing here executes anything. The validator only decides whether a packet can be stored, infers the
metadata the dashboard needs (kind, goal, done-when, cadence, budget, model, threads) and refuses text
that carries a credential. It never reads outside the packet and never changes an approval.
"""
import re

KINDS = ('administrative', 'research', 'legal', 'coding', 'engineering', 'health')
STEP_TYPES = ('research', 'browse', 'mail', 'calendar', 'call', 'write', 'build', 'ask', 'wait', 'work', 'appointment', 'refill', 'portal_read', 'health_log')
ACTION_TYPES = ('browse', 'mail', 'call', 'calendar', 'appointment', 'refill')
MODELS = ('sonnet', 'opus', 'sol', 'terra')
BUILDERS = ('any', 'windows', 'mac')
AGENTS = ('codex', 'claude')
DEFAULT_CADENCE = {'administrative': 240, 'research': 360, 'legal': 480, 'coding': 120, 'engineering': 240, 'health': 240}
KEYS = {'revision': 'revision', 'kind': 'kind', 'goal': 'goal', 'done when': 'done_when', 'done_when': 'done_when', 'cadence': 'cadence',
        'hours': 'hours', 'budget': 'budget', 'model': 'model', 'deliverables': 'deliverables', 'deadline': 'deadline', 'repo': 'repo',
        'builder': 'builder', 'agent': 'agent', 'sites': 'sites', 'owner': 'owner', 'status': 'status', 'case': 'case', 'timezone': 'timezone',
        'priority': 'priority', 'title': 'title', 'subject': 'subject', 'grant': 'grant', 'connection': 'connection',
        'triggers': 'triggers', 'references': 'references'}
MAX_FILES, MAX_TOTAL, MAX_FILE = 25, 2_000_000, 400_000
FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,79}\.(md|txt)$')
FRONT = re.compile(r'^\s*\*\*([A-Za-z][A-Za-z _-]{1,20}):\*\*\s*(.*?)\s*$')
HEADING = re.compile(r'^(#{1,4})\s+(.+?)\s*#*\s*$')
STEP_LINE = re.compile(r'^\s*(?:\d{1,2}[.)]|[-*])\s+(?:\*\*)?type:\s*([a-z_]+)(?:\*\*)?\s*:?\s*(.*)$', re.I)
PLAIN_STEP = re.compile(r'^\s*\d{1,2}[.)]\s+(.+)$')
TABLE_ROW = re.compile(r'^\s*\|(.+)\|\s*$')
JOURNAL_ID = re.compile(r'^[A-Za-z][A-Za-z0-9._:-]{1,47}$')
ID_SAFE = re.compile(r'[^A-Za-z0-9._:-]')
MONEY = re.compile(r'^\$?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:usd)?$', re.I)
HOURS = re.compile(r'^(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})(?:\s*(?:ET|Eastern))?$', re.I)
REPO = re.compile(r'^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$')

# Credential shapes. Each match refuses the upload and names the file and line, never the value.
CREDENTIALS = [
    ('password', re.compile(r'(?i)\b(?:password|passwd|passcode|pass phrase|passphrase)\s*(?:is|[:=])\s*\S{4,}')),
    ('one-time code', re.compile(r'(?i)\b(?:one[- ]time code|otp|verification code|2fa code|security code|pin)\s*(?:is|[:=])\s*\d{4,8}\b')),
    ('API key', re.compile(r'\b(?:sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{30,}'
                           r'|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|sk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,})\b')),
    ('bearer token', re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}')),
    ('private key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('Social Security number', re.compile(r'(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])')),
]
CARD = re.compile(r'(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])')


def luhn(digits):
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def credential_scan(files):
    """[{file, line, kind}] for every line that looks like it carries a secret. Values are never returned."""
    hits = []
    for name in sorted(files):
        for n, line in enumerate(files[name].split('\n'), 1):
            kinds = [k for k, rx in CREDENTIALS if rx.search(line)]
            for m in CARD.finditer(line):
                digits = re.sub(r'\D', '', m.group())
                if 13 <= len(digits) <= 19 and luhn(digits) and len(set(digits)) >= 2:
                    kinds.append('card number')
                    break
            for k in kinds:
                hits.append({'file': name, 'line': n, 'kind': k})
    return hits


def start_file(files):
    names = sorted(files)
    return next((n for n in names if re.search(r'start|readme|^00', n, re.I)), names[0])


def front_matter(text):
    """**Key:** value lines near the top of the start file (before the first ## section)."""
    out, seen_body = {}, 0
    for line in text.split('\n')[:80]:
        if HEADING.match(line) and HEADING.match(line).group(1) != '#':
            break
        m = FRONT.match(line)
        if m:
            key = KEYS.get(m.group(1).strip().lower().replace('-', ' '))
            if key and key not in out:
                out[key] = m.group(2).strip()
            continue
        if line.strip() and not HEADING.match(line):
            seen_body += 1
            if seen_body > 12:
                break
    return out


def sections(text):
    """[(level, heading, body)] for every heading in a file; body is the text up to the next heading."""
    out, cur, buf = [], None, []
    for line in text.split('\n'):
        m = HEADING.match(line)
        if m:
            if cur:
                out.append((cur[0], cur[1], '\n'.join(buf)))
            cur, buf = (len(m.group(1)), m.group(2).strip()), []
        else:
            buf.append(line)
    if cur:
        out.append((cur[0], cur[1], '\n'.join(buf)))
    return out


def bullets(body, limit=20):
    items = []
    for line in body.split('\n'):
        m = re.match(r'^\s*(?:[-*•]|\d{1,2}[.)])\s+(.+?)\s*$', line)
        if m:
            items.append(re.sub(r'\*\*', '', m.group(1)).strip())
    return items[:limit]


def first_paragraph(body):
    para = []
    for line in body.split('\n'):
        if line.strip():
            para.append(line.strip())
        elif para:
            break
    return ' '.join(para).strip()


def find_section(all_sections, words):
    for level, heading, body in all_sections:
        h = heading.lower()
        if level > 1 and any(w in h for w in words):
            return heading, body
    return None, None


KIND_WORDS = {
    'legal': ('grievance', 'arbitration', 'attorney', 'lawyer', 'bar association', 'disciplinary', 'court', 'complaint', 'counsel', 'filing', 'docket', 'ardc', 'legal'),
    'coding': ('pull request', 'draft pr', 'repo', 'build_prepare', 'unit test', 'codex', 'claude code', 'refactor', 'bugfix', 'source code'),
    'engineering': ('simulation', 'finite element', 'schematic', 'hvac', 'load calculation', 'controller', 'firmware', 'engineering'),
    'research': ('literature', 'systematic review', 'pubmed', 'sources', 'summarize the evidence', 'research memo', 'compare options'),
    'administrative': ('appointment', 'reservation', 'form', 'portal', 'claim', 'absence', 'booking', 'renewal', 'invoice', 'customer service'),
    'health': ('prescription', 'refill', 'pharmacy', 'mychart', 'patient portal', 'dermatolog', 'clinic', 'follow-up visit', 'health grant', 'healthgrant', 'food log', 'calories'),
}


def infer_kind(text):
    low = text.lower()
    scores = {k: sum(low.count(w) for w in words) for k, words in KIND_WORDS.items()}
    best = max(scores, key=lambda k: (scores[k], k == 'administrative'))
    if scores[best] == 0:
        return 'administrative', 'default'
    return best, 'inferred from the packet text (%s)' % ', '.join(sorted(w for w in KIND_WORDS[best] if w in low)[:3])


def parse_steps(files):
    """Typed steps from a Steps section ("1. type: browse: …"). Untyped numbered lines there become type work."""
    steps = []
    for name in sorted(files):
        for level, heading, body in sections(files[name]):
            if not re.search(r'\bsteps?\b', heading.lower()) or 'read these files' in heading.lower():
                continue
            for line in body.split('\n'):
                m = STEP_LINE.match(line)
                if m:
                    steps.append({'file': name, 'type': m.group(1).lower(), 'title': re.sub(r'\*\*', '', m.group(2)).strip()[:200] or m.group(1).lower(), 'explicit': True})
                    continue
                m = PLAIN_STEP.match(line)
                if m and heading.lower().startswith('steps') and not line.strip().startswith('|') and len(steps) < 60:
                    title = re.sub(r'\*\*', '', m.group(1)).strip()
                    if 3 <= len(title) <= 300 and not title.startswith('['):
                        steps.append({'file': name, 'type': 'work', 'title': title[:200], 'explicit': False})
    return steps[:60]


STRICT_JOURNAL = re.compile(r'^[A-Z]{2,8}-[A-Z0-9]{2,12}(?:-r\d{1,2})?$')


def parse_journals(files):
    """Threads from a markdown table of journals: either the header names journals/threads and steps,
    or the first cell is a strict journal ID like BG-NJ-r1. The second cell lists the steps with ';'."""
    threads, seen = [], set()
    for name in sorted(files):
        lines = files[name].split('\n')
        i = 0
        while i < len(lines):
            m = TABLE_ROW.match(lines[i])
            if not m or i + 1 >= len(lines) or not re.match(r'^\s*\|[\s:|-]+\|\s*$', lines[i + 1]):
                i += 1
                continue
            header = [c.strip().lower() for c in m.group(1).split('|')]
            named = any(('journal' in h or 'thread' in h) for h in header[:1]) and any('step' in h for h in header[1:])
            i += 2
            while i < len(lines) and TABLE_ROW.match(lines[i]):
                cells = [c.strip() for c in TABLE_ROW.match(lines[i]).group(1).split('|')]
                i += 1
                if len(cells) < 2:
                    continue
                tid = re.sub(r'\*\*|`', '', cells[0]).strip()
                if not JOURNAL_ID.match(tid) or (not named and not STRICT_JOURNAL.match(tid)) or ';' not in cells[1]:
                    continue
                steps = [s.strip() for s in re.sub(r'\*\*', '', cells[1]).split(';') if s.strip()]
                if len(steps) < 2 or tid in seen:
                    continue
                seen.add(tid)
                threads.append({'file': name, 'thread': tid[:48], 'title': tid[:48], 'steps': [s[:200] for s in steps[:20]]})
    return threads[:12]


def parse_budget(value):
    m = MONEY.match(value.strip())
    if not m:
        raise ValueError('Budget must look like $15 (whole dollars, $1–$200).')
    amount = float(m.group(1))
    if not 1 <= amount <= 200:
        raise ValueError('Budget must be between $1 and $200.')
    return round(amount, 2)


def parse_hours(value):
    m = HOURS.match(value.strip())
    if not m:
        raise ValueError('Hours must look like 07:00-21:00 (Eastern).')
    a, b = int(m.group(1)), int(m.group(3))
    if not 0 <= a < b <= 24 or m.group(2) != '00' or m.group(4) != '00':
        raise ValueError('Hours use whole hours, start before end, e.g. 08:00-20:00.')
    return a, b


def validate(files, saved_logins=None, builders=None):
    """Return {'ok', 'errors', 'warnings', 'packet'}. errors non-empty means the packet is refused.

    saved_logins: names of the owner's saved site logins (None = don't check).
    builders: [{'name','online','repos'}] from the build store (None = don't check)."""
    errors, warnings = [], []
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        return {'ok': False, 'errors': ['Upload 1–%d .md or .txt files.' % MAX_FILES], 'warnings': [], 'packet': None}
    for name, body in files.items():
        if not isinstance(name, str) or not FILE.match(name):
            errors.append('File names must end in .md or .txt and use plain characters: %s' % str(name)[:80])
        elif not isinstance(body, str) or not body.strip():
            errors.append('%s is empty.' % name)
        elif len(body) > MAX_FILE:
            errors.append('%s is larger than %d characters.' % (name, MAX_FILE))
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'packet': None}
    if sum(len(b) for b in files.values()) > MAX_TOTAL:
        return {'ok': False, 'errors': ['The packet is larger than 2 MB of text.'], 'warnings': warnings, 'packet': None}
    for hit in credential_scan(files):
        errors.append('%s line %d looks like it contains a %s. Packets never carry credentials; remove it and upload again.' % (hit['file'], hit['line'], hit['kind']))
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'packet': None}

    start = start_file(files)
    head = files[start]
    fm = front_matter(head)
    all_text = '\n'.join(files[n] for n in sorted(files))
    secs = [s for n in sorted(files) for s in sections(files[n])]
    p = {'start': start, 'files': sorted(files), 'revision': fm.get('revision') or None, 'front_matter': fm}
    title = re.search(r'^#\s+(.+)$', head, re.M)
    p['title'] = (fm.get('title') or (title.group(1) if title else start)).strip()[:120]
    if p['revision'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,60}', p['revision']):
        errors.append('Revision must be 3–61 letters, digits, dots, dashes or underscores (e.g. LEAVECO-2026-11-10-r1).')
    if not p['revision']:
        warnings.append('No **Revision:** line in %s; the upload date will be used as the revision.' % start)

    kind = (fm.get('kind') or '').strip().lower()
    if kind and kind not in KINDS:
        errors.append('Kind must be one of: %s.' % ', '.join(KINDS))
        kind = ''
    if kind:
        p['kind'], p['kind_source'] = kind, 'stated'
    else:
        p['kind'], p['kind_source'] = infer_kind(all_text)
        warnings.append('No **Kind:** line; treating this as a %s packet (%s). Add **Kind:** to set it.' % (p['kind'], p['kind_source']))
    kind = p['kind']

    goal = fm.get('goal') or ''
    if not goal:
        h, body = find_section(secs, ('mission', 'goal', 'objective', 'purpose'))
        if body:
            goal = first_paragraph(body)
            if goal:
                warnings.append('No **Goal:** line; using the first paragraph of "%s".' % h)
    if not goal:
        goal = p['title']
        warnings.append('No **Goal:** line or Mission section; using the title as the goal. Add **Goal:** (one sentence) so the done test is clear.')
    p['goal'] = re.sub(r'\s+', ' ', re.sub(r'\*\*', '', goal)).strip()[:600]

    done = []
    if fm.get('done_when'):
        done = [x.strip() for x in re.split(r'\s*;\s*|\s*\|\s*', fm['done_when']) if x.strip()]
    if not done:
        for level, heading, body in secs:
            hl = heading.lower()
            if ('done when' in hl or 'done:' in hl or hl.strip() == 'done'):
                done = bullets(body)
                if done:
                    break
    if not done:
        # A "**Done when:**" line followed by bullets (the short format) sits inside the front matter block.
        m = re.search(r'\*\*Done when:\*\*\s*\n((?:\s*[-*]\s+.+\n?)+)', head)
        if m:
            done = bullets(m.group(1))
    if not done:
        h, body = find_section(secs, ('definition of success', 'completion gate', 'success', 'acceptance'))
        if body:
            done = bullets(body)
            if done:
                warnings.append('No **Done when:** list; using the bullets under "%s" as the done-when checklist.' % h)
    if not done:
        warnings.append('No done-when checklist found. The packet can run, but it will not close on its own; add **Done when:** bullets to the start file.')
    p['done_when'] = [d[:300] for d in done[:12]]

    cadence = DEFAULT_CADENCE[kind] if p['kind_source'] == 'stated' else 240  # an inferred kind never changes timing
    if fm.get('cadence'):
        m = re.match(r'^\s*(\d{2,4})\s*(?:min(?:utes)?)?\s*$', fm['cadence'], re.I)
        if not m or not 60 <= int(m.group(1)) <= 1440:
            errors.append('Cadence is a number of minutes between 60 and 1440.')
        else:
            cadence = int(m.group(1))
    p['cadence'] = cadence
    p['hours'] = (7, 21)
    if fm.get('hours'):
        try:
            p['hours'] = parse_hours(fm['hours'])
        except ValueError as e:
            errors.append(str(e))
    p['budget_usd'] = None
    if fm.get('budget'):
        try:
            p['budget_usd'] = parse_budget(fm['budget'])
        except ValueError as e:
            errors.append(str(e))
    model = (fm.get('model') or '').strip().lower()
    if model and model not in MODELS:
        errors.append('Model must be one of: %s.' % ', '.join(MODELS))
        model = ''
    p['model'] = model or None
    p['deliverables'] = [x.strip() for x in re.split(r'\s*;\s*', fm.get('deliverables') or '') if x.strip()][:10]
    p['deadline'] = (fm.get('deadline') or '').strip()[:60] or None
    if p['deadline'] and not re.match(r'^\d{4}-\d{2}-\d{2}', p['deadline']):
        warnings.append('Deadline "%s" is not an ISO date (2026-11-20); it is shown but not used for priority.' % p['deadline'])
    p['sites'] = [s.strip().lower() for s in re.split(r'[,; ]+', fm.get('sites') or '') if s.strip()][:10]
    # 0.28.0: **Triggers:** inbound event types that wake this packet (mock-social.comment, slack.dm); the event stays untrusted
    # and anything the packet then sends or posts still needs approval. **References:** library collections or items it may read.
    p['triggers'] = []
    for t in re.split(r'[,; ]+', fm.get('triggers') or ''):
        t = t.strip().lower()
        if not t:
            continue
        if re.fullmatch(r'[a-z][a-z0-9-]{1,20}\.(message|mention|comment|reply|dm|reaction|device)', t):
            p['triggers'].append(t)
        else:
            errors.append('Triggers are SOURCE.TYPE, e.g. mock-social.comment or slack.dm (got "%s").' % t[:40])
    p['triggers'] = p['triggers'][:10]
    p['references'] = []
    for r in re.split(r'[,;]+', fm.get('references') or ''):
        r = r.strip().lower()
        if not r:
            continue
        if re.fullmatch(r'(?:lib_[a-f0-9]{12}|[a-z0-9][a-z0-9-]{0,39})', r):
            p['references'].append(r)
        else:
            errors.append('References name library collections (short names) or items (lib_…), separated by commas (got "%s").' % r[:40])
    p['references'] = p['references'][:10]
    if saved_logins is not None:
        for s in p['sites']:
            if s not in saved_logins:
                errors.append('Sites names a login "%s" that is not saved. Save it first (/siteconnect %s) or remove it.' % (s, s))

    steps = parse_steps(files)
    for s in steps:
        if s['type'] not in STEP_TYPES:
            errors.append('Step "%s" in %s has an unknown type "%s". Types: %s.' % (s['title'][:60], s['file'], s['type'], ', '.join(STEP_TYPES)))
    p['steps'] = steps
    p['threads'] = parse_journals(files)
    if not steps and not p['threads']:
        warnings.append('No typed Steps list or journals table; the assistant will plan its own threads from the packet text.')

    repo, builder, agent = (fm.get('repo') or '').strip(), (fm.get('builder') or 'any').strip().lower(), (fm.get('agent') or '').strip().lower()
    p['repo'] = repo or None
    p['builder'] = builder
    p['agent'] = agent or None
    if repo and not REPO.match(repo):
        errors.append('Repo must be owner/name on GitHub.')
    if builder not in BUILDERS:
        errors.append('Builder must be any, windows or mac.')
    if agent and agent not in AGENTS:
        errors.append('Agent must be codex or claude.')
    has_build = any(s['type'] == 'build' for s in steps)
    if kind == 'coding':
        strict = errors if p['kind_source'] == 'stated' else warnings  # an inferred kind never refuses a packet
        if not repo:
            strict.append('A coding packet needs **Repo:** owner/name.')
        if not agent:
            warnings.append('No **Agent:** line; the builder\'s default coding agent will be used.')
        if not has_build:
            strict.append('A coding packet needs at least one "type: build" step.')
        if not p['done_when']:
            strict.append('A coding packet needs **Done when:** bullets the build can test.')
    if kind == 'engineering' and has_build and not repo:
        errors.append('An engineering packet with a build step needs **Repo:** owner/name.')
    if (kind in ('coding', 'engineering') or has_build) and repo and builders is not None:
        pool = [b for b in builders if builder in ('any', b['name'])]
        able = [b for b in pool if repo in (b.get('repos') or [])]
        if not able:
            errors.append('%s is not on the allowed list of %s. Add it on that machine: python3 ~/Procurator/builder/worker.py add-repo %s' % (repo, 'any builder' if builder == 'any' else builder, repo))
        elif not any(b.get('online') for b in able):
            warnings.append('The builder that allows %s is offline right now; the build step will wait for it.' % repo)
    if kind == 'administrative' and steps and not any(s['type'] in ACTION_TYPES for s in steps):
        warnings.append('An administrative packet usually has a browse, mail, call or calendar step; this one has none.')
    if kind == 'research' and not p['deliverables'] and not any(s['type'] == 'write' for s in steps):
        warnings.append('A research packet usually names **Deliverables:** (e.g. memo in Drive; summary in Telegram).')
    if kind == 'legal':
        if not find_section(secs, ('rule', 'authority', 'limits'))[0]:
            warnings.append('A legal packet should have a Rules section (what the assistant must never do on its own).')
        p['rules'] = 'Legal packet: drafts and research only. Every outgoing message is drafted for review; nothing is filed with a court or agency without the owner.'
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'packet': p if not errors else None}


TEMPLATES = {
    'health': '''# Dermatology follow-up: book within my grant
**Revision:** DERM-2026-11-10-r1
**Kind:** health
**Goal:** Book a routine follow-up with Dr. <name> on a Tuesday afternoon within 20 miles, under health grant <GRANT>.
**Done when:**
- The clinic shows a confirmation number for the visit
- The visit is on my calendar with that confirmation number
- I have the private receipt link
**Grant:** <GRANT id from /healthgrant>
**Connection:** <connection id>
**Budget:** $5

## Facts
- Existing patient; visit type: follow-up
- Acceptable: Tuesdays 1–5 PM in the next 6 weeks; Midtown or Buckhead locations
- Do not disclose anything beyond the grant's disclosures

## Steps
1. type: appointment: search the clinic's online booking for Tuesday afternoon follow-up slots that fit the grant; if none, ask me.
2. type: appointment: book the earliest fitting slot (health_effect_begin → dispatch → settle); a "request received" page is NOT a confirmation.
3. type: calendar: add the confirmed visit with its confirmation number; send me one neutral line and the receipt link.
''',
    'administrative': '''# Leaveco absence: <date>
**Revision:** LEAVECO-2026-11-10-r1
**Kind:** administrative
**Goal:** Report the <date> intermittent-leave absence on Leaveco and save the confirmation.
**Done when:**
- Leaveco shows a confirmation number for <date>
- The confirmation is in the activity log
**Sites:** leaveco
**Budget:** $3

## Facts
- Date <date>, 8:00 AM–5:00 PM, 9 hours, no unpaid break, no mandatory overtime
- Reason: episode of incapacity

## Steps
1. type: browse: open the absence form for the active leave and fill it from Facts; stop for my final submit.
2. type: ask: if Leaveco shows a duplicate warning, ask me before continuing.
''',
    'research': '''# <topic>: research memo
**Revision:** RES-2026-11-10-r1
**Kind:** research
**Goal:** <one sentence: the question to answer and for whom>
**Done when:**
- A memo with sources is in Drive
- A five-line summary is in Telegram
**Deliverables:** memo in Drive; summary in Telegram
**Cadence:** 360
**Budget:** $10

## Context
<what I already know and why this matters>

## Steps
1. type: research: find and read the primary sources (guidelines, trials, official pages); record each with its URL and date.
2. type: write: draft the memo (question, findings, uncertainties, sources) to Drive.
3. type: ask: send me the summary and ask whether anything needs a deeper look.
''',
    'legal': '''# <matter>: administration packet
**Revision:** LEG-2026-11-10-r1
**Kind:** legal
**Goal:** <one sentence: the outcome to work toward>
**Done when:**
- <each outcome that counts, with the evidence that proves it>
**Deliverables:** case ledger in Drive; drafts for my review
**Cadence:** 480
**Budget:** $25

## Context
<parties, dates, references, what already happened>

## Facts
- <values the assistant may use: dates, reference numbers, addresses; never passwords or codes>

## Rules
- Draft every letter, email and form for my review; never file with a court or agency on its own.
- Never settle, waive, withdraw, consent to arbitration or sign for anyone.
- Ask before contacting any party for the first time.

## Steps
1. type: research: verify the current official procedure and contacts; record sources.
2. type: write: draft the first submission to Drive.
3. type: ask: send me the draft and the open questions.
''',
    'coding': '''# <repo>: <change>
**Revision:** CODE-2026-11-10-r1
**Kind:** coding
**Goal:** <one sentence: what the change does>
**Done when:**
- <a testable check, e.g. "python3 -m unittest passes">
- <another check the reviewer can verify in the draft PR>
**Repo:** example-org/procurator-sandbox
**Builder:** any
**Agent:** codex
**Cadence:** 120
**Budget:** $10

## Context
<where the code lives, conventions to keep, what not to touch>

## Steps
1. type: build: implement the change and its tests; open a draft PR.
2. type: ask: send me the PR link and the test result; ask whether to address review comments.
''',
    'engineering': '''# <project>: <analysis or design>
**Revision:** ENG-2026-11-10-r1
**Kind:** engineering
**Goal:** <one sentence: the design or analysis to deliver>
**Done when:**
- <the document or model exists in Drive with its assumptions and results>
**Deliverables:** calculation note in Drive
**Cadence:** 240
**Budget:** $15

## Context
<system, constraints, standards, data sources>

## Steps
1. type: research: gather the inputs (standards, data sheets, measurements) with sources.
2. type: write: produce the calculation note (assumptions, method, results, checks) to Drive.
3. type: ask: send me the summary and the open assumptions.
''',
}
