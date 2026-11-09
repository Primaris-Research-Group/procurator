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
stream = '--input-format' in sys.argv            # Claude Code live mode (1.4.0): the prompt arrives on stdin
def emit(ev): print(json.dumps(ev), flush=True)
def read_user():
    line = sys.stdin.readline()
    if not line: sys.exit(0)
    return json.loads(line)['message']['content'][0]['text']
def result(text, cost=0.42): emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text, 'total_cost_usd': cost, 'num_turns': 1})
argv_log = ['agent'] + sys.argv[1:4]
if stream:
    prompt = read_user()
    argv_log = ['agent', '-p', prompt, 'stream']
if '-m' in sys.argv: argv_log.append('model=' + sys.argv[sys.argv.index('-m') + 1])
if '--model' in sys.argv: argv_log.append('model=' + sys.argv[sys.argv.index('--model') + 1])
open(os.environ['FAKE_LOG'], 'a').write(json.dumps(argv_log) + '\n')
if mode == 'sleep': time.sleep(60)
if mode == 'ask' and stream:
    emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Looking at the repo.'}, {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'README.md'}}]}})
    result('QUESTION: Should the greeting be in English or Spanish?')
    answer = read_user()                           # the owner's note, relayed as "OWNER: …"
    emit({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Write', 'input': {'file_path': 'src/hello.txt'}}]}})
    os.makedirs('src', exist_ok=True); open('src/hello.txt', 'w').write(answer + '\n')
    result('Wrote src/hello.txt as the owner asked: ' + answer)
    read_user()                                    # EOF: PROCURATOR closed stdin
if mode == 'json-codex':
    emit({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'ls src'}})
    emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Adding the file now.'}})
    os.makedirs('src', exist_ok=True); open('src/hello.txt', 'w').write('hello from codex\n')
    emit({'type': 'item.completed', 'item': {'type': 'file_change', 'changes': [{'path': 'src/hello.txt'}]}})
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
if stream:
    emit({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Write', 'input': {'file_path': 'src/hello.txt'}}]}})
    result('Added src/hello.txt and tested it.')
    read_user()                                    # wait for PROCURATOR to close stdin (no question, no notes)
elif '--json' not in sys.argv:
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
                    'progress_s': 5, 'wait_minutes': 1,
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
        # live builds (1.4.0): progress is appended, notes come back once, waiting_owner carries the question
        self.assertFalse(self.relay('report ' + i, {'status': 'running', 'progress': [{'t': 'hack', 'text': 'x'}]})['ok'])
        out = self.relay('report ' + i, {'status': 'running', 'progress': [{'t': 'say', 'text': 'Reading'}, {'t': 'tool', 'name': 'Edit', 'text': 'src/a.py'}], 'model': 'opus'})
        self.assertEqual(out['notes'], [])
        self.assertEqual([json.loads(l)['t'] for l in open(self.t('state', 'procurator-build', 'progress', i + '.jsonl'))], ['say', 'tool'])
        self.assertEqual(self.report(i)['progress_count'], 2); self.assertEqual(self.report(i)['model'], 'opus')
        notes = self.t('state', 'procurator-build', 'notes', i); os.makedirs(notes)
        json.dump({'text': 'use Spanish', 'at': 5}, open(os.path.join(notes, '1700000000000-abcd.json'), 'w'))
        open(os.path.join(notes, 'evil.txt'), 'w').write('x')
        out = self.relay('report ' + i, {'status': 'waiting_owner', 'question': 'Which language?'})
        self.assertEqual([n['text'] for n in out['notes']], ['use Spanish']); self.assertEqual(self.report(i)['question'], 'Which language?')
        self.assertEqual(self.relay('report ' + i, {'status': 'running'})['notes'], [], 'each note is delivered once')
        self.assertNotIn('question', self.report(i), 'moving on clears the question')
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
        self.assertIn('Change only src/', agent[2]); self.assertIn('QUESTION:', agent[2], 'the live protocol is in the prompt')
        self.assertEqual(agent[3], 'stream', 'Claude Code runs in stream-json mode by default (1.4.0)')
        progress = [json.loads(l) for l in open(self.t('state', 'procurator-build', 'progress', i + '.jsonl'))]
        self.assertEqual([e['t'] for e in progress], ['tool', 'result']); self.assertEqual(progress[0]['name'], 'Write'); self.assertEqual(progress[0]['text'], 'src/hello.txt')
        msg = subprocess.run(['git', 'log', '-1', '--format=%B', 'procurator/' + i], cwd=self.t('remote.git'), capture_output=True, text=True).stdout
        self.assertIn('PROCURATOR build ' + i, msg)

    def test_live_build_waits_for_the_owner_and_relays_the_answer(self):
        """1.4.0: the agent asks QUESTION:, the report goes waiting_owner, /buildnote (a notes/ file) reaches the agent as the
        next message, the build finishes with the owner's answer in the change, and every step is in the progress log."""
        i = self.queue(model='opus')
        env = dict(self.env, FAKE_MODE='ask')
        p = subprocess.Popen([sys.executable, WORKER, 'once'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if self.report(i)['status'] == 'waiting_owner':
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.3)
        r = self.report(i)
        self.assertEqual(r['status'], 'waiting_owner', r); self.assertEqual(r['question'], 'Should the greeting be in English or Spanish?'); self.assertEqual(r['model'], 'opus')
        notes = self.t('state', 'procurator-build', 'notes', i); os.makedirs(notes, exist_ok=True)
        json.dump({'text': 'Spanish, please: hola', 'at': 1}, open(os.path.join(notes, '%d-abcd1234.json' % int(time.time() * 1000)), 'w'))
        out, err = p.communicate(timeout=90)
        self.assertEqual(p.returncode, 0, err)
        r = self.report(i)
        self.assertEqual(r['status'], 'done', r); self.assertNotIn('question', r, 'an answered question is cleared')
        self.assertIn('hola', r['summary'])
        self.assertEqual(os.listdir(notes), [], 'delivered notes are consumed')
        shown = subprocess.run(['git', 'show', 'procurator/%s:src/hello.txt' % i], cwd=self.t('remote.git'), capture_output=True, text=True).stdout
        self.assertEqual(shown.strip(), 'OWNER: Spanish, please: hola')
        kinds = [json.loads(l)['t'] for l in open(self.t('state', 'procurator-build', 'progress', i + '.jsonl'))]
        self.assertEqual(kinds, ['say', 'tool', 'question', 'note', 'tool', 'result'])
        calls = [json.loads(l) for l in open(self.t('calls.log'))]
        self.assertIn('model=opus', next(c for c in calls if c[0] == 'agent'))

    def test_codex_live_streams_progress_and_passes_the_model(self):
        i = self.queue(agent='codex', model='gpt-5.3-codex')
        out = self.worker('once', mode='json-codex')
        self.assertEqual(out.returncode, 0, out.stderr)
        r = self.report(i)
        self.assertEqual(r['status'], 'done', r); self.assertIn('Codex summary', r['summary'])
        kinds = [(json.loads(l)['t'], json.loads(l).get('name')) for l in open(self.t('state', 'procurator-build', 'progress', i + '.jsonl'))]
        self.assertEqual(kinds, [('tool', 'bash'), ('say', None), ('tool', 'edit'), ('result', None)])
        calls = [json.loads(l) for l in open(self.t('calls.log'))]
        self.assertIn('model=gpt-5.3-codex', next(c for c in calls if c[0] == 'agent'))

    def test_model_must_be_on_the_builders_list_when_one_is_set(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('w', WORKER); w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
        cfg = dict(self.cfg, models={'claude': ['opus', 'sonnet']}, default_models={'claude': 'sonnet'}, tools={'claude': sys.executable})
        base = {'repo': 'example-user/demo', 'title': 'Add it', 'goal': 'Add the file please.'}
        self.assertEqual(w.validate({'id': 'a' * 16, 'plan': base}, cfg)['model'], 'sonnet', 'the builder default fills in')
        self.assertEqual(w.validate({'id': 'a' * 16, 'plan': dict(base, model='opus')}, cfg)['model'], 'opus')
        with self.assertRaisesRegex(w.JobError, 'model list'):
            w.validate({'id': 'a' * 16, 'plan': dict(base, model='haiku')}, cfg)
        with self.assertRaisesRegex(w.JobError, 'Bad model'):
            w.validate({'id': 'a' * 16, 'plan': dict(base, model='x;rm')}, cfg)
        self.assertEqual(w.find_question('Done.\nQUESTION: Tabs or spaces?'), 'Tabs or spaces?')
        self.assertEqual(w.find_question('I changed two files. Should I also update the docs?'), 'I changed two files. Should I also update the docs?')
        self.assertIsNone(w.find_question('All done. Tests pass.'))

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
