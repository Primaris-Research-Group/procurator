"""Home browser lane (web 0.19.0, PROCURATOR browsing plan Phase 5).

Two lanes run the same Node driver and the same gates:
- cloud: Browserbase (as before),
- home:  real Chrome on the owner's always-on PC, run by browser-worker 1.0 under WSL2, on their home internet with one
         persistent Chrome profile shared by all tasks (0.20.3; /homeprofile separate: one per site). The PC PULLS jobs over the existing SSH relay (build-relay.py --browser NAME);
         the droplet never connects to the PC. Sign-in to a home profile is done at the PC itself (/homesignin SITE,
         then /homeready SITE); no password ever crosses the relay.

The broker writes one job file per driver operation to /var/lib/procurator-web/lanes/<machine>/jobs/ and waits for the
result. A job the PC has not claimed when the wait runs out is withdrawn (it can never run late); one it claimed but did
not answer counts as unconfirmed, exactly like a cloud browser that stops answering mid-action.

Lane choice per website (owner setting site_lane: home | cloud | either; /weblane): the defaults prefer home for
.gov, insurers (leaveco.example), pharmacies (cvs.com), utilities, walmart.com and target.com and fall back to cloud when the PC
is offline; "home" means home only.
"""
import base64
import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path

ONLINE_MS = 90_000
HOME_DEFAULT = re.compile(r'(?:\.gov$|(?:^|\.)leaveco\.example$|(?:^|\.)cvs\.com$|(?:^|\.)walmart\.com$|(?:^|\.)target\.com$|power|energy|electric|utilit|water|gasco|\bgas\b)', re.I)
PROFILE = re.compile(r'[^a-z0-9.-]+')
# 0.20.3 (Nov 6, "the bot always opens a browser with no history"): by default every home task now uses ONE persistent
# Chrome profile on the PC, so history, cache, cookies and every site the owner signed in to carry over between tasks and sites.
# /homeprofile separate restores the old one-profile-per-site isolation. Signing in is still their: the gates refuse
# password, code and sign-in controls whatever the profile.
SHARED_PROFILE = 'procurator'
SIGNING_WINDOW_S = 15 * 60

def profile_name(name):
    return PROFILE.sub('-', (name or 'default').lower()).strip('-.')[:60] or 'default'

OFFLINE_GIVE_UP_S = 30 * 60

class Lanes:
    def __init__(self, broker, state, poll=0.2, sleep=time.sleep, now=time.time):
        self.b, self.root = broker, Path(state) / 'lanes'
        self.poll, self.sleep, self.now = poll, sleep, now
        self.started = {}
        self.signing = {}      # site -> (time, profile): a sign-in window the owner may still be using on the PC

    # ---------- machines ----------
    def machines(self):
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            try:
                h = json.loads((d / 'hello.json').read_text())
            except (OSError, ValueError):
                continue
            seen = max(h.get('seen_at') or 0, self._alive(d))
            out.append({**h, 'online': self.now() * 1000 - seen < ONLINE_MS, 'last_seen_s': int(self.now() - seen / 1000)})
        return out

    def _alive(self, d):
        try:
            return json.loads((d / 'alive.json').read_text()).get('at') or 0
        except (OSError, ValueError):
            return 0

    def online_machine(self):
        on = [m for m in self.machines() if m['online']]
        return (sorted(on, key=lambda m: m['name'] != 'home')[0] if on else None)

    # ---------- lane choice ----------
    def rules(self):
        with self.b.db() as db:
            r = db.execute("SELECT value FROM owner_settings WHERE key='site_lane'").fetchone()
        return json.loads(r['value']) if r else {}

    def _setting(self, key, default):
        with self.b.db() as db:
            r = db.execute('SELECT value FROM owner_settings WHERE key=?', (key,)).fetchone()
        return json.loads(r['value']) if r else default

    def _put(self, key, value):
        with self.b.db() as db:
            db.execute('INSERT OR REPLACE INTO owner_settings VALUES(?,?)', (key, json.dumps(value)))

    # ---------- home profile (0.20.3) ----------
    def profile_mode(self):
        return 'separate' if self._setting('home_profile', 'shared') == 'separate' else 'shared'

    def set_profile_mode(self, mode):
        if mode not in ('shared', 'separate'):
            raise ValueError('Use /homeprofile shared or /homeprofile separate.')
        self._put('home_profile', mode)
        return self.profile_status()

    def shared_ready(self):
        return set(self._setting('home_shared_ready', []))

    def profile_status(self):
        return {'mode': self.profile_mode(), 'profile': SHARED_PROFILE if self.profile_mode() == 'shared' else 'one per site',
                'signed_in_sites': sorted(self.shared_ready())}

    @staticmethod
    def _legacy_ready(machine, site):
        return any(p['name'] == profile_name(site) and p['ready'] for p in (machine or {}).get('profiles', []))

    def site_ready(self, machine, site):
        if self.profile_mode() == 'shared' and site in self.shared_ready():
            return True
        return self._legacy_ready(machine, site)    # a site signed in before 0.20.3 keeps its own profile until the owner signs in again

    def ready_online(self, site, origin=None):
        """0.20.3 (Nov 6 hotels.com): the owner signed this saved login in at the PC (/homeready) and the PC is online now, and the owner
        did not set its website to the cloud with /weblane (their rule wins)."""
        if origin:
            host = urllib.parse.urlsplit(origin).hostname or ''
            rule = self.rule_for(host[4:] if host.startswith('www.') else host)
            if rule['source'] == 'owner' and rule['lane'] == 'cloud':
                return False
        m = self.online_machine()
        return bool(m and site and self.site_ready(m, site))

    def home_profile(self, site=None, machine=None, host=None):
        """The Chrome profile a home browser uses. Shared mode: the one PROCURATOR profile, except a site still signed in
        only in its own pre-0.20.3 profile. Separate mode: one profile per saved login, or per website."""
        if self.profile_mode() == 'separate':
            return profile_name(site or host)
        if site and site not in self.shared_ready():
            m = machine if isinstance(machine, dict) else next((x for x in self.machines() if x['name'] == machine), None)
            if self._legacy_ready(m, site):
                return profile_name(site)
        return SHARED_PROFILE

    def signing_conflict(self, profile):
        """A sign-in window the owner opened in this profile in the last 15 minutes and has not confirmed with /homeready."""
        for site, (at, prof) in list(self.signing.items()):
            if self.now() - at > SIGNING_WINDOW_S:
                self.signing.pop(site, None)
            elif prof == profile:
                return site
        return None

    def set_rule(self, domain, lane):
        rules = self.rules()
        if lane == 'default':
            rules.pop(domain, None)
        elif lane in ('home', 'cloud', 'either'):
            rules[domain] = lane
        else:
            raise ValueError('Use home, cloud, either or default.')
        with self.b.db() as db:
            db.execute("INSERT OR REPLACE INTO owner_settings VALUES('site_lane',?)", (json.dumps(rules),))
        return self.rule_for(domain)

    def rule_for(self, domain):
        # 0.19.2 (Nov 6 compat run): a rule for wikipedia.org also covers en.wikipedia.org; the most specific rule wins.
        rules, parts = self.rules(), (domain or '').split('.')
        for i in range(0, max(1, len(parts) - 1)):
            cand = '.'.join(parts[i:])
            if cand in rules:
                return {'domain': domain, 'lane': rules[cand], 'source': 'owner', 'rule': cand}
        return {'domain': domain, 'lane': 'prefer_home' if HOME_DEFAULT.search(domain or '') else 'cloud', 'source': 'default'}

    def wants_home(self, domain):
        """Nov 6 (Leaveco): would a browser for this site run on the PC right now? home rule: yes; either/default-home: when the PC
        is online; cloud: no. Used before reusing a cloud browser and before sending a cloud sign-in link."""
        lane = self.rule_for(domain)['lane']
        if lane == 'home':
            return True
        if lane in ('either', 'prefer_home'):
            return bool(self.online_machine())
        return False

    def choose(self, origin, practice=False, service=None, site=None, prefer=None):
        """('home', machine, reason) or ('cloud', None, reason). Raises when the owner set home-only and the PC is offline.
        prefer ('home' | 'cloud', Nov 6): the owner asked in chat for their PC (or the cloud) for this browser. It wins over the built-in
        defaults but never over a rule the owner set with /weblane; then the reason says how the owner can change it."""
        if practice or service:
            if prefer == 'home':
                raise ValueError('Practice and connected-project browsers always run in the cloud.')
            return 'cloud', None, 'practice and project browsers run in the cloud'
        host = urllib.parse.urlsplit(origin or '').hostname or ''
        domain = host[4:] if host.startswith('www.') else host
        rule = self.rule_for(domain)
        if prefer == 'cloud':
            if rule['lane'] == 'home' and rule['source'] == 'owner':
                raise ValueError('The owner set %s to their PC only (/weblane). It opens in the cloud only if the owner changes that: /weblane %s cloud (or either).' % (rule.get('rule') or domain, rule.get('rule') or domain))
            return 'cloud', None, 'cloud browser (the owner asked for the cloud)'
        if prefer == 'home':
            if rule['lane'] == 'cloud' and rule['source'] == 'owner':
                raise ValueError('The owner set %s to the cloud browser (/weblane). It opens on their PC only if the owner changes that: /weblane %s home (or either).' % (rule.get('rule') or domain, rule.get('rule') or domain))
            m = self.online_machine()
            if not m:
                raise ValueError('Their PC is offline (last check-in %s), so the home browser cannot open. Ask them to wake the PC, or offer the cloud browser.' % self.last_seen_text())
            if site:
                if not self.site_ready(m, site):
                    raise ValueError('%s is not signed in on their PC yet. Call web_home_signin site=%s: a Chrome window opens on the PC for them to sign in; after the owner sends /homeready %s, open it again.' % (site, site, site))
            return 'home', m['name'], 'home browser on %s (the owner asked for their PC)' % (m.get('label') or m['name'])
        if rule['lane'] == 'cloud':
            return 'cloud', None, 'cloud browser for ' + domain
        m = self.online_machine()
        if m and site:
            # A saved login opens at home only when the owner signed that profile in at the PC (/homeready).
            if not self.site_ready(m, site):
                if rule['lane'] == 'home':
                    raise ValueError('%s is set to the home browser, but its profile on the PC is not signed in yet. Ask them to run /homesignin %s, sign in at the PC, then /homeready %s.' % (domain, site, site))
                return 'cloud', None, 'home profile for %s is not signed in yet; using the cloud login' % site
        if m:
            return 'home', m['name'], 'home browser on %s (%s)' % (m.get('label') or m['name'], 'set by them' if rule['source'] == 'owner' else 'default for this kind of site')
        if rule['lane'] == 'home':
            raise ValueError('%s is set to the home browser only, and the PC is offline (last check-in %s). Ask them to wake the PC, or switch it with /weblane %s either.' % (
                domain, self.last_seen_text(), domain))
        return 'cloud', None, 'the home browser is offline; cloud browser instead'

    def last_seen_text(self):
        ms = self.machines()
        return ', '.join('%s %d min ago' % (m['name'], m['last_seen_s'] // 60) for m in ms) or 'never'

    # ---------- jobs ----------
    def _dirs(self, machine):
        base = self.root / machine
        for sub in ('jobs', 'claims', 'results'):
            (base / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        return base

    @staticmethod
    def _result(path):
        """The PC's answer, or None while it is not (completely) there. 0.20.4: a half-written file is waited for, not an error."""
        try:
            return json.loads(path.read_text()).get('stdout') or ''
        except (OSError, ValueError):
            return None

    def submit(self, machine, op, payload, timeout=70, grace=20):
        """Write the job, wait for its result. Returns (stdout, state) with state ok | withdrawn | lost."""
        base = self._dirs(machine)
        jid = uuid.uuid4().hex[:24]
        job = {'id': jid, 'op': op, 'created_at': int(self.now() * 1000), **payload}
        tmp = base / 'jobs' / (jid + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(job, f)
        os.replace(tmp, base / 'jobs' / (jid + '.json'))
        result = base / 'results' / (jid + '.json')
        claim = base / 'claims' / (jid + '.json')
        try:
            end = self.now() + timeout
            while self.now() < end:
                got = self._result(result) if result.exists() else None
                if got is not None:
                    return got, 'ok'
                self.sleep(self.poll)
            # Not claimed yet: withdraw it so it can never run late (the relay claims with an exclusive create too).
            try:
                fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b'{"by":"broker-withdrawn"}')
                os.close(fd)
                return '', 'withdrawn'
            except FileExistsError:
                pass
            end = self.now() + grace
            while self.now() < end:
                got = self._result(result) if result.exists() else None
                if got is not None:
                    return got, 'ok'
                self.sleep(self.poll)
            return '', 'lost'
        finally:
            for p in (base / 'jobs' / (jid + '.json'), result):
                try:
                    p.unlink()
                except OSError:
                    pass

    @staticmethod
    def parts(provider_id):
        m = re.fullmatch(r'home:([a-z0-9-]{1,20}):([a-f0-9]{16})', provider_id or '')
        if not m:
            raise ValueError('Not a home browser.')
        return m.group(1), m.group(2)

    def profile_for(self, s):
        host = urllib.parse.urlsplit(s.get('origin') or '').hostname or 'default'
        machine = self.parts(s['provider_id'])[0] if str(s.get('provider_id') or '').startswith('home:') else None
        return self.home_profile(s.get('site_login'), machine, host[4:] if host.startswith('www.') else host)

    def start(self, machine, session_id, profile, url=None):
        from broker import CreationRejected
        # Remember when it started: until the PC's next heartbeat lists it, its absence there proves nothing.
        self.started[session_id] = self.now() * 1000
        out, state = self.submit(machine, 'start', {'session_id': session_id, 'profile': profile, 'url': url}, timeout=45)
        try:
            r = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            r = {}
        if state == 'withdrawn':
            # 0.19.1 (Nov 6 QA): the PC never took the job, so nothing started: a clean failure, not "uncertain".
            raise CreationRejected('The home browser PC did not pick up the request (it may be asleep or its worker stopped); nothing was started. '
                                   'Check /weblane, or ask them to switch this site with /weblane SITE cloud.')
        if state == 'ok' and not r.get('ok'):
            raise CreationRejected('The home browser refused to start: %s' % str(r.get('error') or 'no reason given')[:200])
        if state != 'ok':
            raise RuntimeError('home browser start unconfirmed (the PC took the job but did not answer)')
        return {'id': 'home:%s:%s' % (machine, session_id)}

    def run(self, s, operation, **kwargs):
        from broker import driver_result
        machine, sid = self.parts(s['provider_id'])
        payload = {'operation': operation, 'practice': bool(s['practice']), 'url': s['url'], 'origin': s['origin'],
                   **({'site_domain': s['site_domain']} if s.get('site_domain') else {}), **kwargs}
        if operation == 'act':
            steps = []
            for st in kwargs.get('steps') or []:
                if st.get('kind') == 'upload' and st.get('file_path'):
                    st = {**st, 'file_b64': base64.b64encode(Path(st['file_path']).read_bytes()).decode()}
                steps.append(st)
            payload['steps'] = steps
        out, state = self.submit(machine, 'driver', {'session_id': sid, 'profile': self.profile_for(s), 'input': payload})
        n = len(kwargs.get('steps', []))
        if state == 'withdrawn':
            # The PC never took it: nothing happened on the page.
            return {'ok': False, 'kind': 'not_dispatched', 'error_stage': 'connect', 'error_type': 'home_offline'} if operation == 'act' else {'ok': False, 'kind': 'home_offline'}
        return driver_result(out, operation, n, timed_out=state == 'lost')

    def inspect(self, provider_id):
        machine, sid = self.parts(provider_id)
        m = next((x for x in self.machines() if x['name'] == machine), None)
        if not m:
            return {'id': provider_id, 'status': 'COMPLETED'}   # the machine was removed: nothing can be running there
        if not m['online']:
            # Offline for a while (30 min): its browser cannot be driven any more; free the slot. Shorter: unknown, left as is.
            if m.get('last_seen_s', 0) > OFFLINE_GIVE_UP_S:
                return {'id': provider_id, 'status': 'COMPLETED'}
            raise TimeoutError('home browser offline')
        if sid in (m.get('sessions') or []):
            return {'id': provider_id, 'status': 'RUNNING'}
        if (m.get('seen_at') or 0) <= self.started.get(sid, 0):
            raise TimeoutError('no heartbeat from the PC since this browser started')   # unknown: left as it is
        return {'id': provider_id, 'status': 'COMPLETED'}

    def close(self, provider_id):
        machine, sid = self.parts(provider_id)
        self.submit(machine, 'close', {'session_id': sid}, timeout=15, grace=5)
        return {}

    def signin(self, site, url):
        m = self.online_machine()
        if not m:
            raise ValueError('The home browser PC is offline (last check-in %s).' % self.last_seen_text())
        profile = SHARED_PROFILE if self.profile_mode() == 'shared' else profile_name(site)
        out, state = self.submit(m['name'], 'signin', {'profile': profile, 'url': url}, timeout=45)
        if state != 'ok':
            raise ValueError('The PC did not answer; it may be asleep.')
        self.signing[site] = (self.now(), profile)
        return {'machine': m['name'], 'profile': profile, 'headed': m.get('headed'), 'shared': profile == SHARED_PROFILE}

    def mark_ready(self, site, ready=True):
        m = self.online_machine()
        if not m:
            raise ValueError('The home browser PC is offline.')
        if self.profile_mode() == 'shared':
            # The PC closes the sign-in window; which sites are signed in to the shared profile is kept here.
            out, state = self.submit(m['name'], 'profile', {'profile': SHARED_PROFILE, 'ready': True}, timeout=30)
            if state != 'ok':
                raise ValueError('The PC did not answer; it may be asleep.')
            done = self.shared_ready()
            (done.add if ready else done.discard)(site)
            self._put('home_shared_ready', sorted(done))
            if not ready and self._legacy_ready(m, site):
                self.submit(m['name'], 'profile', {'profile': profile_name(site), 'ready': False}, timeout=30)
            profile = SHARED_PROFILE
        else:
            out, state = self.submit(m['name'], 'profile', {'profile': profile_name(site), 'ready': bool(ready)}, timeout=30)
            if state != 'ok':
                raise ValueError('The PC did not answer; it may be asleep.')
            profile = profile_name(site)
        self.signing.pop(site, None)
        return {'machine': m['name'], 'site': site, 'profile': profile, 'ready': bool(ready)}

class LaneProvider:
    """Wraps the cloud provider: sessions whose provider id starts with home: go to the PC, everything else to Browserbase."""
    def __init__(self, inner, lanes):
        object.__setattr__(self, 'inner', inner)
        object.__setattr__(self, 'lanes', lanes)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __setattr__(self, name, value):
        setattr(self.inner, name, value)

    def run(self, session, operation, **kwargs):
        if str(session.get('provider_id') or '').startswith('home:'):
            return self.lanes.run(session, operation, **kwargs)
        return self.inner.run(session, operation, **kwargs)

    def inspect(self, identifier):
        if str(identifier or '').startswith('home:'):
            return self.lanes.inspect(identifier)
        return self.inner.inspect(identifier)

    def close(self, identifier):
        if str(identifier or '').startswith('home:'):
            return self.lanes.close(identifier)
        return self.inner.close(identifier)

    def api(self, method, path, *args, **kwargs):
        if 'sessions/home:' in str(path):
            raise ValueError('This browser runs on the PC at home: there is no web live view. Use the PC itself (or remote desktop to it); the Chrome window is titled with the site.')
        return self.inner.api(method, path, *args, **kwargs)
