import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
process.env.PROCURATOR_EDITION='x';   // 0.30.0: builds exist on PROCURATOR-X; the last test covers the standard edition
const {Builds, registerBuilds, validatePlan, parsePacketMarkdown}=await import('./builds.mjs');

const RELAY = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'builder', 'build-relay.py');
const args = {repo: 'example-user/demo', title: 'Add hello file', goal: 'Add src/hello.txt that says hello.', acceptance: ['src/hello.txt exists'], paths: ['src/'], request_id: 'req-hello-01'};
const machine = (name, label) => (dir, extra = {}) => { fs.mkdirSync(path.join(dir, 'builders'), {recursive: true}); fs.writeFileSync(path.join(dir, 'builders', name + '.json'), JSON.stringify({name, label, seen_at: Date.now(), repos: ['example-user/demo'], agents: {claude: true, codex: false}, default_agent: 'claude', branches: {'example-user/demo': 'main'}, ...extra})); };
const mac = machine('mac', 'Mac'), windows = machine('windows', 'Windows PC');
function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'builds-'));
  const sent = [];
  const gateway = async (method, params) => { if (method === 'send') sent.push(params); return method === 'sessions.describe' ? {session: {deliveryContext: {channel: 'telegram', to: '100000001', accountId: 'default'}}} : {}; };
  let now = Date.now();
  const builds = new Builds(dir, gateway, () => now);
  return {dir, sent, builds, tick: ms => { now += ms; }};
}

test('packets are validated against the builders\' allowed repos and agents', () => {
  assert.throws(() => validatePlan(args, []), /No builder has checked in/);
  const b = [{name: 'mac', label: 'Mac', online: true, repos: ['example-user/demo'], agents: {claude: true, codex: false}, branches: {}}];
  assert.equal(validatePlan(args, b).plan.base, 'main');
  assert.equal(validatePlan(args, b).plan.agent, 'claude');
  assert.equal(validatePlan(args, b).plan.builder, 'any');
  assert.throws(() => validatePlan({...args, repo: 'someone/else'}, b), /not on the allowed list of any builder.*add-repo someone\/else/);
  assert.throws(() => validatePlan({...args, agent: 'codex'}, b), /Codex is not installed on Mac/);
  assert.throws(() => validatePlan({...args, builder: 'windows'}, b), /No builder named windows/);
  assert.throws(() => validatePlan({...args, base: '../main'}, b), /base must be a branch/);
  assert.throws(() => validatePlan({...args, paths: ['/etc']}, b), /paths must be/);
  assert.throws(() => validatePlan({...args, paths: ['../x']}, b), /paths must be/);
  assert.throws(() => validatePlan({...args, max_minutes: 90}, b), /5-60/);
  assert.throws(() => validatePlan({...args, shell: 'rm -rf /'}, b), /Unknown field/);
  assert.throws(() => validatePlan({...args, request_id: 'x'}, b), /request_id/);
  const two = [...b, {name: 'windows', label: 'Windows PC', online: true, repos: ['example-user/demo', 'example-user/site'], agents: {claude: true, codex: true}, branches: {'example-user/site': 'develop'}}];
  assert.equal(validatePlan({...args, agent: 'codex'}, two).plan.agent, 'codex', 'Codex is on the Windows PC');
  assert.equal(validatePlan({...args, repo: 'example-user/site'}, two).plan.base, 'develop');
  assert.throws(() => validatePlan({...args, repo: 'example-user/site', builder: 'mac'}, two), /not on the allowed list of Mac/);
  assert.equal(validatePlan({...args, builder: 'Windows'}, two).plan.builder, 'windows');
});

test('prepare sends the exact review; /buildapprove queues it once; wrong code queues nothing', async () => {
  const {dir, sent, builds} = fixture(); mac(dir);
  const out = await builds.prepare(args);
  assert.equal(out.status, 'awaiting_approval'); assert.equal(out.runs_on, 'Mac (online)');
  const review = sent[0].message;
  assert.equal(sent[0].to, '100000001'); assert.equal(sent[0].accountId, 'default');
  for (const want of ['BUILD REVIEW — ' + out.id, 'example-user/demo (from main) · Claude Code', 'Runs on: Mac (online)', 'Done when:', '• src/hello.txt exists', 'Files it may change: src/', 'DRAFT pull request', `/buildapprove ${out.id} `]) assert.ok(review.includes(want), want);
  assert.ok(!/`/.test(review), 'no backticks in Telegram commands');
  const again = await builds.prepare(args);
  assert.equal(again.id, out.id, 'same request_id + same packet = same build');
  await assert.rejects(builds.prepare({...args, goal: 'Something else entirely, please.'}), /already used for a different build/);
  assert.throws(() => builds.approve(out.id, '0'.repeat(16)), /does not match/);
  assert.equal(fs.existsSync(path.join(dir, 'queue', out.id + '.json')), false);
  const r = builds.find(builds.read(), out.id);
  assert.equal(builds.approve(out.id, r.hash).status, 'queued');
  const q = JSON.parse(fs.readFileSync(path.join(dir, 'queue', out.id + '.json'), 'utf8'));
  assert.deepEqual(q.plan, r.plan); assert.equal(q.hash, r.hash);
  assert.throws(() => builds.approve(out.id, r.hash), /queued; nothing to approve/);
});

test('approval expires after 30 minutes', async () => {
  const {dir, builds, tick} = fixture(); mac(dir);
  const out = await builds.prepare(args); const r = builds.find(builds.read(), out.id);
  tick(31 * 60000);
  assert.throws(() => builds.approve(out.id, r.hash), /expired/);
});

test('the queue file is exactly what the droplet relay hands the Mac', {skip: spawnSync('python3', ['--version']).status !== 0}, async () => {
  const {dir, builds} = fixture(); mac(dir);
  const out = await builds.prepare(args); const r = builds.find(builds.read(), out.id); builds.approve(out.id, r.hash);
  const run = (cmd, input = '{}') => JSON.parse(spawnSync('python3', [RELAY, '--builder', 'mac', ...cmd.split(' ')], {input, env: {...process.env, PROCURATOR_BUILD_DIR: dir}, encoding: 'utf8'}).stdout.trim().split('\n').pop());
  run('hello', JSON.stringify({repos: ['example-user/demo'], label: 'Mac', agents: {claude: true}}));
  const pulled = run('pull').job;
  assert.equal(pulled.id, out.id); assert.deepEqual(pulled.plan, r.plan);
  assert.equal(run('pull').job, null);
  assert.equal(run('report ' + out.id, JSON.stringify({status: 'running', branch: 'procurator/' + out.id})).ok, true);
  assert.equal(builds.report(out.id).status, 'running'); assert.equal(builds.report(out.id).builder, 'mac');
  assert.equal(builds.builders()[0].label, 'Mac');
});

test('sweep tells the owner when the Mac starts, finishes, fails, and when it is offline', async () => {
  const {dir, sent, builds, tick} = fixture(); mac(dir);
  const a = await builds.prepare(args); builds.approve(a.id, builds.find(builds.read(), a.id).hash);
  const write = (id, rep) => fs.writeFileSync(path.join(dir, 'reports', id + '.json'), JSON.stringify({id, updated_at: Date.now(), ...rep}));
  sent.length = 0;
  write(a.id, {status: 'running', builder: 'mac'}); await builds.sweep(); await builds.sweep();
  assert.equal(sent.length, 1); assert.match(sent[0].message, /started on your Mac: Claude Code is working on example-user\/demo/); assert.match(sent[0].message, new RegExp('/buildcancel ' + a.id));
  write(a.id, {status: 'done', builder: 'mac', pr_url: 'https://github.com/example-user/demo/pull/7', tests: {status: 'passed', command: 'npm test'}, files: ['src/hello.txt'], file_count: 1, insertions: 1, deletions: 0, duration_s: 240, cost_usd: 0.42, summary: 'Added src/hello.txt.'});
  await builds.sweep(); await builds.sweep();
  assert.equal(sent.length, 2);
  for (const want of ['BUILD READY — ' + a.id, 'built on your Mac', 'Draft pull request: https://github.com/example-user/demo/pull/7', 'Tests: passed (npm test)', '1 file(s), +1/-0 lines, 4 min, ~$0.42', 'Added src/hello.txt.', 'nothing is merged or deployed']) assert.ok(sent[1].message.includes(want), want);
  assert.equal(builds.status(a.id).build.pr_url, 'https://github.com/example-user/demo/pull/7');
  // a forged PR link from the report file is not shown
  const b = await builds.prepare({...args, request_id: 'req-hello-02', title: 'Second build'}); builds.approve(b.id, builds.find(builds.read(), b.id).hash);
  write(b.id, {status: 'failed', message: 'Change blocked before pushing: blocked file: .env', pr_url: 'https://evil.example/pull/1'});
  await builds.sweep();
  const failed = sent.at(-1).message;
  assert.match(failed, /BUILD FAILED/); assert.match(failed, /blocked file: \.env/); assert.match(failed, /Nothing was pushed to GitHub/); assert.ok(!failed.includes('evil'));
  // offline Mac: one alert after 10 minutes
  const c = await builds.prepare({...args, request_id: 'req-hello-03', title: 'Third build'});
  mac(dir, {seen_at: Date.now() - 3600000}); builds.approve(c.id, builds.find(builds.read(), c.id).hash);
  const before = sent.length; tick(11 * 60000); await builds.sweep(); await builds.sweep();
  assert.equal(sent.length, before + 1); assert.match(sent.at(-1).message, /no builder that allows example-user\/demo is online \(Mac: last seen 1 h ago\)/);
  tick(24 * 3600000); await builds.sweep();
  assert.equal(builds.find(builds.read(), c.id).status, 'expired'); assert.match(sent.at(-1).message, /expired: no builder picked it up/);
  assert.ok(fs.existsSync(path.join(dir, 'cancel', c.id)), 'the relay will never hand it out');
});

test('cancel: pending is dropped, claimed asks the Mac to stop', async () => {
  const {dir, builds} = fixture(); mac(dir);
  const a = await builds.prepare(args);
  assert.equal(builds.cancel(a.id).status, 'cancelled');
  const b = await builds.prepare({...args, request_id: 'req-hello-02'}); builds.approve(b.id, builds.find(builds.read(), b.id).hash);
  assert.equal(builds.cancel(b.id).status, 'cancelled', 'not yet claimed: cancelled outright');
  assert.ok(fs.existsSync(path.join(dir, 'cancel', b.id)));
  const c = await builds.prepare({...args, request_id: 'req-hello-03'}); builds.approve(c.id, builds.find(builds.read(), c.id).hash);
  fs.writeFileSync(path.join(dir, 'claims', c.id + '.json'), '{}');
  assert.equal(builds.cancel(c.id).status, 'cancel_requested');
});

test('tools are main-agent only; commands are owner-only and plain text', async () => {
  const {dir, builds} = fixture(); mac(dir);
  const tools = [], commands = [];
  registerBuilds({registerTool: (f, o) => tools.push([f, o]), registerCommand: c => commands.push(c)}, builds, ctx => { if (!ctx.owner) throw new Error('Only the owner.'); });
  clearInterval(globalThis[Symbol.for('owner.builds.sweep')]);
  assert.deepEqual(tools.map(([, o]) => o.name), ['build_prepare', 'build_status', 'build_cancel']);
  assert.equal(tools[0][0]({agentId: 'phone'}), undefined);
  const prep = tools[0][0]({agentId: 'main'});
  const out = JSON.parse((await prep.execute('x', args)).content[0].text);
  assert.equal(out.status, 'awaiting_approval');
  const status = JSON.parse((await tools[1][0]({agentId: 'main'}).execute('x', {})).content[0].text);
  assert.deepEqual(status.builders[0].repos, ['example-user/demo']); assert.equal(status.builds[0].id, out.id);
  const approve = commands.find(c => c.name === 'buildapprove');
  assert.match((await approve.handler({owner: false, args: 'x'})).text, /Only the owner/);
  assert.match((await approve.handler({owner: true, args: 'nonsense'})).text, /entire line/);
  const hash = builds.find(builds.read(), out.id).hash;
  assert.match((await approve.handler({owner: true, args: `${out.id} ${hash}`})).text, /approved and queued\. Mac picks it up/);
  const st = (await commands.find(c => c.name === 'buildstatus').handler({owner: true, args: ''})).text;
  assert.match(st, /Mac \(mac\): online/); assert.match(st, new RegExp(out.id + ' · queued · example-user/demo'));
  assert.ok(!/`/.test(st));
  assert.deepEqual(commands.map(c => c.name), ['buildapprove', 'buildcancel', 'buildstatus', 'buildreview', 'buildfrom', 'buildnote', 'buildlog']);
});

test('two builders: the review names who can run it; a named builder is respected', async () => {
  const {dir, sent, builds} = fixture(); mac(dir, {seen_at: Date.now() - 7200000}); windows(dir, {agents: {claude: true, codex: true}});
  const a = await builds.prepare(args);
  assert.equal(a.runs_on, 'Windows PC (online)');
  assert.match(sent.at(-1).message, /Runs on: the first free of: Mac \(offline, last seen 2 h ago\), Windows PC \(online\)/);
  const b = await builds.prepare({...args, request_id: 'req-mac-only-1', builder: 'mac'});
  assert.match(sent.at(-1).message, /Runs on: Mac \(offline, last seen 2 h ago\)/);
  assert.equal(builds.find(builds.read(), b.id).plan.builder, 'mac');
  const st = builds.status();
  assert.deepEqual(st.builders.map(x => [x.name, x.online]), [['mac', false], ['windows', true]]);
});

test('a build can belong to a packet step; finishing it reports once to onFinish (Nov 6)', async () => {
  const f = fixture(); mac(f.dir);
  assert.throws(() => validatePlan({...args, mission: 'BG!'}, [{name: 'mac', label: 'Mac', online: true, repos: ['example-user/demo'], agents: {claude: true}, branches: {}}]), /mission must be/);
  assert.throws(() => validatePlan({...args, mission: 'bg', step: 'nope'}, [{name: 'mac', label: 'Mac', online: true, repos: ['example-user/demo'], agents: {claude: true}, branches: {}}]), /step must look like/);
  const r = await f.builds.prepare({...args, mission: 'bg', step: 'MAIN/s1'});
  const job = f.builds.read().jobs[0];
  assert.equal(job.mission, 'bg'); assert.equal(job.step, 'MAIN/s1');
  assert.equal(f.builds.status(r.id).build.mission, 'bg');
  f.builds.approve(r.id, job.hash);
  fs.writeFileSync(path.join(f.dir, 'reports', r.id + '.json'), JSON.stringify({status: 'done', builder: 'mac', pr_url: 'https://github.com/example-user/demo/pull/7', cost_usd: 0.3, tests: {status: 'passed'}, files: []}));
  const finished = [];
  f.builds.onFinish = async (job, rep) => finished.push([job.id, job.mission, job.step, rep.pr_url, rep.cost_usd]);
  await f.builds.sweep(); await f.builds.sweep();
  assert.deepEqual(finished, [[r.id, 'bg', 'MAIN/s1', 'https://github.com/example-user/demo/pull/7', 0.3]]);
  assert.ok(f.builds.read().jobs[0].recorded);
});

test('standard edition: build commands answer with the edition note and touch nothing',async()=>{
 process.env.PROCURATOR_EDITION='standard';
 try{
  const cmds={};const api={registerTool(){},registerCommand(c){cmds[c.name]=c;}};
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'builds-std-'));
  registerBuilds(api,new Builds(dir,{}),()=>{});
  const r=await cmds.buildapprove.handler({args:'0123456789abcdef 0123456789abcdef',channel:'telegram'});
  assert.match(r.text,/PROCURATOR-X/);
  assert.ok(!fs.existsSync(path.join(dir,'jobs.json')));
 }finally{process.env.PROCURATOR_EDITION='x';}
});

const BUILD_MD = `---
repo: example-user/demo
agent: claude
model: opus
paths: [src/, tests/]
max_minutes: 45
---
# Add a greeting file

## Goal
Add src/hello.txt with a friendly greeting.
Keep it to one line.

## Acceptance
- [ ] src/hello.txt exists
- [ ] tests pass

## Notes
The owner prefers Spanish.
`;

test('0.31.0: a BUILD.md packet is parsed into a plan; the model rides along; explicit fields win', async () => {
  const parsed = parsePacketMarkdown(BUILD_MD);
  assert.equal(parsed.title, 'Add a greeting file'); assert.equal(parsed.repo, 'example-user/demo'); assert.equal(parsed.model, 'opus');
  assert.deepEqual(parsed.paths, ['src/', 'tests/']); assert.equal(parsed.max_minutes, 45);
  assert.deepEqual(parsed.acceptance, ['src/hello.txt exists', 'tests pass']);
  assert.match(parsed.goal, /^Add src\/hello\.txt with a friendly greeting\.\nKeep it to one line\.\n\nNotes:\nThe owner prefers Spanish\.$/);
  const {dir, builds} = fixture(); mac(dir, {models: {claude: ['opus', 'sonnet']}});
  const {plan} = validatePlan({markdown: BUILD_MD, request_id: 'req-md-01', max_minutes: 20}, builds.builders());
  assert.equal(plan.model, 'opus'); assert.equal(plan.max_minutes, 20, 'explicit argument wins over the file'); assert.equal(plan.title, 'Add a greeting file');
  assert.throws(() => validatePlan({markdown: BUILD_MD.replace('model: opus', 'model: haiku'), request_id: 'req-md-02'}, builds.builders()), /not on the builder's model list/);
  assert.throws(() => validatePlan({markdown: BUILD_MD.replace('model: opus', 'model: x;rm -rf'), request_id: 'req-md-03'}, builds.builders()), /short model name/);
  const a = await builds.prepare({markdown: BUILD_MD, request_id: 'req-md-04'});
  const review = builds.review(builds.find(builds.read(), a.id));
  assert.match(review, /Claude Code \(opus\)/); assert.match(review, /\/buildnote [a-f0-9]{16} <text> steers it/);
  assert.ok(!fs.existsSync(path.join(dir, 'queue', a.id + '.json')), 'not queued before approval');
  builds.approve(a.id, builds.find(builds.read(), a.id).hash);
  assert.equal(JSON.parse(fs.readFileSync(path.join(dir, 'queue', a.id + '.json'), 'utf8')).plan.model, 'opus', 'the relay hands the model to the builder');
});

test('0.31.0: live build — the question reaches the owner at once, /buildnote queues the answer, digests are throttled, /buildlog shows progress', async () => {
  const {dir, sent, builds, tick} = fixture(); mac(dir);
  const commands = [];
  registerBuilds({registerTool: () => {}, registerCommand: c => commands.push(c)}, builds, ctx => { if (!ctx.owner) throw new Error('Only the owner.'); });
  clearInterval(globalThis[Symbol.for('owner.builds.sweep')]);
  const note = commands.find(c => c.name === 'buildnote'), log = commands.find(c => c.name === 'buildlog');
  const a = await builds.prepare(args); builds.approve(a.id, builds.find(builds.read(), a.id).hash);
  assert.match((await note.handler({owner: true, args: `${a.id} hurry`})).text, /notes reach the agent only while it is working/);
  const write = (rep) => fs.writeFileSync(path.join(dir, 'reports', a.id + '.json'), JSON.stringify({id: a.id, updated_at: Date.now(), builder: 'mac', ...rep}));
  const progress = (entries) => fs.appendFileSync(path.join(dir, 'progress', a.id + '.jsonl'), entries.map(e => JSON.stringify({at: Date.now(), ...e})).join('\n') + '\n');
  sent.length = 0;
  progress([{t: 'say', text: 'Reading the repo.'}, {t: 'tool', name: 'Edit', text: 'src/hello.txt'}]);
  write({status: 'running', progress_count: 2}); await builds.sweep();
  assert.equal(sent.length, 2, 'started notice and the first progress digest');
  const digest1 = sent.find(m => /▸ Edit/.test(m.message));
  assert.ok(digest1); assert.match(digest1.message, /▸ Edit src\/hello\.txt/); assert.match(digest1.message, new RegExp('/buildnote ' + a.id));
  progress([{t: 'say', text: 'Running tests.'}]); write({status: 'running', progress_count: 3}); await builds.sweep();
  assert.equal(sent.length, 2, 'no second digest within two minutes');
  tick(3 * 60000); await builds.sweep();
  assert.equal(sent.length, 3); assert.match(sent[2].message, /Running tests\./);
  // the agent asks
  progress([{t: 'question', text: 'English or Spanish?'}]); write({status: 'waiting_owner', question: 'English or Spanish?', progress_count: 4}); await builds.sweep(); await builds.sweep();
  assert.equal(sent.length, 4); assert.match(sent[3].message, /BUILD QUESTION — [a-f0-9]{16}/); assert.match(sent[3].message, /English or Spanish\?/); assert.match(sent[3].message, new RegExp('/buildnote ' + a.id + ' your answer'));
  assert.equal(builds.find(builds.read(), a.id).status, 'waiting_owner');
  assert.match((await note.handler({owner: false, args: `${a.id} Spanish`})).text, /Only the owner/);
  assert.match((await note.handler({owner: true, args: `${a.id} Spanish`})).text, /Answer sent to build/);
  const files = fs.readdirSync(path.join(dir, 'notes', a.id));
  assert.equal(files.length, 1); assert.match(files[0], /^\d{13}-[a-f0-9]{8}\.json$/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(dir, 'notes', a.id, files[0]), 'utf8')).text, 'Spanish');
  const text = (await log.handler({owner: true, args: a.id})).text;
  assert.match(text, /waiting owner/); assert.match(text, /Waiting for you: English or Spanish\?/); assert.match(text, /❓ English or Spanish\?/); assert.match(text, /▸ Edit src\/hello\.txt/);
  const view = builds.status(a.id).build;
  assert.equal(view.question, 'English or Spanish?'); assert.equal(view.progress.length, 4); assert.equal(view.steer, `/buildnote ${a.id} <text>`);
  // answered: the relay clears the question; the same question is not re-sent
  write({status: 'running', progress_count: 5}); await builds.sweep(); await builds.sweep();
  assert.equal(sent.filter(m => /BUILD QUESTION/.test(m.message)).length, 1);
  write({status: 'done', pr_url: 'https://github.com/example-user/demo/pull/9', files: ['src/hello.txt'], file_count: 1}); await builds.sweep();
  assert.match(sent.at(-1).message, /BUILD READY/);
  assert.match((await note.handler({owner: true, args: `${a.id} too late`})).text, /notes reach the agent only while it is working/);
});

test('0.32.0: verified PRs — review fields in the packet, verification in the status view and the BUILD READY message', async () => {
  const {dir, sent, builds} = fixture(); mac(dir);
  const parsed = parsePacketMarkdown(BUILD_MD.replace('model: opus', 'model: opus\nreview: codex\nreview_model: gpt-5.3-codex'));
  assert.equal(parsed.review, 'codex'); assert.equal(parsed.review_model, 'gpt-5.3-codex');
  assert.throws(() => validatePlan({...args, review: 'gemini'}, builds.builders()), /review must be auto/);
  const {plan} = validatePlan({...args, review: 'off'}, builds.builders());
  assert.equal(plan.review, 'off');
  const a = await builds.prepare({...args, review: 'off'});
  assert.match(builds.review(builds.find(builds.read(), a.id)), /no independent review/); assert.match(builds.review(builds.find(builds.read(), a.id)), /no second agent reviews it/);
  const b = await builds.prepare({...args, request_id: 'req-ver-02', title: 'Verified build'});
  assert.match(builds.review(builds.find(builds.read(), b.id)), /reviewed by the other agent/);
  builds.approve(b.id, builds.find(builds.read(), b.id).hash);
  const ver = {checks: [{n: 1, text: 'src/hello.txt exists', status: 'passed', output: ''}, {n: 2, text: 'says hola', status: 'failed', output: 'no hola yet'}],
    review: {verdict: 'approve', agent: 'codex', model: 'gpt-5.3-codex', summary: 'Small and correct.', issues: [{severity: 'low', file: 'src/hello.txt', text: 'Trailing newline.'}]},
    rounds: 1, unresolved: 'CHECKS', screenshots: 2, screenshot_note: '', passed: false};
  fs.writeFileSync(path.join(dir, 'reports', b.id + '.json'), JSON.stringify({id: b.id, updated_at: Date.now(), builder: 'mac', status: 'done', pr_url: 'https://github.com/example-user/demo/pull/12', files: ['src/hello.txt'], file_count: 1, tests: {status: 'passed', command: 'npm test'}, verification: ver}));
  sent.length = 0; await builds.sweep(); await builds.sweep();
  const ready = sent.find(m => /BUILD READY/.test(m.message)).message;
  assert.match(ready, /Acceptance: 1\/2 checks passed — #2 failed/); assert.match(ready, /Review \(Codex, gpt-5.3-codex\): approve — Small and correct\./);
  assert.match(ready, /Screenshots: 2 in the PR/); assert.match(ready, /1 fix round\(s\) were sent back to Claude Code/); assert.match(ready, /Verification: OPEN ITEMS \(checks\)/);
  const view = builds.status(b.id).build;
  assert.equal(view.verification.checks[1].status, 'failed'); assert.equal(view.verification.review.issues[0].file, 'src/hello.txt'); assert.equal(view.verification.passed, false);
});

test('0.32.0: /buildfrom SLUG prepares the build from the BUILD.md the Composer saved; a missing packet is a clear error', async () => {
  const {dir, sent, builds} = fixture(); mac(dir);
  const commands = [];
  registerBuilds({registerTool: () => {}, registerCommand: c => commands.push(c)}, builds, ctx => { if (!ctx.owner) throw new Error('Only the owner.'); });
  clearInterval(globalThis[Symbol.for('owner.builds.sweep')]);
  const from = commands.find(c => c.name === 'buildfrom');
  assert.match((await from.handler({owner: true, args: 'badge'})).text, /No BUILD\.md saved for "badge"/);
  assert.match((await from.handler({owner: true, args: '../x'})).text, /Use: \/buildfrom SLUG/);
  fs.writeFileSync(path.join(dir, 'packets', 'badge.md'), BUILD_MD);
  const text = (await from.handler({owner: true, args: 'badge'})).text;
  assert.match(text, /BUILD REVIEW sent for packet badge \(build [a-f0-9]{16}\)/);
  const review = sent.at(-1).message;
  assert.match(review, /Title: Add a greeting file/); assert.match(review, /Claude Code \(opus\)/);
  const job = builds.read().jobs.at(-1);
  assert.equal(job.mission, 'badge'); assert.equal(job.plan.model, 'opus');
  assert.match((await from.handler({owner: false, args: 'badge'})).text, /Only the owner/);
  const again = (await from.handler({owner: true, args: 'badge'})).text;
  assert.match(again, /BUILD REVIEW sent/, 'the same file re-sends the same review (same request id)');
  assert.equal(builds.read().jobs.length, 1);
});
