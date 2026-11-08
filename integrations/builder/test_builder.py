"""End-to-end tests: droplet relay + builder worker (Mac or WSL) with a fake gh and fake coding agents and a local git remote.
Run: python3 -m unittest -q  (from integrations/builder). No network, no real Claude/Codex/GitHub."""
import json, os, subprocess, sys, tempfile, textwrap, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, 'build-relay.py')
WORKER = os.path.join(HERE, 'worker.py')

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
a = sys.argv[1:]
log = os.environ['FAKE_LOG']
open(log, 'a').write(json.dumps(a) + '\n')
if a[:2] == ['repo', 'clone']:
    extra = [x for x in a[4:] if x not in ('--',) and not x.startswith('--filter')]
    sys.exit(subprocess.call(['git', 'clone', '-q', os.environ['FAKE_REMOTE'], a[3]] + extra))
if a[:2] == ['auth', 'status']: sys.exit(0)
if a[:2] == ['auth', 'git-credential']: sys.exit(0)
if a[:2] == ['repo', 'view']: print(json.dumps({'nameWithOwner': a[2], 'viewerPermission': 'WRITE'})); sys.exit(0)
if a[:2] == ['pr', 'create']:
    repo = a[a.index('--repo') + 1]
    print('https://github.com/%s/pull/7' % repo); sys.exit(0)
sys.exit(3)
'''

FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ.get('FAKE_MODE', 'ok')
if '--version' in sys.argv: print('fake 1.0'); sys.exit(0)
if '--help' in sys.argv: print('--permission-mode --disallowedTools --allowedTools -p --output-format --sandbox --color -C --output-last-message'); sys.exit(0)
if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'): open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['token-seen', len(os.environ['CLAUDE_CODE_OAUTH_TOKEN'])]) + '\n')
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(['agent'] + sys.argv[1:4]) + '\n')
if mode == 'sleep': time.sleep(60)
if mode == 'ok':
    os.makedirs('src', exist_ok=True); open('src/hello.txt', 'w').write('hello from the agent\n')
if mode == 'secret':
    open('src/config.js', 'w').write('const k = "sk-ant-' + 'A' * 40 + '";\n')
if mode == 'outside':
    open('README.md', 'a').write('changed\n')
if mode == 'envfile':
    open('.env', 'w').write('X=1\n')
last = [sys.argv[i + 1] for i, x in enumerate(sys.argv) if x == '--output-last-message']
if last: open(last[0], 'w').write('Codex summary: added src/hello.txt')
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Added src/hello.txt and tested it.', 'total_cost_usd': 0.42}))
'''


class BuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = lambda *p: os.path.join(self.tmp, *p)
        os.makedirs(t('bin')); os.makedirs(t('home'))
        for d in ('queue', 'cancel', 'claims', 'reports'):
            os.makedirs(t('state', 'procurator-build', d))  # the gateway plugin creates these
        for name, body in (('gh', FAKE_GH), ('claude', FAKE_AGENT), ('codex', FAKE_AGENT)):
            with open(t('bin', name), 'w') as f:
                f.write(body)
            os.chmod(t('bin', name), 0o755)
        self.env = dict(os.environ, HOME=t('home'), FAKE_LOG=t('calls.log'), FAKE_REMOTE=t('remote.git'),
                        PROCURATOR_BUILD_DIR=t('state', 'procurator-build'), PROCURATOR_BUILDER_HOME=t('builder'),
                        GIT_AUTHOR_NAME='A', GIT_AUTHOR_EMAIL='a@example.com', GIT_COMMITTER_NAME='A', GIT_COMMITTER_EMAIL='a@example.com')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', t('remote.git')], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', t('seed')], check=True)
        open(t('seed', 'README.md'), 'w').write('seed\n')
        os.makedirs(t('seed', 'src')); open(t('seed', 'src', 'app.txt'), 'w').write('app\n')
        open(t('seed', 'test.sh'), 'w').write('#!/bin/sh\ntest -f src/hello.txt && echo all good\n'); os.chmod(t('seed', 'test.sh'), 0o755)
        for c in (['add', '-A'], ['commit', '-qm', 'seed'], ['push', '-q', t('remote.git'), 'main']):
            subprocess.run(['git'] + c, cwd=t('seed'), check=True, env=self.env)
        os.makedirs(t('builder'))
        self.cfg = {'relay': [sys.executable, RELAY, '--builder', 'mac'], 'label': 'Mac', 'builds_dir': t('builds'), 'path': t('bin') + ':' + os.environ['PATH'],
                    'tools': {'gh': t('bin', 'gh'), 'claude': t('bin', 'claude'), 'codex': t('bin', 'codex'), 'git': 'git'},
                    'repos': {'example-user/demo': {'branch': 'main', 'test': './test.sh', 'agents': ['claude', 'codex']}}}
        json.dump(self.cfg, open(t('builder', 'config.json'), 'w'))
        self.t = t

    def queue(self, build_id='a' * 16, **plan):
        p = {'repo': 'example-user/demo', 'base': 'main', 'agent': 'claude', 'title': 'Add hello file', 'goal': 'Add src/hello.txt saying hello.',
             'acceptance': ['src/hello.txt exists'], 'paths': ['src/'], 'max_minutes': 5}
        p.update(plan)
        json.dump({'id': build_id, 'approved_at': int(time.time() * 1000), 'plan': p, 'hash': 'x' * 16, 'version': 1},
                  open(self.t('state', 'procurator-build', 'queue', build_id + '.json'), 'w'))
        return build_id

    def relay(self, cmd, data=None, name='mac'):
        p = subprocess.run([sys.executable, RELAY, '--builder', name] + cmd.split(), input=json.dumps(data or {}), capture_output=True, text=True, env=self.env)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def worker(self, *args, mode='ok', timeout=120):
        env = dict(self.env, FAKE_MODE=mode)
        return subprocess.run([sys.executable, WORKER] + list(args), capture_output=True, text=True, env=env, timeout=timeout)

    def report(self, build_id):
        return json.load(open(self.t('state', 'procurator-build', 'reports', build_id + '.json')))

    def test_relay_hello_pull_report_contract(self):
        self.assertEqual(self.relay('hello', {'repos': ['example-user/demo'], 'agents': {'claude': True}})['cancel'], [])
        b = json.load(open(self.t('state', 'procurator-build', 'builders', 'mac.json')))
        self.assertEqual(b['repos'], ['example-user/demo']); self.assertTrue(b['agents']['claude']); self.assertFalse(b['agents']['codex'])
        self.assertFalse(self.relay('hello', {'repos': ['not a repo']})['ok'])
        self.assertIsNone(self.relay('pull')['job'])
        i = self.queue()
        self.assertEqual(self.relay('pull')['job']['id'], i)
        self.assertIsNone(self.relay('pull')['job'], 'claimed once only')
        self.assertFalse(self.relay('report ' + 'b' * 16, {'status': 'running'})['ok'], 'unclaimed id refused')
        self.assertFalse(self.relay('report ' + i, {'status': 'done', 'pr_url': 'https://evil.example/pull/1'})['ok'])
        self.assertFalse(self.relay('report ' + i, {'status': 'running', 'branch': 'main'})['ok'])
        self.assertTrue(self.relay('report ' + i, {'status': 'running', 'message': 'x' * 5000})['ok'])
        self.assertEqual(len(self.report(i)['message']), 600)
        open(self.t('state', 'procurator-build', 'cancel', i), 'w').write('1')
        self.assertEqual(self.relay('hello', {})['cancel'], [i])
        self.assertTrue(self.relay('report ' + i, {'status': 'cancelled'})['cancel'])
        self.assertFalse(self.relay('report ' + i, {'status': 'running'})['ok'], 'final status is final')
        self.assertFalse(self.relay('rm -rf /')['ok'])
        self.relay('hello', {'repos': ['example-user/demo']})
        old = self.queue('c' * 16)
        q = self.t('state', 'procurator-build', 'queue', old + '.json'); j = json.load(open(q)); j['approved_at'] = 0; json.dump(j, open(q, 'w'))
        self.assertIsNone(self.relay('pull')['job'])
        self.assertEqual(self.report(old)['status'], 'failed'); self.assertIn('24 hours', self.report(old)['message'])

    def test_claude_build_end_to_end_opens_draft_pr(self):
        i = self.queue()
        out = self.worker('once')
        self.assertEqual(out.returncode, 0, out.stderr)
        r = self.report(i)
        self.assertEqual(r['status'], 'done', r)
        self.assertEqual(r['pr_url'], 'https://github.com/example-user/demo/pull/7')
        self.assertEqual(r['tests']['status'], 'passed'); self.assertEqual(r['files'], ['src/hello.txt'])
        self.assertEqual(r['cost_usd'], 0.42); self.assertIn('Added src/hello.txt', r['summary'])
        self.assertEqual([e['status'] for e in r['events']], ['claimed', 'preparing', 'running', 'testing', 'pushing', 'done'])
        branches = subprocess.run(['git', 'branch', '--list'], cwd=self.t('remote.git'), capture_output=True, text=True).stdout
        self.assertIn('procurator/' + i, branches)
        calls = [json.loads(l) for l in open(self.t('calls.log'))]
        pr = next(c for c in calls if c[:2] == ['pr', 'create'])
        self.assertIn('--draft', pr); self.assertEqual(pr[pr.index('--base') + 1], 'main')
        agent = next(c for c in calls if c[0] == 'agent')
        self.assertEqual(agent[1], '-p'); self.assertIn('Never add or print secrets', agent[2])
        self.assertIn('Change only src/', agent[2])
        msg = subprocess.run(['git', 'log', '-1', '--format=%B', 'procurator/' + i], cwd=self.t('remote.git'), capture_output=True, text=True).stdout
        self.assertIn('PROCURATOR build ' + i, msg)

    def test_saved_claude_token_reaches_only_the_claude_run(self):
        with open(self.t('builder', 'claude-oauth-token'), 'w') as f:
            f.write('sk-ant-oat01-' + 'x' * 40 + '\n')
        i = self.queue()
        self.worker('once')
        self.assertEqual(self.report(i)['status'], 'done')
        calls = [json.loads(l) for l in open(self.t('calls.log'))]
        self.assertIn(['token-seen', 53], calls)
        self.assertNotIn('sk-ant-oat01', open(self.t('builder', 'logs', 'worker.log')).read())
        out = self.worker('check')
        self.assertIn('Claude Code unattended token: saved', out.stdout)

    def test_codex_build_uses_last_message_summary(self):
        i = self.queue(agent='codex')
        self.worker('once')
        r = self.report(i)
        self.assertEqual(r['status'], 'done', r); self.assertIn('Codex summary', r['summary'])

    def test_blocked_changes_are_never_pushed(self):
        for n, (mode, want) in enumerate((('secret', 'looks like a key'), ('outside', 'outside the allowed paths: README.md'),
                                           ('envfile', 'blocked file: .env'), ('nothing', 'without changing any files'))):
            i = '%016x' % (n + 1)
            self.queue(i)
            self.worker('once', mode=mode)
            r = self.report(i)
            self.assertEqual(r['status'], 'failed', (mode, r)); self.assertIn(want, r['message'])
            self.assertNotIn('pr_url', r)
            branches = subprocess.run(['git', 'branch', '--list'], cwd=self.t('remote.git'), capture_output=True, text=True).stdout
            self.assertNotIn('procurator/' + i, branches, mode)

    def test_repo_not_allowed_is_never_handed_out_and_worker_refuses_it_anyway(self):
        i = self.queue(repo='someone/else')
        self.worker('once')
        self.assertFalse(os.path.exists(self.t('state', 'procurator-build', 'reports', i + '.json')), 'relay never hands it to this builder')
        sys.path.insert(0, HERE)
        import importlib
        w = importlib.import_module('worker')
        with self.assertRaisesRegex(w.JobError, "not on this builder's allowed repo list"):
            w.validate({'id': i, 'plan': {'repo': 'someone/else', 'title': 'x' * 5, 'goal': 'y' * 20}}, self.cfg | {'repos': self.cfg['repos'], 'tools': self.cfg['tools']})

    def test_builders_only_see_and_report_their_own_builds(self):
        self.relay('hello', {'repos': ['example-user/demo'], 'label': 'Mac'}, name='mac')
        self.relay('hello', {'repos': ['example-user/demo'], 'label': 'Windows PC'}, name='windows')
        w = json.load(open(self.t('state', 'procurator-build', 'builders', 'windows.json')))
        self.assertEqual((w['name'], w['label']), ('windows', 'Windows PC'))
        mac_only = self.queue('1' * 16, builder='mac')
        self.assertIsNone(self.relay('pull', name='windows')['job'], 'a Mac-only build is not handed to Windows')
        self.assertEqual(self.relay('pull', name='mac')['job']['id'], mac_only)
        self.assertFalse(self.relay('report ' + mac_only, {'status': 'running'}, name='windows')['ok'], 'Windows cannot report on the Mac build')
        self.assertTrue(self.relay('report ' + mac_only, {'status': 'running'}, name='mac')['ok'])
        self.assertEqual(self.report(mac_only)['builder'], 'mac')
        anyone = self.queue('2' * 16)
        self.assertEqual(self.relay('pull', name='windows')['job']['id'], anyone)
        self.assertEqual(json.load(open(self.t('state', 'procurator-build', 'claims', anyone + '.json')))['builder'], 'windows')
        with open(self.t('state', 'procurator-build', 'cancel', mac_only), 'w') as f:
            f.write('1')
        self.assertEqual(self.relay('hello', {'repos': ['example-user/demo']}, name='windows')['cancel'], [])
        self.assertEqual(self.relay('hello', {'repos': ['example-user/demo']}, name='mac')['cancel'], [mac_only])
        self.assertFalse(self.relay('hello', {}, name='../x')['ok'])

    def test_restart_mid_build_is_reported_once(self):
        i = self.queue()
        self.relay('hello', {'repos': ['example-user/demo']})
        self.relay('pull')
        self.relay('report ' + i, {'status': 'running'})
        with open(self.t('builder', 'current.json'), 'w') as f:
            json.dump({'id': i, 'started': 0}, f)
        self.worker('once')
        r = self.report(i)
        self.assertEqual(r['status'], 'failed'); self.assertIn('builder restarted', r['message'])
        self.assertFalse(os.path.exists(self.t('builder', 'current.json')))

    def test_only_one_worker_runs_per_machine(self):
        import fcntl
        fd = os.open(self.t('builder', 'worker.lock'), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        i = self.queue()
        self.worker('once')
        self.assertFalse(os.path.exists(self.t('state', 'procurator-build', 'claims', i + '.json')))
        self.assertIn('another builder is already running', open(self.t('builder', 'logs', 'worker.log')).read())
        os.close(fd)

    def test_cancel_stops_a_running_agent(self):
        i = self.queue()
        env = dict(self.env, FAKE_MODE='sleep', PROCURATOR_BUILDER_TICK='0.5')
        p = subprocess.Popen([sys.executable, WORKER, 'once'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(100):
            time.sleep(0.2)
            try:
                if self.report(i)['status'] == 'running':
                    break
            except (OSError, ValueError):
                pass
        with open(self.t('state', 'procurator-build', 'cancel', i), 'w') as f:
            f.write('1')
        p.wait(30)
        r = self.report(i)
        self.assertEqual(r['status'], 'cancelled', r)
        self.assertIn('/buildcancel', r['message'])
        branches = subprocess.run(['git', 'branch', '--list'], cwd=self.t('remote.git'), capture_output=True, text=True).stdout
        self.assertNotIn('procurator/' + i, branches)

    def test_check_and_add_repo(self):
        out = self.worker('check')
        self.assertIn('droplet: connected', out.stdout); self.assertIn('example-user/demo', out.stdout)
        self.assertIn('claude options: OK', out.stdout); self.assertIn('codex options: OK', out.stdout)
        out = self.worker('add-repo', 'example-user/other', '--test', 'npm test', '--agents', 'claude')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        cfg = json.load(open(self.t('builder', 'config.json')))
        self.assertEqual(cfg['repos']['example-user/other'], {'branch': 'main', 'test': 'npm test', 'agents': ['claude'], 'allow_bash': []})
        self.assertNotEqual(self.worker('add-repo', 'bad repo').returncode, 0)


if __name__ == '__main__':
    unittest.main()


class VitalsTests(unittest.TestCase):
    """Worker 1.3.0 (Nov 6): machine vitals ride along in hello; the relay keeps numbers only."""

    def test_worker_vitals_and_relay_filter(self):
        import importlib.util, os
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('worker_mod', os.path.join(here, 'worker.py')); w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
        v = w.vitals()
        self.assertIn(v['os'], ('macOS', 'Linux', 'WSL2 Ubuntu')); self.assertGreater(v.get('mem_total', 1), 0); self.assertIn('disk_free', v)
        self.assertEqual(w.hello_payload({'repos': {}, 'label': 'Mac', 'tools': {}})['vitals']['os'], v['os'])
        spec = importlib.util.spec_from_file_location('relay_mod', os.path.join(here, 'build-relay.py')); r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
        self.assertEqual(r.vitals_of({'load1': 0.5, 'cpus': 8, 'mem_used': 1e9, 'mem_total': 2e9, 'os': 'macOS', 'evil': 'x', 'disk_free': -1, 'uptime': True}),
                         {'load1': 0.5, 'cpus': 8, 'mem_used': 1000000000, 'mem_total': 2000000000, 'os': 'macOS'})
        self.assertEqual(r.vitals_of('nope'), {})
