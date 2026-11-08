#!/usr/bin/env python3
"""PROCURATOR home browser worker 1.0 (web 0.19.0, browsing plan Phase 5). Runs on the owner's PC inside Ubuntu (WSL2).

It drives a real Google Chrome on this PC (on their home internet) for the assistant's browser tasks:
- persistent Chrome profiles under ~/Procurator/browser/profiles/<name> (sign-ins stay on this PC); since 1.1.0 (web 0.20.3)
  the droplet normally asks for one shared profile, "procurator", so history, cache and sign-ins carry over between tasks,
  session cookies are kept when Chrome closes ("continue where you left off") and Chrome is shut down cleanly,
- the same Node driver (runner.cjs) and the same broker gates as the cloud browser,
- it PULLS jobs from the droplet over SSH with a restricted key that can only run the relay in browser mode
  (bhello / bpull / bresult); the droplet never connects to this PC and no port is opened here,
- it refuses the never-browsed sites (banks, brokerages, payment apps) itself, before any navigation.

Commands:
  python3 browser_worker.py run        the loop (started by the Windows scheduled task "PROCURATOR browser")
  python3 browser_worker.py check      configuration, Chrome, Node, relay reachability
  python3 browser_worker.py profiles   list site profiles and whether they are marked signed in
"""
import base64, json, os, re, shutil, signal, socket, subprocess, sys, time, urllib.parse, urllib.request

VERSION = '1.1.2'
BASE = os.path.expanduser(os.environ.get('PROCURATOR_BROWSER_HOME', '~/Procurator/browser'))
CONFIG = os.path.join(BASE, 'config.json')
INBOX = os.path.join(BASE, 'inbox')
PROFILES = os.path.join(BASE, 'profiles')
DROPLET_INBOX = '/run/procurator-web/inbox/'
NEVER = ['ally.com', 'americanexpress.com', 'bankofamerica.com', 'barclaysus.com', 'binance.us', 'bmo.com', 'capitalone.com', 'cash.app', 'chase.com', 'chime.com', 'citi.com', 'citibank.com', 'citizensbank.com', 'coinbase.com', 'discover.com', 'etrade.com', 'fidelity.com', 'firstcitizens.com', 'goldmansachs.com', 'huntington.com', 'jpmorganchase.com', 'key.com', 'kraken.com', 'marcus.com', 'morganstanley.com', 'mtb.com', 'mysynchrony.com', 'navyfederal.org', 'paypal.com', 'penfed.org', 'pnc.com', 'regions.com', 'robinhood.com', 'santanderbank.com', 'schwab.com', 'sofi.com', 'synchrony.com', 'td.com', 'tdbank.com', 'truist.com', 'usaa.com', 'usbank.com', 'vanguard.com', 'venmo.com', 'wellsfargo.com', 'zellepay.com']
PROFILE = re.compile(r'^[a-z0-9][a-z0-9.-]{0,60}$')
SID = re.compile(r'^[a-f0-9]{16}$')
DRIVER_OPS = ('init', 'snapshot', 'act', 'look', 'point', 'capture', 'screenshot', 'page_tool', 'read_url')

def log(msg):
    os.makedirs(os.path.join(BASE, 'logs'), exist_ok=True)
    with open(os.path.join(BASE, 'logs', 'worker.log'), 'a') as f:
        f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')

def load_config():
    with open(CONFIG) as f:
        c = json.load(f)
    c.setdefault('profiles', {})
    return c

def save_config(c):
    tmp = CONFIG + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(c, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)

def never_host(url):
    host = (urllib.parse.urlsplit(url or '').hostname or '').lower()
    return any(host == d or host.endswith('.' + d) for d in NEVER)

def relay(cfg, command, data=None, timeout=60):
    p = subprocess.run(list(cfg['relay']) + command.split(), input=json.dumps(data or {}), capture_output=True, text=True, timeout=timeout)
    line = (p.stdout.strip().splitlines() or [''])[-1]
    try:
        out = json.loads(line)
    except ValueError:
        raise RuntimeError('droplet relay did not answer (%s): %s' % (p.returncode, (p.stderr or p.stdout).strip()[-300:]))
    if not out.get('ok'):
        raise RuntimeError('droplet relay refused: %s' % out.get('error'))
    return out

def vitals():
    out = {'os': 'WSL2 Ubuntu' if 'microsoft' in os.uname().release.lower() else 'Linux'}
    try:
        out['load1'] = round(os.getloadavg()[0], 2); out['cpus'] = os.cpu_count()
        u = shutil.disk_usage(os.path.expanduser('~')); out['disk_free'], out['disk_total'] = u.free, u.total
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, _, rest = line.partition(':'); info[k] = int(rest.split()[0]) * 1024
        out['mem_total'], out['mem_used'] = info['MemTotal'], info['MemTotal'] - info.get('MemAvailable', 0)
        with open('/proc/uptime') as f:
            out['uptime'] = int(float(f.read().split()[0]))
    except Exception:
        pass
    return out

def headed():
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))

def our_chromes(profile=None):
    """1.1.1 (Nov 6): Chrome processes started on one of OUR profiles (~/Procurator/browser/profiles/<name>), found in /proc.
    Each Chrome is started in its own session, so a worker restart (the installer, a crash) leaves them running untracked;
    Chrome then refuses a second start on the same profile (it hands the new window to the old process and exits), and
    the assistant's browser "closes before it opened". Only processes whose --user-data-dir is one of our profiles count."""
    root, found = os.path.realpath(PROFILES), []
    try:
        pids = [p for p in os.listdir('/proc') if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open('/proc/%s/cmdline' % pid, 'rb') as f:
                args = f.read().split(b'\0')
        except OSError:
            continue
        for a in args:
            if a.startswith(b'--user-data-dir='):
                d = os.path.realpath(a[len(b'--user-data-dir='):].decode('utf8', 'replace'))
                if os.path.dirname(d) == root and (profile is None or os.path.basename(d) == profile):
                    found.append(int(pid))
                break
    return found

def reap_orphans(profile=None, tracked=(), sleep=time.sleep):
    """Stop untracked Chrome processes on our profiles (all of them, or one profile): SIGTERM first so the profile is saved,
    then SIGKILL. Returns how many were stopped. Their own Chrome and anything else on the PC are never touched."""
    keep = set(tracked)
    pids = [p for p in our_chromes(profile) if p not in keep and p != os.getpid()]
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except OSError: pass
    for _ in range(20):
        if not any(os.path.exists('/proc/%d' % p) and not _zombie(p) for p in pids):
            break
        sleep(0.25)
    for pid in pids:
        for target in ((lambda: os.killpg(os.getpgid(pid), signal.SIGKILL)), (lambda: os.kill(pid, signal.SIGKILL))):
            try: target()
            except OSError: pass
    if pids:
        log('stopped %d leftover Chrome process(es)%s' % (len(pids), ' on profile ' + profile if profile else ''))
    return len(pids)

def _zombie(pid):
    try:
        with open('/proc/%d/stat' % pid) as f:
            return f.read().rsplit(')', 1)[1].split()[0] == 'Z'
    except (OSError, IndexError):
        return True

def keep_session(user_data_dir):
    """Chrome keeps session cookies (sign-ins without "remember me") across restarts only with "continue where you left off".
    Also mark the last exit clean, so Chrome does not offer to restore after a crash."""
    path = os.path.join(user_data_dir, 'Default', 'Preferences')
    try:
        with open(path) as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    session, profile = prefs.setdefault('session', {}), prefs.setdefault('profile', {})
    if not isinstance(session, dict) or not isinstance(profile, dict):
        return
    if session.get('restore_on_startup') == 1 and profile.get('exit_type') == 'Normal' and profile.get('exited_cleanly') is True:
        return
    session['restore_on_startup'] = 1
    profile['exit_type'], profile['exited_cleanly'] = 'Normal', True
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + '.procurator-tmp'
    with open(tmp, 'w') as f:
        json.dump(prefs, f)
    os.replace(tmp, path)

def tidy_tabs(port, url, settle=1.5, sleep=time.sleep, fetch=None):
    """Restoring the last session reopens old tabs. Keep one page (the requested address when Chrome opened it, else the
    first) so the driver and the sign-in window always act on the right tab; the cookies stay."""
    def get(path):
        if fetch:
            return fetch(path)
        with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=3) as r:
            return r.read()
    try:
        sleep(settle)
        pages = [t for t in json.loads(get('/json/list')) if t.get('type') == 'page']
    except Exception:
        return 0
    if len(pages) < 2:
        return 0
    want = None if url in (None, '', 'about:blank') else urllib.parse.urlsplit(url).hostname
    keep = next((t for t in pages if want and urllib.parse.urlsplit(t.get('url') or '').hostname == want), pages[0])
    closed = 0
    for t in pages:
        if t is not keep and re.fullmatch(r'[A-Za-z0-9-]{1,64}', str(t.get('id') or '')):
            try:
                get('/json/close/' + t['id']); closed += 1
            except Exception:
                pass
    return closed

class Chrome:
    """One Chrome process per open browser session (and one for a sign-in window), each on its own profile and port."""
    def __init__(self, cfg):
        self.cfg, self.procs = cfg, {}

    def free_port(self):
        for port in range(9301, 9399):
            with socket.socket() as s:
                if s.connect_ex(('127.0.0.1', port)) != 0:
                    return port
        raise RuntimeError('no free local port')

    def launch(self, key, profile, url='about:blank', visible=False):
        if not PROFILE.match(profile):
            raise RuntimeError('bad profile name')
        for k, p in list(self.procs.items()):
            if p['profile'] == profile:
                self.stop(k)                     # Chrome allows one process per profile
        reap_orphans(profile, [p['proc'].pid for p in self.procs.values()])   # 1.1.1: and none left from an earlier worker
        os.makedirs(PROFILES, mode=0o700, exist_ok=True)
        keep_session(os.path.join(PROFILES, profile))
        port = self.free_port()
        args = [self.cfg['chrome'], '--user-data-dir=' + os.path.join(PROFILES, profile), '--remote-debugging-port=%d' % port,
                '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check', '--window-size=1280,800',
                '--disable-features=Translate', '--password-store=basic']
        if not (visible or self.cfg.get('headed', True)) or not headed():
            args.append('--headless=new')
        proc = subprocess.Popen(args + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/json/version' % port, timeout=1) as r:
                    json.load(r); break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError('Chrome exited at start (code %s)%s' % (proc.returncode, ' — another Chrome still had this profile open' if proc.returncode == 0 else ''))
                time.sleep(0.25)
        else:
            proc.kill(); raise RuntimeError('Chrome did not open its local debugging port')
        self.procs[key] = {'proc': proc, 'port': port, 'profile': profile, 'started': time.time()}
        tidy_tabs(port, url)
        return port

    def stop(self, key):
        p = self.procs.pop(key, None)
        if p and p['proc'].poll() is None:
            # 1.1.0: SIGTERM to the main Chrome process only, so it shuts down in order and writes its cookies and session
            # to the profile; signalling the whole group at once could kill the network process before it saved them.
            try:
                p['proc'].send_signal(signal.SIGTERM); p['proc'].wait(timeout=10)
            except Exception:
                pass
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try: os.killpg(p['proc'].pid, sig)
                except Exception: break
                time.sleep(0.5)

    def alive(self):
        for k, p in list(self.procs.items()):
            if p['proc'].poll() is not None:
                self.procs.pop(k, None)
        return [k for k in self.procs if SID.match(k)]

    def version(self):
        try:
            return subprocess.run([self.cfg['chrome'], '--version'], capture_output=True, text=True, timeout=10).stdout.strip()[:80]
        except Exception:
            return 'not found'

def localize(inp):
    """Droplet inbox paths in a job -> files in this PC's inbox. Returns (input, {local: droplet}) for files to send back."""
    os.makedirs(INBOX, mode=0o700, exist_ok=True)
    back = {}
    inp = dict(inp)
    for key, ext in (('trace_path', '.zip'), ('capture_path', '.mhtml')):
        v = inp.get(key)
        if isinstance(v, str) and v.startswith(DROPLET_INBOX) and v.endswith(ext) and '/' not in v[len(DROPLET_INBOX):]:
            local = os.path.join(INBOX, os.path.basename(v)); inp[key] = local; back[local] = v
        elif key in inp:
            inp.pop(key)
    steps = []
    for st in inp.get('steps') or []:
        if st.get('kind') == 'upload' and st.get('file_b64'):
            local = os.path.join(INBOX, 'upload-%s.bin' % os.urandom(16).hex())
            with open(local, 'wb') as f:
                f.write(base64.b64decode(st['file_b64']))
            st = {k: v for k, v in st.items() if k != 'file_b64'}; st['file_path'] = local
        steps.append(st)
    if 'steps' in inp:
        inp['steps'] = steps
    return inp, back

class Worker:
    def __init__(self, cfg):
        self.cfg, self.chrome = cfg, Chrome(cfg)
        try:
            reap_orphans()                       # 1.1.1: a fresh worker owns no browser yet; close any left by the last one
        except Exception as e:
            log('leftover check failed: %s' % e)

    def hello(self):
        return relay(self.cfg, 'bhello', {'version': VERSION, 'label': self.cfg.get('label', 'Home PC'), 'chrome': self.chrome.version(), 'headed': headed(),
                                          'profiles': [{'name': n, 'ready': bool(v.get('ready'))} for n, v in sorted(self.cfg['profiles'].items())][:100],
                                          'sessions': self.chrome.alive()[:10], 'vitals': vitals()})

    def handle(self, job):
        op = job.get('op')
        try:
            if op == 'start':
                sid, profile, url = job['session_id'], job['profile'], job.get('url') or 'about:blank'
                if not SID.match(sid) or not PROFILE.match(profile):
                    return {'ok': False, 'error': 'bad job'}
                if never_host(url):
                    return {'ok': False, 'error': 'never-browsed site (refused on this PC)'}
                self.chrome.launch(sid, profile)
                self.cfg['profiles'].setdefault(profile, {'ready': False}); save_config(self.cfg)
                return {'ok': True}
            if op == 'close':
                self.chrome.stop(job.get('session_id')); return {'ok': True}
            if op == 'signin':
                profile, url = job['profile'], job.get('url') or 'about:blank'
                if not PROFILE.match(profile) or never_host(url):
                    return {'ok': False, 'error': 'refused'}
                self.chrome.launch('signin-' + profile, profile, url, visible=True)
                self.cfg['profiles'].setdefault(profile, {'ready': False}); save_config(self.cfg)
                return {'ok': True, 'visible': headed()}
            if op == 'profile':
                profile = job['profile']
                if not PROFILE.match(profile):
                    return {'ok': False, 'error': 'bad profile'}
                self.chrome.stop('signin-' + profile)
                self.cfg['profiles'].setdefault(profile, {})['ready'] = bool(job.get('ready')); save_config(self.cfg)
                return {'ok': True}
            if op == 'driver':
                return self.driver(job)
            return {'ok': False, 'error': 'unknown job'}
        except Exception as e:
            log('job %s %s failed: %s' % (job.get('id'), op, e))
            return {'ok': False, 'error': str(e)[:200]}

    def driver(self, job):
        sid, inp = job.get('session_id'), dict(job.get('input') or {})
        if inp.get('operation') not in DRIVER_OPS:
            return {'raw': json.dumps({'ok': False, 'kind': 'unsupported_on_home_browser'})}
        urls = [inp.get('url')] + [st.get('url') for st in inp.get('steps') or [] if st.get('kind') == 'navigate'] + [inp.get('read_url')]
        if any(never_host(u) for u in urls if u):
            return {'raw': json.dumps({'ok': False, 'kind': 'refused_never'})}
        p = self.chrome.procs.get(sid)
        if not p or p['proc'].poll() is not None:
            return {'raw': json.dumps({'ok': False, 'kind': 'browser_closed'})}
        inp['connectUrl'] = 'http://127.0.0.1:%d' % p['port']
        inp, back = localize(inp)
        env = dict(os.environ, PROCURATOR_PLAYWRIGHT_PATH=self.cfg['playwright'], PROCURATOR_INBOX_PREFIX=INBOX + '/')
        files = {}
        try:
            r = subprocess.run([self.cfg['node'], self.cfg['runner']], input=json.dumps(inp), capture_output=True, text=True, timeout=68, env=env)
            raw = r.stdout
        except subprocess.TimeoutExpired as e:
            raw = (e.stdout or b'').decode('utf8', 'replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
        for local, droplet in back.items():
            if os.path.isfile(local) and os.path.getsize(local) <= 60 * 1024 * 1024:
                with open(local, 'rb') as f:
                    files[droplet] = base64.b64encode(f.read()).decode()
        for st in inp.get('steps') or []:
            if st.get('kind') == 'upload' and st.get('file_path', '').startswith(INBOX):
                back[st['file_path']] = None
        for local in back:
            try: os.remove(local)
            except OSError: pass
        return {'raw': raw, 'files': files}

    def loop(self, once=False):
        last_hello = 0
        while True:
            try:
                if time.time() - last_hello > 30:
                    self.hello(); last_hello = time.time()
                busy = bool(self.chrome.alive())
                got = relay(self.cfg, 'bpull', {'wait': 20 if busy else 25}, timeout=60).get('job')
                if got:
                    out = self.handle(got)
                    if 'raw' in out:
                        relay(self.cfg, 'bresult ' + got['id'], {'stdout': out['raw'], 'files': out.get('files') or {}}, timeout=180)
                    else:
                        relay(self.cfg, 'bresult ' + got['id'], {'stdout': json.dumps(out)}, timeout=60)
                    last_hello = 0 if got.get('op') in ('start', 'close', 'profile', 'signin') else last_hello
            except Exception as e:
                log('loop: %s' % e)
                time.sleep(10)
            if once:
                return

def single_instance():
    """One worker per PC. 1.0.2 (Nov 6 QA): a PID number in the lock file is trusted only if that process really is a
    worker; WSL reuses PIDs quickly, and an unrelated process holding the old number made a new worker quit."""
    lock = os.path.join(BASE, 'worker.pid')
    try:
        with open(lock) as f:
            pid = int(f.read().strip())
        if pid != os.getpid():
            with open('/proc/%d/cmdline' % pid, 'rb') as f:
                cmd = f.read().replace(b'\0', b' ')
            if b'browser_worker.py' in cmd and b' run' in cmd:
                print('browser worker already running (pid %d)' % pid); sys.exit(0)
    except (OSError, ValueError):
        pass
    with open(lock, 'w') as f:
        f.write(str(os.getpid()))

def cmd_check():
    c = load_config(); ok = True
    for k in ('chrome', 'node', 'runner', 'playwright'):
        exists = os.path.exists(c.get(k, ''))
        print('  %-10s %s %s' % (k, c.get(k), 'ok' if exists else 'MISSING')); ok = ok and exists
    print('  chrome     ' + Chrome(c).version())
    print('  windows    ' + ('visible (WSLg)' if headed() else 'hidden (no display; sign-in windows need WSLg)'))
    try:
        # Send the real profile list: an empty one would mark every home sign-in as not ready until the worker's next heartbeat.
        relay(c, 'bhello', {'version': VERSION, 'label': c.get('label', 'Home PC'), 'chrome': Chrome(c).version(), 'headed': headed(),
                            'profiles': [{'name': n, 'ready': bool(v.get('ready'))} for n, v in sorted(c['profiles'].items())][:100],
                            'sessions': [], 'vitals': vitals()}); print('  relay      ok (droplet reached)')
    except Exception as e:
        print('  relay      FAILED: %s' % e); ok = False
    print('  profiles   ' + (', '.join('%s (%s)' % (n, 'signed in' if v.get('ready') else 'not signed in') for n, v in sorted(c['profiles'].items())) or 'none yet'))
    return 0 if ok else 1

def main(argv):
    if not argv or argv[0] not in ('run', 'check', 'profiles'):
        print(__doc__); return 2
    if argv[0] == 'check':
        return cmd_check()
    if argv[0] == 'profiles':
        for n, v in sorted(load_config()['profiles'].items()):
            print('%s: %s' % (n, 'signed in' if v.get('ready') else 'not signed in'))
        return 0
    single_instance()
    log('browser worker %s starting' % VERSION)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (log('worker stopping: SIGTERM'), sys.exit(0)))
    try:
        Worker(load_config()).loop()
    except BaseException as e:
        # 1.0.2: say why it stopped (QA saw a worker vanish with no trace); run-worker.sh restarts it.
        log('worker stopping: %s: %s' % (type(e).__name__, str(e)[:300]))
        raise

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
