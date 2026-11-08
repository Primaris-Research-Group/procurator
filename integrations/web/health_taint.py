"""Health data labels and leak checks (web 0.25.0, H6 in claude/PROCURATOR_INTEGRATIONS_AND_HEALTH_LESSONS_2026-11-08.md).

Health values the assistant has seen carry a label: names of clinicians, practices, pharmacies, medications, visit reasons and
phone numbers that came back from a health tool, plus the values written into the owner's health grants and connections. Before
a tool sends text to someone else (an email draft, a phone brief, a web form step, a calendar invitation), the plugin gate asks
this module whether the text carries a labeled value (`health_taint_check`). If it does, the text may only go to a recipient a
health grant allows: the grant's own connection (its endpoint: portal host or clinic phone) or a recipient named in its
disclosures. The owner's own address is always allowed. Anything else is refused before it leaves, and nothing is sent.

Audiences (0.27.0): for a public or workspace audience (a post, a channel message) labeled text is refused outright — no grant,
disclosure or owner address makes it allowed. labels(text) marks inbound events that carry labeled values.

What it is not: a filter of medical vocabulary (an evidence search for a drug name is not a disclosure), or a check of the
owner's own accounts (their calendar, their Drive). Values expire after 30 days unless a grant or connection still names them.
"""
import json
import re
import time

KEYS = re.compile(r'(?i)(?:^|_)(?:name|display|clinician|practitioner|provider|doctor|physician|institution|practice|clinic|location|'
                  r'address|phone|pharmacy|medication|medicine|drug|prescription|rx|reason|condition|diagnosis|problem|specialty|'
                  r'visit_type|service|test|procedure|allergy|title|label)(?:s|_name|_text|_display)?$')
SKIP_KEYS = re.compile(r'(?i)(?:^|_)(?:id|ids|hash|status|state|kind|source|environment|step|request_id|at|time|date|created|updated|'
                       r'expires|url|link|token|token_ref|code|unit|units|tz|type)$')
STOP = set('''appointment appointments visit visits office clinic pharmacy refill refills prescription prescriptions confirmed pending
booked cancelled canceled requested waitlisted failed uncertain queued authorized dispatching draft held none unknown today tomorrow
morning afternoon evening monday tuesday wednesday thursday friday saturday sunday january february march april may june july
august september october november december primary care follow up follow-up new patient established patient telehealth in person
in-person video phone call message portal health medical doctor nurse other self owner owner active revoked retired connected
reauth required sandbox production fake mock true false yes no ok'''.split())
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-Z]*)?$')
HEX = re.compile(r'^[a-f0-9]{8,}$')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
TTL = 30 * 86400


def norm(text):
    return ' '.join(re.sub(r'[^0-9a-z]+', ' ', str(text or '').lower()).split())


def digits(text):
    return re.sub(r'\D', '', str(text or ''))


def phone_key(value):
    d = digits(value)
    if 10 <= len(d) <= 11 and (len(d) == 10 or d.startswith('1')):
        return d[-10:]
    return None


def label_worthy(value):
    v = str(value).strip()
    if not (4 <= len(v) <= 80) or ISO.match(v) or HEX.match(v.lower()) or v.lower().startswith(('http://', 'https://')):
        return None
    n = norm(v)
    if not n or n in STOP or len(n) < 4:
        return None
    words = n.split()
    if all(w in STOP or w.isdigit() for w in words):
        return None
    return n


class Taint:
    def __init__(self, health, now=time.time):
        self.h, self.now = health, now
        with self.h.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS taint_values(value TEXT PRIMARY KEY,kind TEXT,source TEXT,created REAL,expires REAL)')

    # ---------- labeling ----------
    def collect(self, node, key='', out=None, depth=0):
        out = out if out is not None else {}
        if depth > 8 or len(out) > 400:
            return out
        if isinstance(node, dict):
            for k, v in node.items():
                self.collect(v, str(k), out, depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node[:200]:
                self.collect(v, key, out, depth + 1)
        elif isinstance(node, str) and key and not SKIP_KEYS.search(key):
            p = phone_key(node) if re.search(r'(?i)phone|tel|fax|number', key) else None
            if p:
                out[p] = 'phone'
            elif KEYS.search(key):
                n = label_worthy(node)
                if n:
                    out[n] = 'text'
        return out

    def note(self, result, source='health tool'):
        """Labels the health values in a result the assistant is about to see. Returns how many were new."""
        found = self.collect(result)
        if not found:
            return 0
        t = self.now()
        with self.h.db() as db:
            before = db.total_changes
            for value, kind in found.items():
                db.execute('INSERT INTO taint_values(value,kind,source,created,expires) VALUES(?,?,?,?,?) '
                           'ON CONFLICT(value) DO UPDATE SET expires=excluded.expires', (value, kind, source[:40], t, t + TTL))
            return db.total_changes - before

    def standing(self, db):
        """Values named by active grants and live connections (they never expire while the grant or connection lives)."""
        out = {}
        for g in db.execute("SELECT constraints,title FROM grants WHERE status='active'"):
            try:
                c = json.loads(g['constraints'] or '{}')
            except ValueError:
                c = {}
            for k in ('clinician', 'provider', 'pharmacy', 'prescriptions', 'locations', 'specialty', 'visit_type'):
                self.collect({k: c.get(k)}, out=out)
        for c in db.execute("SELECT institution,endpoint FROM connections WHERE status IN ('pending','connected','reauth_required')"):
            self.collect({'institution': c['institution'], 'phone': c['endpoint'] or ''}, out=out)
        return out

    def values(self):
        t = self.now()
        with self.h.db() as db:
            db.execute('DELETE FROM taint_values WHERE expires<?', (t,))
            out = {r['value']: r['kind'] for r in db.execute('SELECT value,kind FROM taint_values')}
            out.update(self.standing(db))
        return out

    # ---------- recipients ----------
    @staticmethod
    def recipients(destination):
        """Normalizes a destination (email list, phone, URL, host, or a list of them) into comparable keys."""
        items = destination if isinstance(destination, (list, tuple)) else [destination]
        keys = set()
        for d in items:
            if isinstance(d, dict):
                d = d.get('email') or d.get('address') or d.get('phone') or d.get('url') or ''
            s = str(d or '').strip().lower()
            if not s:
                continue
            for m in EMAIL.finditer(s):
                keys.add('email:' + m.group(0))
                keys.add('domain:' + m.group(1).removeprefix('www.'))
            p = phone_key(s)
            if p and not EMAIL.search(s):
                keys.add('phone:' + p)
            host = re.match(r'^(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})(?::\d+)?(?:[/?#].*)?$', s)
            if host and '@' not in s:
                keys.add('domain:' + host.group(1).removeprefix('www.'))
        return keys

    def allowed_recipients(self, db, owner_addresses=()):
        keys = set()
        for a in owner_addresses:
            keys |= {k for k in self.recipients(a) if k.startswith('email:')}
        conns = {c['id']: c for c in db.execute('SELECT id,endpoint,institution,contacts FROM connections')}
        for g in db.execute("SELECT connection,disclosures FROM grants WHERE status='active'"):
            c = conns.get(g['connection'])
            if c and c['endpoint']:
                keys |= self.recipients(c['endpoint'])
            if c:
                try:
                    keys |= self.recipients(json.loads(c['contacts'] or '[]'))
                except ValueError:
                    pass
            try:
                disclosures = json.loads(g['disclosures'] or '[]')
            except ValueError:
                disclosures = []
            for d in disclosures if isinstance(disclosures, list) else []:
                text = d.get('to') if isinstance(d, dict) else d
                for token in re.split(r'[\s,;]+', str(text or '')):
                    keys |= self.recipients(token)
        return keys

    @staticmethod
    def covered(dest, allowed):
        """Every recipient must be allowed: an exact address or phone, an address at an allowed domain, or an allowed web host."""
        if not dest:
            return False
        exact = [k for k in dest if not k.startswith('domain:')]
        if not exact:
            return dest <= allowed
        for k in exact:
            if k in allowed:
                continue
            if k.startswith('email:') and 'domain:' + k.split('@', 1)[1] in allowed:
                continue
            return False
        return True

    # ---------- the check ----------
    def labels(self, text):
        """['health'] when the text carries a labeled health value (inbound events are labeled this way), else []."""
        return ['health'] if self._hits(str(text or '')[:60000]) else []

    def _hits(self, body):
        n = ' ' + norm(body) + ' '
        runs = set(re.findall(r'\d{10,11}', digits(body))) | {digits(x)[-10:] for x in re.findall(r'\+?1?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4}', body)}
        hits = []
        for value, kind in self.values().items():
            if kind == 'phone':
                if any(r.endswith(value) for r in runs):
                    hits.append(kind)
            elif ' ' + value + ' ' in n:
                hits.append(kind)
        return hits

    def check(self, text, destination=None, owner_addresses=(), audience=None):
        body = str(text or '')[:60000]
        if audience in ('public', 'workspace'):
            # 0.27.0 hard block: labeled health text never goes to a public or workspace audience, whatever the grants say.
            hits = self._hits(body)
            if hits:
                return {'tainted': True, 'allowed': False, 'labels': len(hits), 'hard_block': True,
                        'reason': ('The text carries health details (%d labeled value%s from their health records or grants), and this is a %s '
                                   'destination (%s). Health details never go there, whatever the grants say. Nothing was posted or saved.') % (
                                       len(hits), '' if len(hits) == 1 else 's', audience,
                                       'anyone on the internet' if audience == 'public' else 'everyone in the workspace or channel')}
            return {'tainted': False, 'allowed': True}
        hits = self._hits(body)
        if not hits:
            return {'tainted': False, 'allowed': True}
        dest = self.recipients(destination)
        with self.h.db() as db:
            allowed = self.allowed_recipients(db, owner_addresses)
        ok = self.covered(dest, allowed)
        return {'tainted': True, 'allowed': ok, 'labels': len(hits),
                'reason': None if ok else ('The text carries health details (%d labeled value%s from their health records or grants), and %s is not a '
                                           'recipient any active health grant allows. Nothing was sent or saved. The owner can add this recipient to a '
                                           'grant\'s disclosures (/healthgrant), or send it themselves.' % (len(hits), '' if len(hits) == 1 else 's',
                                                                                                          'the destination' if dest else 'no recipient'))}
