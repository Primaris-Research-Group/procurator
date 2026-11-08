"""Connector framework (web 0.25.0, Nov 8 2026; audiences, accounts, media and rate limits 0.27.0; claude/PROCURATOR_INTEGRATIONS_AND_HEALTH_LESSONS_2026-11-08.md).

Every integration PROCURATOR ships, and every extension pack it may load later, is described by one manifest
(connectors/manifests/*.json). A manifest declares:

  * id, version, title, class ("general" or "health"), source ("core" or "pack")
  * auth: how the connector is authorized (oauth2 scopes, api_key, local, none) — documentation for the wizard and manual
  * tools: every model-facing tool the connector registers, each with a risk class
        read       looks something up; nothing changes anywhere
        draft      prepares something (a draft, a packet, a preview); nothing leaves the owner's accounts
        own_write  changes the owner's own data (own calendar event, task, note, memory)
        external   reaches a third party (send, call, submit, book, share, text) — only after a hash-bound owner approval
        public     publishes to an audience rather than a recipient (a post on a profile or page, a message in a workspace
                   channel) — only after a hash-bound owner approval with the publish preview (account, final text, media,
                   visibility). Added in 0.27.0: a post is not own_write (the whole internet sees it) and not external
                   (there is no single recipient to show in the approval).
        owner      an owner-only native command or a notice to the owner themselves
    plus "outbound": true when the tool's arguments may carry text to someone else (checked for health data, see
    health_taint.py), optional "destination": the argument holding the recipient (email, phone, URL), and (0.27.0):
        audience   public | workspace | person | self — who can read what leaves. Required on every outbound, external
                   and public tool. The audience decides the approval preview, the health rule and the mail copy rule
                   (AUDIENCE_RULES below): labeled health text never reaches public or workspace, whatever the grants.
        account    the argument naming which of the connector's accounts acts (required on outbound tools when the
                   connector declares "accounts": "multiple"); the broker binds it into the approval hash.
        media      the argument holding media refs (media.py); each file's sha256 is bound into the approval hash.
        rate_limit {per_hour, per_day, cooldown_s} for this tool, per account, on top of the connector's own rate_limit.
  * actions: every broker action the plugin may call on the model-facing (tools) socket. The broker refuses any other
    action there (deny by default). Owner commands use the approval socket and are not listed.
  * for class "health": min_data (what it reads, by category) and fixtures (its synthetic test environment). Health
    connectors come from the core repository only.
  * (0.27.0) accounts: "single" (default) or "multiple"; rate_limit: {per_hour, per_day, cooldown_s} per account, enforced by
    the broker where the outbound action runs; events: the inbound event kinds the connector may deliver (events.py; always
    untrusted).
  * for source "pack": sha256 of the pack directory contents; the pack is loaded only when that hash is pinned in
    packs.lock on the server (no registry, no download: a pack is a directory the owner copies in).

The broker enforces the action list; the plugins enforce the tool list (connector-gate.mjs, generated policy file).
Mode: PROCURATOR_CONNECTOR_GATE=enforce (default) | audit (log only, for an emergency rollback).

  python3 connectors.py check            validate every manifest, print a summary
  python3 connectors.py policy OUT.json  write the plugin policy file (tools -> risk, outbound, destination)
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFESTS = HERE / 'connectors'
RISKS = ('read', 'draft', 'own_write', 'external', 'public', 'owner')
AUDIENCES = ('public', 'workspace', 'person', 'self')
# One rule row per audience (0.27.0). preview: which approval the owner sees; health: what happens to labeled health text
# ('block' = refused whatever the grants, 'grant' = only to recipients a health grant allows, 'allow' = their own data);
# auto_cc: the default owner copy on outgoing mail (mail broker; the owner changes it with /mailautocc).
AUDIENCE_RULES = {
    'public': {'preview': 'publish', 'health': 'block', 'auto_cc': 'off', 'reads': 'anyone on the internet'},
    'workspace': {'preview': 'publish', 'health': 'block', 'auto_cc': 'off', 'reads': 'everyone in the workspace or channel'},
    'person': {'preview': 'send', 'health': 'grant', 'auto_cc': 'bcc', 'reads': 'the named recipients'},
    'self': {'preview': 'none', 'health': 'allow', 'auto_cc': 'off', 'reads': 'only the owner'},
}
EVENT_KINDS = ('message', 'mention', 'comment', 'reply', 'dm', 'reaction', 'device')
# Manifest v2 (0.28.0, plan v2 B1): physical-world tools and inbound declarations.
EFFECTS = ('data', 'physical')
SAFETY_TIERS = ('comfort', 'access', 'safety')
SIGNATURES = ('slack-v0', 'procurator-hmac', 'teams-jwt', 'meta-sha256')
MANIFEST_VERSION = 2
CLASSES = ('general', 'health')
SOURCES = ('core', 'pack')
AUTH_TYPES = ('oauth2', 'api_key', 'local', 'none', 'owner_signin', 'token')
BROKERS = ('web', 'mail')   # the model-facing sockets: /run/procurator-web/tools.sock, /run/procurator-mail/drafts.sock
ID = re.compile(r'^[a-z][a-z0-9-]{1,39}$')
NAME = re.compile(r'^[a-z][a-z0-9_]{1,63}$')
VERSION = re.compile(r'^\d+\.\d+\.\d+$')
HEX64 = re.compile(r'^[a-f0-9]{64}$')
MIN_DATA = ('appointments', 'medications', 'prescriptions', 'results', 'conditions', 'allergies', 'demographics', 'coverage',
            'activity', 'sleep', 'vitals', 'body', 'nutrition', 'providers', 'documents', 'messages', 'none')


class ManifestError(ValueError):
    pass


def rate_spec(value, where, what):
    """{per_hour, per_day, cooldown_s}: at least one, each a positive integer in range."""
    _need(isinstance(value, dict) and value and set(value) <= {'per_hour', 'per_day', 'cooldown_s'}, where, what + ' is {per_hour, per_day, cooldown_s}')
    for k, hi in (('per_hour', 1000), ('per_day', 10000), ('cooldown_s', 86400)):
        if k in value:
            v = value[k]
            _need(type(v) is int and 1 <= v <= hi, where, '%s.%s is a whole number from 1 to %d' % (what, k, hi))
    if 'per_hour' in value and 'per_day' in value:
        _need(value['per_hour'] <= value['per_day'], where, what + ': per_hour cannot exceed per_day')
    return value


def _need(cond, where, what):
    if not cond:
        raise ManifestError('%s: %s' % (where, what))


def validate(m, where='manifest'):
    """Returns the manifest normalized, or raises ManifestError naming the first problem."""
    _need(isinstance(m, dict), where, 'not an object')
    allowed = {'id', 'version', 'title', 'description', 'class', 'source', 'plugin', 'auth', 'tools', 'actions', 'mcp_tools',
               'min_data', 'fixtures', 'sha256', 'setup', 'notes', 'accounts', 'rate_limit', 'events', 'manifest_version', 'inbound',
               'edition'}
    extra = set(m) - allowed
    _need(not extra, where, 'unknown keys ' + ', '.join(sorted(extra)))
    _need(isinstance(m.get('id'), str) and ID.match(m['id']), where, 'id is 2-40 lowercase letters, digits or hyphens')
    where = '%s (%s)' % (where, m['id'])
    _need(isinstance(m.get('version'), str) and VERSION.match(m['version']), where, 'version is MAJOR.MINOR.PATCH')
    _need(isinstance(m.get('title'), str) and 0 < len(m['title']) <= 80, where, 'title is 1-80 characters')
    _need(m.get('class', 'general') in CLASSES, where, 'class is general or health')
    _need(m.get('source', 'core') in SOURCES, where, 'source is core or pack')
    _need(m.get('plugin') in ('procurator-web', 'procurator-mail', 'procurator-voice', 'mcp', 'broker'), where,
          'plugin is procurator-web, procurator-mail, procurator-voice, mcp or broker')
    auth = m.get('auth') or {}
    _need(isinstance(auth, dict) and auth.get('type') in AUTH_TYPES, where, 'auth.type is one of ' + ', '.join(AUTH_TYPES))
    scopes = auth.get('scopes', [])
    _need(isinstance(scopes, list) and all(isinstance(s, str) and len(s) < 200 for s in scopes), where, 'auth.scopes is a list of strings')
    _need(m.get('accounts', 'single') in ('single', 'multiple'), where, 'accounts is single or multiple')
    _need(m.get('edition', 'standard') in EDITIONS, where, 'edition is standard or x (PROCURATOR-X only)')
    if 'rate_limit' in m:
        rate_spec(m['rate_limit'], where, 'rate_limit')
    ev = m.get('events', [])
    _need(isinstance(ev, list) and all(e in EVENT_KINDS for e in ev), where, 'events lists kinds from ' + ', '.join(EVENT_KINDS))
    mv = m.get('manifest_version', 1)
    _need(mv in (1, 2), where, 'manifest_version is 1 or 2')
    if 'inbound' in m:
        ib = m['inbound']
        _need(isinstance(ib, dict) and set(ib) <= {'types', 'signature', 'headers'}, where, 'inbound is {types, signature, headers}')
        _need(isinstance(ib.get('types'), list) and ib['types'] and all(t in EVENT_KINDS for t in ib['types']), where, 'inbound.types lists event kinds')
        _need(ib.get('signature') in SIGNATURES, where, 'inbound.signature is one of ' + ', '.join(SIGNATURES))
        _need(isinstance(ib.get('headers', {}), dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in ib.get('headers', {}).items()), where, 'inbound.headers maps roles to header names')
    tools = m.get('tools') or {}
    _need(isinstance(tools, dict), where, 'tools is an object')
    for name, spec in tools.items():
        _need(isinstance(name, str) and NAME.match(name), where, 'tool name %r is invalid' % name)
        _need(isinstance(spec, dict) and spec.get('risk') in RISKS, where, 'tool %s: risk is one of %s' % (name, ', '.join(RISKS)))
        _need(set(spec) <= {'risk', 'outbound', 'destination', 'note', 'audience', 'account', 'media', 'rate_limit', 'effect', 'safety_tier', 'bounds', 'confirm'},
              where, 'tool %s: unknown keys' % name)
        _need(isinstance(spec.get('outbound', False), bool), where, 'tool %s: outbound is true or false' % name)
        if mv == 1 and 'audience' not in spec:
            # v1 manifests (packs written before 0.27.0) are read with defaults; v2 must say it.
            if spec['risk'] == 'external':
                spec['audience'] = 'person'
            elif spec['risk'] == 'own_write' and spec.get('outbound'):
                spec['audience'] = 'self'
        risk, aud = spec['risk'], spec.get('audience')
        eff = spec.get('effect', 'data')
        _need(eff in EFFECTS, where, 'tool %s: effect is data or physical' % name)
        if eff == 'physical':
            _need(spec.get('safety_tier') in SAFETY_TIERS, where, 'tool %s: a physical tool declares safety_tier (comfort, access, safety)' % name)
            _need(spec.get('confirm') == 'readback', where, 'tool %s: a physical tool declares confirm: readback' % name)
            _need(risk in ('own_write', 'external'), where, 'tool %s: a physical tool is own_write or external' % name)
        else:
            _need('safety_tier' not in spec, where, 'tool %s: safety_tier is only for physical tools' % name)
        if 'confirm' in spec:
            _need(spec['confirm'] == 'readback', where, 'tool %s: confirm is readback' % name)
        if 'bounds' in spec:
            b = spec['bounds']
            _need(isinstance(b, dict) and b, where, 'tool %s: bounds maps parameters to limits' % name)
            for param, lim in b.items():
                _need(NAME.match(param) and isinstance(lim, dict) and lim and set(lim) <= {'min', 'max', 'enum', 'window'}, where,
                      'tool %s: bounds.%s is {min, max, enum, window}' % (name, param))
                if 'min' in lim or 'max' in lim:
                    _need(all(isinstance(lim.get(k, 0), (int, float)) and not isinstance(lim.get(k, 0), bool) for k in ('min', 'max')), where, 'tool %s: bounds.%s min/max are numbers' % (name, param))
                    if 'min' in lim and 'max' in lim:
                        _need(lim['min'] <= lim['max'], where, 'tool %s: bounds.%s min exceeds max' % (name, param))
                if 'enum' in lim:
                    _need(isinstance(lim['enum'], list) and lim['enum'], where, 'tool %s: bounds.%s enum is a list' % (name, param))
                if 'window' in lim:
                    _need(isinstance(lim['window'], str) and re.fullmatch(r'\d{2}:\d{2}-\d{2}:\d{2}', lim['window']), where, 'tool %s: bounds.%s window is HH:MM-HH:MM' % (name, param))
        if m.get('class') == 'health':
            _need(aud not in ('public', 'workspace'), where, 'tool %s: a health connector cannot reach a public or workspace audience' % name)
        if spec.get('outbound'):
            _need(risk in ('draft', 'external', 'own_write', 'public'), where, 'tool %s: an outbound tool is draft, own_write, external or public' % name)
        if spec.get('outbound') or risk in ('external', 'public'):
            _need(aud in AUDIENCES, where, 'tool %s: an outbound, external or public tool declares audience (%s)' % (name, ', '.join(AUDIENCES)))
        elif aud is not None:
            _need(aud in AUDIENCES, where, 'tool %s: audience is one of %s' % (name, ', '.join(AUDIENCES)))
        if risk == 'public':
            _need(aud in ('public', 'workspace'), where, 'tool %s: a public tool has audience public or workspace' % name)
        if aud in ('public', 'workspace'):
            _need(risk in ('public', 'draft', 'owner', 'read'), where, 'tool %s: a %s audience is reached only by a public tool (or a draft of one)' % (name, aud))
        if risk == 'external':
            _need(aud == 'person', where, 'tool %s: an external tool has audience person (use public for posts and channels)' % name)
        if risk == 'own_write' and aud is not None:
            _need(aud in ('self', 'person'), where, 'tool %s: an own_write tool has audience self or person' % name)
        for k in ('destination', 'account', 'media'):
            if k in spec:
                _need(isinstance(spec[k], str) and NAME.match(spec[k]), where, 'tool %s: %s names an argument' % (name, k))
        if m.get('accounts') == 'multiple' and spec.get('outbound'):
            _need('account' in spec, where, 'tool %s: the connector has several accounts, so this tool names its account argument' % name)
        if 'rate_limit' in spec:
            rate_spec(spec['rate_limit'], where, 'tool %s rate_limit' % name)
    mcp = m.get('mcp_tools') or {}
    _need(isinstance(mcp, dict), where, 'mcp_tools is an object')
    for name, spec in mcp.items():
        _need(isinstance(name, str) and NAME.match(name) and isinstance(spec, dict) and spec.get('risk') in RISKS, where, 'mcp tool %s needs a risk' % name)
    actions = m.get('actions') or {}
    _need(isinstance(actions, dict) and set(actions) <= set(BROKERS), where, 'actions maps a broker (%s) to its action names' % ', '.join(BROKERS))
    for b, names in actions.items():
        _need(isinstance(names, list) and all(isinstance(a, str) and NAME.match(a) for a in names), where, 'actions.%s is a list of action names' % b)
        _need(len(set(names)) == len(names), where, 'an action is listed twice in actions.%s' % b)
    if m.get('class') == 'health':
        md = m.get('min_data')
        _need(isinstance(md, list) and md and all(x in MIN_DATA for x in md), where, 'a health connector lists min_data from ' + ', '.join(MIN_DATA))
        fx = m.get('fixtures')
        _need(isinstance(fx, list) and fx and all(isinstance(x, str) and x for x in fx), where, 'a health connector names its synthetic fixtures')
        _need(m.get('source', 'core') == 'core', where, 'health connectors come from the core repository only')
    if m.get('source') == 'pack':
        _need(isinstance(m.get('sha256'), str) and HEX64.match(m['sha256']), where, 'a pack carries the sha256 of its directory')
        for name, spec in tools.items():
            # A pack may not declare a tool that bypasses approval: anything reaching third parties is external.
            _need(spec['risk'] != 'owner', where, 'tool %s: packs cannot register owner commands' % name)
    return {**m, 'class': m.get('class', 'general'), 'source': m.get('source', 'core'), 'tools': tools, 'actions': actions, 'mcp_tools': mcp,
            'accounts': m.get('accounts', 'single'), 'events': ev}


def tree_hash(directory):
    """sha256 over the pack directory: sorted relative paths and file bytes (manifest's own sha256 field excluded)."""
    h = hashlib.sha256()
    root = Path(directory)
    for p in sorted(x for x in root.rglob('*') if x.is_file()):
        rel = p.relative_to(root).as_posix()
        if '__pycache__' in rel:
            continue
        data = p.read_bytes()
        if rel == 'manifest.json':
            try:
                d = json.loads(data)
                d.pop('sha256', None)
                data = json.dumps(d, sort_keys=True, separators=(',', ':')).encode()
            except ValueError:
                pass
        h.update(rel.encode() + b'\0' + hashlib.sha256(data).digest())
    return h.hexdigest()


EDITIONS = ('standard', 'x')
INSTANCE_ENV = Path(os.environ.get('PROCURATOR_INSTANCE_ENV', '/etc/procurator/instance.env'))


def instance_edition(path=INSTANCE_ENV):
    """The box edition: PROCURATOR (standard) or PROCURATOR-X (x). PROCURATOR_EDITION in the environment wins; otherwise the
    EDITION line bin/install.sh wrote to /etc/procurator/instance.env; otherwise standard. A connector marked "edition": "x"
    (the coding orchestrator: builds) is absent from a standard box: its actions are refused by the broker and its tools by the
    plugin gate, so the model never gets a half-working tool."""
    env = os.environ.get('PROCURATOR_EDITION', '').strip().lower()
    if env in EDITIONS:
        return env
    try:
        for line in Path(path).read_text().splitlines():
            k, _, v = line.partition('=')
            if k.strip() == 'EDITION' and v.strip().strip('"').lower() in EDITIONS:
                return v.strip().strip('"').lower()
    except OSError:
        pass
    return 'standard'


class Registry:
    def __init__(self, manifests=MANIFESTS, packs=None, lock=None, mode=None, edition=None):
        self.mode = mode or os.environ.get('PROCURATOR_CONNECTOR_GATE', 'enforce')
        if self.mode not in ('enforce', 'audit'):
            self.mode = 'enforce'
        self.edition = edition if edition in EDITIONS else instance_edition()
        self.connectors, self.problems, self.refused_log = {}, [], []
        for path in sorted(Path(manifests).glob('*.json')):
            try:
                m = validate(json.loads(path.read_text()), path.name)
            except (ValueError, OSError) as e:
                self.problems.append(str(e))
                continue
            if m['source'] != 'core':
                self.problems.append('%s: a pack must be installed under packs/, not in the core manifests' % path.name)
                continue
            self._add(m)
        if packs:
            pinned = set()
            try:
                pinned = {line.split()[0] for line in Path(lock).read_text().splitlines() if line.strip() and not line.startswith('#')}
            except (OSError, TypeError, IndexError):
                pinned = set()
            for d in sorted(p for p in Path(packs).iterdir() if p.is_dir()) if Path(packs).is_dir() else []:
                try:
                    m = validate(json.loads((d / 'manifest.json').read_text()), d.name)
                    _need(m['source'] == 'pack', d.name, 'source must be pack')
                    actual = tree_hash(d)
                    _need(actual == m['sha256'], d.name, 'contents do not match the manifest sha256')
                    _need(actual in pinned, d.name, 'not pinned in packs.lock (add its sha256 there to allow it)')
                    self._add(m)
                except (ValueError, OSError) as e:
                    self.problems.append(str(e))

    def _add(self, m):
        if m['id'] in self.connectors:
            self.problems.append('%s: declared twice' % m['id'])
            return
        for other in self.connectors.values():
            dup = (set(other['tools']) | set(other['mcp_tools'])) & (set(m['tools']) | set(m['mcp_tools']))
            if dup:
                self.problems.append('%s and %s both declare tool %s' % (other['id'], m['id'], sorted(dup)[0]))
                return
        self.connectors[m['id']] = m

    def active(self):
        """Connectors this edition offers: everything, minus "edition": "x" manifests on a standard box."""
        return [m for m in self.connectors.values() if m.get('edition', 'standard') == 'standard' or self.edition == 'x']

    def actions(self, broker='web'):
        return {a for m in self.active() for a in m['actions'].get(broker, [])}

    def tools(self):
        return {name: {**spec, 'connector': m['id'], 'class': m['class'], 'edition': m.get('edition', 'standard')}
                for m in self.connectors.values() for name, spec in m['tools'].items()}

    def gate(self, action, privileged, broker='web'):
        """None if allowed; otherwise the refusal text. The approval socket (owner commands) is not gated here."""
        if privileged:
            return None
        if isinstance(action, str) and action in self.actions(broker):
            return None
        msg = ('Action %r is not declared by any connector manifest, so the broker refuses it (deny by default). '
               'Nothing was done.' % (action if isinstance(action, str) else '?'))
        self.refused_log = (self.refused_log + [str(action)[:64]])[-50:]
        if self.mode == 'audit':
            print('[connectors] audit: undeclared action ' + str(action)[:64], file=sys.stderr, flush=True)
            return None
        return msg

    def tool(self, name):
        return self.tools().get(name)

    def limits(self, connector_id, tool=None):
        """The rate limits that apply to one outbound action: [(scope, spec)] — the connector's, then the tool's."""
        m = self.connectors.get(connector_id)
        if not m:
            return []
        out = [('connector', m['rate_limit'])] if m.get('rate_limit') else []
        spec = m['tools'].get(tool) if tool else None
        if spec and spec.get('rate_limit'):
            out.append(('tool', spec['rate_limit']))
        return out

    def policy(self):
        """The plugin-side policy: tool -> risk/outbound/destination/audience/...; the gate refuses tools missing from it."""
        keep = ('risk', 'outbound', 'destination', 'class', 'connector', 'audience', 'account', 'media', 'effect', 'safety_tier', 'bounds', 'confirm',
                'edition')
        # The policy file is the same for both editions (it is committed); the plugin gate reads PROCURATOR_EDITION at run time
        # and refuses 'edition': 'x' tools on a standard box.
        return {'version': 3, 'manifest_version': MANIFEST_VERSION, 'mode': self.mode, 'audiences': AUDIENCE_RULES,
                'tools': {n: {k: v for k, v in s.items() if k in keep and not (k == 'edition' and v == 'standard')} for n, s in sorted(self.tools().items())}}

    def summary(self):
        out = []
        for m in sorted(self.connectors.values(), key=lambda x: x['id']):
            risks = {}
            for s in m['tools'].values():
                risks[s['risk']] = risks.get(s['risk'], 0) + 1
            out.append({'id': m['id'], 'title': m['title'], 'version': m['version'], 'class': m['class'], 'source': m['source'],
                        'plugin': m['plugin'], 'auth': m['auth'].get('type'), 'tools': len(m['tools']), 'risks': risks,
                        'external_tools': sorted(n for n, s in m['tools'].items() if s['risk'] == 'external'),
                        'public_tools': sorted(n for n, s in m['tools'].items() if s['risk'] == 'public'),
                        'accounts': m.get('accounts', 'single'), 'rate_limit': m.get('rate_limit'), 'events': m.get('events', []),
                        'manifest_version': m.get('manifest_version', 1),
                        'physical_tools': sorted(n for n, s in m['tools'].items() if s.get('effect') == 'physical'),
                        'setup': m.get('setup', '')})
        return {'connectors': out, 'problems': self.problems, 'mode': self.mode, 'refused_recent': self.refused_log[-10:]}


def main(argv):
    r = Registry()
    if len(argv) > 1 and argv[1] == 'policy':
        Path(argv[2]).write_text(json.dumps(r.policy(), indent=1, sort_keys=True) + '\n')
        return 0 if not r.problems else 1
    s = r.summary()
    for c in s['connectors']:
        print('%-22s %-6s %-5s %3d tools %s' % (c['id'], c['class'], c['source'], c['tools'], c['risks']))
    for p in s['problems']:
        print('PROBLEM: ' + p)
    return 1 if s['problems'] else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
