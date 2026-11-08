"""Home browser lane (web 0.19.0, Phase 5): relay contract, per-machine isolation, never-list, and an end-to-end run
broker -> relay -> worker -> real Chrome on this machine (skipped without a local Chrome and playwright-core)."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / 'web'))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['PROCURATOR_LANES_DIR'] = self.tmp.name + '/lanes'
        os.environ['PROCURATOR_INBOX_DIR'] = self.tmp.name + '/inbox'
        os.environ['PROCURATOR_BUILD_DIR'] = self.tmp.name + '/build/procurator-build'
        os.makedirs(self.tmp.name + '/inbox'); os.makedirs(self.tmp.name + '/build')
        self.r = load('relay_t', REPO / 'builder' / 'build-relay.py')
    def tearDown(self):
        self.tmp.cleanup()
        for k in ('PROCURATOR_LANES_DIR', 'PROCURATOR_INBOX_DIR', 'PROCURATOR_BUILD_DIR'):
            os.environ.pop(k, None)
    def call(self, name, cmd, data=None):
        return self.r.browser_main(cmd, io.StringIO(json.dumps(data or {})), name)
    def job(self, machine, jid, **extra):
        d = Path(self.tmp.name) / 'lanes' / machine / 'jobs'; d.mkdir(parents=True, exist_ok=True)
        (d / (jid + '.json')).write_text(json.dumps({'id': jid, 'op': 'driver', **extra}))

    def test_hello_pull_result_and_isolation(self):
        self.assertTrue(self.call('home', 'bhello', {'label': 'Ryzen PC', 'profiles': [{'name': 'leaveco', 'ready': True}], 'sessions': ['a' * 16]})['ok'])
        hello = json.loads((Path(self.tmp.name) / 'lanes/home/hello.json').read_text())
        self.assertEqual(hello['profiles'], [{'name': 'leaveco', 'ready': True}])
        jid = 'b' * 24
        self.job('home', jid)
        self.assertIsNone(self.call('other', 'bpull', {'wait': 0})['job'])          # another machine never sees it
        self.assertEqual(self.call('home', 'bpull', {'wait': 0})['job']['id'], jid)
        self.assertIsNone(self.call('home', 'bpull', {'wait': 0})['job'])           # claimed once
        with self.assertRaises(self.r.RelayError): self.call('other', 'bresult ' + jid, {'stdout': 'x'})
        self.assertTrue(self.call('home', 'bresult ' + jid, {'stdout': '{"ok":true}'})['ok'])
        with self.assertRaises(self.r.RelayError): self.call('home', 'bresult ' + jid, {'stdout': 'again'})

    def test_withdrawn_jobs_are_never_pulled_and_files_are_checked(self):
        jid = 'c' * 24; self.job('home', jid)
        claims = Path(self.tmp.name) / 'lanes/home/claims'; claims.mkdir(parents=True, exist_ok=True)
        (claims / (jid + '.json')).write_text('{"by":"broker-withdrawn"}')
        self.assertIsNone(self.call('home', 'bpull', {'wait': 0})['job'])
        jid2 = 'd' * 24; self.job('home', jid2); self.call('home', 'bpull', {'wait': 0})
        inbox = self.tmp.name + '/inbox/'
        for bad in ['/etc/passwd', inbox + '../x.zip', inbox + 'evil.sh', '/tmp/trace-abc-12345678.zip']:
            with self.assertRaises(self.r.RelayError): self.call('home', 'bresult ' + jid2, {'stdout': '', 'files': {bad: 'eA=='}})
        ok = inbox + 'trace-abcdef-12345678.zip'
        self.call('home', 'bresult ' + jid2, {'stdout': '', 'files': {ok: 'UEsDBA=='}})
        self.assertEqual(Path(ok).read_bytes(), b'PK\x03\x04')

    def test_build_and_browser_keys_cannot_use_each_others_commands(self):
        with self.assertRaises(self.r.RelayError): self.r.browser_main('pull', io.StringIO(''), 'home')
        with self.assertRaises(self.r.RelayError): self.r.main('bpull', io.StringIO(''), 'home')

class WorkerUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['PROCURATOR_BROWSER_HOME'] = self.tmp.name
        self.w = load('bw_t', HERE / 'browser_worker.py')
        Path(self.tmp.name, 'config.json').write_text(json.dumps({'relay': ['true'], 'chrome': '/bin/false', 'node': 'node', 'runner': 'x', 'playwright': 'x', 'profiles': {}}))
    def tearDown(self):
        self.tmp.cleanup(); os.environ.pop('PROCURATOR_BROWSER_HOME', None)
    def test_never_list_matches_the_broker_and_is_enforced_locally(self):
        import site_policy
        self.assertEqual(sorted(self.w.NEVER), sorted(site_policy.FIXED))
        worker = self.w.Worker(self.w.load_config())
        self.assertIn('never', worker.handle({'op': 'start', 'session_id': 'a' * 16, 'profile': 'chase', 'url': 'https://secure.chase.com/'})['error'])
        out = worker.handle({'op': 'driver', 'session_id': 'a' * 16, 'input': {'operation': 'act', 'url': 'https://www.leaveco.example/', 'steps': [{'kind': 'navigate', 'url': 'https://www.wellsfargo.com/'}]}})
        self.assertEqual(json.loads(out['raw'])['kind'], 'refused_never')
        self.assertEqual(json.loads(worker.handle({'op': 'driver', 'session_id': 'a' * 16, 'input': {'operation': 'owner_login'}})['raw'])['kind'], 'unsupported_on_home_browser')
    def test_check_keeps_the_signed_in_profiles_on_the_droplet(self):
        c = self.w.load_config(); c['profiles'] = {'leaveco': {'ready': True}, 'cvs': {}}; self.w.save_config(c)
        sent = []
        self.w.relay = lambda cfg, cmd, data=None: sent.append((cmd, data)) or {'ok': True}
        with unittest.mock.patch('sys.stdout', io.StringIO()):
            self.w.cmd_check()
        self.assertEqual(sent[0][0], 'bhello')
        self.assertEqual(sent[0][1]['profiles'], [{'name': 'cvs', 'ready': False}, {'name': 'leaveco', 'ready': True}])
    @unittest.skipUnless(os.path.isdir('/proc/self'), 'the worker runs in WSL (Linux); the PID check reads /proc, which macOS does not have')
    def test_a_reused_pid_does_not_stop_a_new_worker(self):
        # Nov 6 QA: the worker vanished; an unrelated process holding the old PID made a new worker think one was running.
        import subprocess
        lock = Path(self.tmp.name, 'worker.pid')
        lock.write_text(str(os.getppid()))                      # some live process that is not a worker
        self.w.single_instance()
        self.assertEqual(lock.read_text(), str(os.getpid()))
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)', 'browser_worker.py', 'run'])
        try:
            lock.write_text(str(other.pid))
            with unittest.mock.patch('sys.stdout', io.StringIO()), self.assertRaises(SystemExit):
                self.w.single_instance()
        finally:
            other.kill()
    def test_droplet_paths_are_mapped_to_this_pc(self):
        inp, back = self.w.localize({'trace_path': '/run/procurator-web/inbox/trace-ab-12345678.zip', 'capture_path': '/etc/x.mhtml',
                                     'steps': [{'kind': 'upload', 'file_b64': 'eA==', 'file': {'name': 'n.pdf'}}]})
        self.assertTrue(inp['trace_path'].startswith(self.tmp.name)); self.assertNotIn('capture_path', inp)
        self.assertEqual(open(inp['steps'][0]['file_path'], 'rb').read(), b'x'); self.assertNotIn('file_b64', inp['steps'][0])
        self.assertEqual(list(back.values()), ['/run/procurator-web/inbox/trace-ab-12345678.zip'])

class KeepHistoryTests(WorkerUnitTests):
    """1.1.0 (Nov 6, "the bot always opens a browser with no history"): the profile keeps session cookies across restarts,
    restored old tabs do not confuse the driver, and Chrome is stopped in order so it saves its cookies."""
    def test_profiles_continue_where_they_left_off(self):
        d = os.path.join(self.tmp.name, 'profiles', 'procurator')
        self.w.keep_session(d)
        prefs = json.loads(Path(d, 'Default', 'Preferences').read_text())
        self.assertEqual(prefs['session']['restore_on_startup'], 1); self.assertEqual(prefs['profile']['exit_type'], 'Normal')
        Path(d, 'Default', 'Preferences').write_text(json.dumps({'session': {'restore_on_startup': 5}, 'profile': {'exit_type': 'Crashed', 'name': 'P'}, 'x': 1}))
        self.w.keep_session(d)
        prefs = json.loads(Path(d, 'Default', 'Preferences').read_text())
        self.assertEqual((prefs['session']['restore_on_startup'], prefs['profile']['exit_type'], prefs['profile']['name'], prefs['x']), (1, 'Normal', 'P', 1))
        Path(d, 'Default', 'Preferences').write_text('not json')
        self.w.keep_session(d)                                    # a damaged file is replaced, not fatal
        self.assertEqual(json.loads(Path(d, 'Default', 'Preferences').read_text())['session']['restore_on_startup'], 1)

    def test_restored_tabs_are_closed_except_the_one_to_use(self):
        tabs = [{'id': 'A1', 'type': 'page', 'url': 'https://www.leaveco.example/claims'}, {'id': 'B2', 'type': 'page', 'url': 'https://www.hotels.com/trips'},
                {'id': 'W3', 'type': 'service_worker', 'url': 'https://www.hotels.com/sw.js'}]
        closed = []
        def fetch(path):
            if path == '/json/list': return json.dumps(tabs).encode()
            closed.append(path); return b'Target is closing'
        self.assertEqual(self.w.tidy_tabs(1, 'https://www.hotels.com/', sleep=lambda s: None, fetch=fetch), 1)
        self.assertEqual(closed, ['/json/close/A1'])                     # the sign-in window keeps the hotels tab
        closed.clear(); self.w.tidy_tabs(1, 'about:blank', sleep=lambda s: None, fetch=fetch)
        self.assertEqual(closed, ['/json/close/B2'])                     # a task browser keeps one page; the driver navigates it
        closed.clear(); tabs[:] = tabs[:1]; self.assertEqual(self.w.tidy_tabs(1, None, sleep=lambda s: None, fetch=fetch), 0)
        self.assertEqual(self.w.tidy_tabs(1, None, sleep=lambda s: None, fetch=lambda p: (_ for _ in ()).throw(OSError('down'))), 0)

    @unittest.skipUnless(os.path.isdir('/proc/self'), 'process groups as on WSL')
    def test_chrome_is_asked_to_quit_before_anything_is_killed(self):
        import signal, subprocess
        script = 'import signal,sys,time\nsignal.signal(signal.SIGTERM, lambda *a: (open(sys.argv[1],"w").write("clean"), sys.exit(0)))\ntime.sleep(30)'
        mark = Path(self.tmp.name, 'mark')
        proc = subprocess.Popen([sys.executable, '-c', script, str(mark)], start_new_session=True)
        time.sleep(0.3)
        chrome = self.w.Chrome(self.w.load_config()); chrome.procs['a' * 16] = {'proc': proc, 'port': 1, 'profile': 'procurator', 'started': 0}
        chrome.stop('a' * 16)
        self.assertEqual(mark.read_text(), 'clean'); self.assertIsNotNone(proc.poll())

class LeftoverChromeTests(WorkerUnitTests):
    """1.1.1 (Nov 6, hotels.com after the 1.1.0 install): Chrome windows left by the previous worker kept the profile, so
    the new browser exited at once. Leftovers on OUR profiles are stopped; other processes are not."""
    @unittest.skipUnless(os.path.isdir('/proc/self'), 'reads /proc, as on WSL')
    def test_leftovers_on_our_profiles_are_stopped_and_nothing_else(self):
        import subprocess
        prof = os.path.join(self.tmp.name, 'profiles')
        sleeper = 'import time; time.sleep(30)'
        ours = subprocess.Popen([sys.executable, '-c', sleeper, '--user-data-dir=' + os.path.join(prof, 'hotels')], start_new_session=True)
        other = subprocess.Popen([sys.executable, '-c', sleeper, '--user-data-dir=' + os.path.join(self.tmp.name, 'elsewhere', 'hotels')], start_new_session=True)
        mine = subprocess.Popen([sys.executable, '-c', sleeper, '--user-data-dir=' + os.path.join(prof, 'leaveco')], start_new_session=True)
        try:
            time.sleep(0.3)
            self.assertEqual(sorted(self.w.our_chromes()), sorted([ours.pid, mine.pid]))
            self.assertEqual(self.w.reap_orphans('hotels'), 1)
            ours.wait(timeout=5); self.assertIsNone(other.poll()); self.assertIsNone(mine.poll())
            self.assertEqual(self.w.reap_orphans(None, tracked=[mine.pid]), 0)          # a browser this worker runs is kept
            self.w.Worker(self.w.load_config())                                         # a new worker clears the rest
            mine.wait(timeout=5); self.assertIsNone(other.poll())
        finally:
            for p in (ours, other, mine):
                p.kill()

CHROME, PW = os.environ.get('PROCURATOR_TEST_CHROME'), os.environ.get('PROCURATOR_PLAYWRIGHT_PATH')

@unittest.skipUnless(CHROME and PW, 'needs a local Chrome and playwright-core (PROCURATOR_TEST_CHROME, PROCURATOR_PLAYWRIGHT_PATH)')
class EndToEndTests(unittest.TestCase):
    """The broker drives a real Chrome through the relay and the worker: open, read, type, an approved submit, close."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); t = self.tmp.name
        os.environ.update(PROCURATOR_LANES_DIR=t + '/state/lanes', PROCURATOR_INBOX_DIR=t + '/inbox', PROCURATOR_BROWSER_HOME=t + '/pc')
        os.makedirs(t + '/inbox'); os.makedirs(t + '/pc'); os.makedirs(t + '/state')
        self.w = load('bw_e2e', HERE / 'browser_worker.py')
        Path(t, 'pc', 'config.json').write_text(json.dumps({'relay': [sys.executable, str(REPO / 'builder' / 'build-relay.py'), '--browser', 'home'],
            'chrome': CHROME, 'node': 'node', 'runner': str(REPO / 'web-plugin' / 'runner.cjs'), 'playwright': PW, 'profiles': {}, 'label': 'Test PC', 'headed': False}))
        self.worker = self.w.Worker(self.w.load_config())
        self.stop = False
        def loop():
            while not self.stop:
                self.worker.loop(once=True)
        self.thread = threading.Thread(target=loop, daemon=True); self.thread.start()
        from broker import Broker
        sys.path.insert(0, str(REPO / 'web'))
        from test_broker import Provider
        self.cloud = Provider()
        self.b = Broker(Path(t, 'state', 'browser.sqlite3'), self.cloud)
        for _ in range(60):
            if self.b.lanes.online_machine(): break
            time.sleep(0.5)
    def tearDown(self):
        self.stop = True
        for k in list(self.worker.chrome.procs): self.worker.chrome.stop(k)
        self.tmp.cleanup()
        for k in ('PROCURATOR_LANES_DIR', 'PROCURATOR_INBOX_DIR', 'PROCURATOR_BROWSER_HOME'): os.environ.pop(k, None)

    def test_practice_form_on_the_home_browser_with_an_approved_submit(self):
        m = self.b.lanes.online_machine(); self.assertIsNotNone(m); self.assertEqual(m['label'], 'Test PC')
        sid = 'f' * 16
        created = self.b.lanes.start('home', sid, 'practice')
        with self.b.db() as db:
            db.execute('INSERT INTO sessions(id,request_id,provider_id,url,origin,practice,status,created,timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?)',
                       (sid, 'home-e2e-0001', created['id'], 'practice', 'practice', 1, 'open', time.time(), 2700))
        s = self.b.session(sid); self.assertEqual(s['lane'], 'home')
        init = self.b.provider.run(s, 'init'); self.assertTrue(init['ok'], init)
        snap = self.b.call('snapshot', {'id': sid})['snapshot']
        self.assertTrue(any(c['label'] == 'Name' for c in snap['controls']))
        name = next(c['ref'] for c in snap['controls'] if c['label'] == 'Name')
        submit = next(c['ref'] for c in snap['controls'] if c['label'].startswith('Submit'))
        for rid, steps in [('home-fill-01', [{'kind': 'fill', 'ref': name, 'value': 'The owner'}]), ('home-subm-01', [{'kind': 'click', 'ref': submit}])]:
            p = self.b.call('prepare', {'id': sid, 'request_id': rid, 'steps': steps})
            self.b.call('review', {'id': p['id']}); r = self.b.plan(p['id'])
            self.assertEqual(self.b.call('execute', {'id': p['id'], 'hash': r['hash'][:16]}, True)['status'], 'completed')
        self.assertIn('PRACTICE CONFIRMED: The owner', self.b.call('snapshot', {'id': sid})['snapshot']['text'])
        self.assertEqual(self.b.provider.inspect(created['id'])['status'], 'RUNNING')
        self.b.call('close', {'id': sid})
        self.assertEqual(self.b.session(sid)['status'], 'closed')
        self.assertEqual(self.cloud.acts, 0)                       # nothing went to the cloud browser


class InstallerTests(unittest.TestCase):
    def test_installer_carries_the_current_worker_and_driver(self):
        import base64, re
        text = (HERE / 'dist' / 'install-procurator-browser-wsl.sh').read_text()
        for marker, path in (('WORKER_B64', HERE / 'browser_worker.py'), ('RUNNER_B64', REPO / 'web-plugin' / 'runner.cjs')):
            blob = re.search(r"<<'%s'\n(.*?)\n%s\n" % (marker, marker), text, re.S).group(1)
            self.assertEqual(base64.b64decode(blob.replace('\n', '')), path.read_bytes(), 'installer is stale: run python3 integrations/browser-worker/bundle.py')

if __name__ == '__main__':
    unittest.main()
