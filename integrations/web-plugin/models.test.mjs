import {test} from 'node:test';
import assert from 'node:assert/strict';
import {PRESETS, ModelSwitch, registerModelSwitch, checkAccess, patchFor, currentModel} from './models.mjs';

const baseConfig = () => ({agents: {defaults: {model: {primary: 'anthropic/claude-sonnet-5-5'}, models: {'anthropic/claude-sonnet-5-5': {params: {cacheRetention: 'short', anthropicServerCompaction: true, anthropicCompactThreshold: 80000}}}},
  entries: {main: {workspace: '/w', params: {cacheRetention: 'long'}}, phone: {model: 'anthropic/claude-sonnet-5-5'}}}});
function fixture({status = 200, env = {OPENAI_API_KEY: 'sk-test', ANTHROPIC_API_KEY: 'ak-test'}, config = baseConfig()} = {}) {
  const calls = [], fetched = [];
  let cfg = config;
  const gateway = async (method, params, timeoutMs) => {
    calls.push([method, params, timeoutMs]);
    if (method === 'config.get') return {config: cfg, hash: 'h1'};
    if (method === 'config.patch') { const p = JSON.parse(params.raw); cfg = {...cfg, agents: {...cfg.agents, entries: {...cfg.agents.entries, main: {...cfg.agents.entries.main, ...p.agents.entries.main}}}}; return {ok: true}; }
    return {ok: true};
  };
  const fetchImpl = async (url, opts) => { fetched.push([url, opts]); return {status}; };
  const models = new ModelSwitch(gateway, {env, fetchImpl, defer: fn => fn(), verifyTries: 2, verifyDelayMs: 1, readyDelayMs: 1, readyEveryMs: 1, readyTries: 2});
  return {calls, fetched, models, cfg: () => cfg, sent: () => calls.filter(c => c[0] === 'send').map(c => c[1].message)};
}

test('status shows the current model, the four choices as one-tap commands, and what is not affected', async () => {
  const {models} = fixture();
  const s = await models.status();
  assert.match(s, /AI model for PROCURATOR: Claude Sonnet 5\.5/);
  for (const k of ['sonnet', 'opus', 'sol', 'terra']) assert.match(s, new RegExp('/ai_' + k + ' — '));
  assert.match(s, /\/ai_sonnet — Claude Sonnet 5\.5 \(current\)/);
  assert.match(s, /Phone calls keep their own voice/);
  assert.ok(!/`/.test(s), 'no backticks in Telegram');
});

test('switching to Opus checks access, writes the main agent default with a fallback, keeps cost settings, clears the chat pin', async () => {
  const {models, calls, fetched, cfg, sent} = fixture();
  const reply = await models.switchTo('Opus');
  assert.match(reply, /Switching PROCURATOR to Claude Opus 5\.5 \(your account has access\)… I'll confirm here/);
  const out = await models.lastApply;
  // 0.24.3 (B10): saved first, with the restart said plainly; Ready only after the gateway answers on the new model.
  assert.match(out, /^Saved: PROCURATOR will use Claude Opus 5\.5 \(was Claude Sonnet 5\.5\)\.\nIt restarts to load the new model/);
  assert.doesNotMatch(out, /Switched|^Ready|now uses/m);
  assert.equal(sent()[0], out, 'the saved notice arrives as its own Telegram message');
  const ready = await models.ready;
  assert.match(ready, /^Ready: PROCURATOR is answering again and now uses Claude Opus 5\.5 \(was Claude Sonnet 5\.5\)/);
  assert.match(ready, /If Claude Opus 5\.5 fails, it answers with Claude Sonnet 5\.5/);
  assert.equal(sent()[1], ready);
  assert.equal(calls.find(c => c[0] === 'send')[1].to, '100000001');
  assert.equal(calls.find(c => c[0] === 'config.patch')[2], 180000, 'long timeout for the write');
  assert.equal(fetched[0][0], 'https://api.anthropic.com/v1/models/claude-opus-5-5');
  const patch = calls.find(c => c[0] === 'config.patch')[1];
  assert.equal(patch.baseHash, 'h1'); assert.deepEqual(patch.replacePaths, ['agents.entries.main.model.fallbacks']);
  const body = JSON.parse(patch.raw);
  assert.deepEqual(body.agents.entries.main.model, {primary: 'anthropic/claude-opus-5-5', fallbacks: ['anthropic/claude-sonnet-5-5']});
  assert.deepEqual(body.agents.defaults.models['anthropic/claude-opus-5-5'].params, {anthropicServerCompaction: true, anthropicCompactThreshold: 80000});
  assert.equal(body.agents.entries.phone, undefined, 'phone agent untouched');
  assert.equal(body.agents.defaults.model, undefined, 'global default untouched (cost and voice scripts keep working)');
  assert.deepEqual(calls.find(c => c[0] === 'sessions.patch')[1], {key: 'agent:main:main', model: null});
  assert.equal(currentModel(cfg()).ref, 'anthropic/claude-opus-5-5');
  assert.match(await models.switchTo('opus'), /already uses Claude Opus 5\.5/);
});

test('GPT models run on the OpenClaw runtime with OpenAI compaction, and refuse cleanly when the account lacks them', async () => {
  const p = patchFor('sol', baseConfig());
  assert.deepEqual(p.agents.defaults.models['openai/gpt-6.1-sol'], {alias: 'sol', params: {responsesServerCompaction: true, responsesCompactThreshold: 80000}, agentRuntime: {id: 'openclaw'}});
  const missing = fixture({status: 404});
  assert.match(await missing.models.switchTo('terra'), /Not switched: GPT-6\.1 Terra is not available to your OpenAI account/);
  assert.equal(missing.calls.some(c => c[0] === 'config.patch'), false, 'nothing written');
  const refused = fixture({status: 401});
  assert.match(await refused.models.switchTo('sol'), /OpenAI key on the server was refused \(401\)/);
  const nokey = fixture({env: {}});
  assert.match(await nokey.models.switchTo('sol'), /no OpenAI API key/);
  const ok = fixture();
  await ok.models.switchTo('sol');
  await ok.models.lastApply; assert.match(await ok.models.ready, /now uses GPT-6\.1 Sol/);
  assert.equal(ok.fetched[0][0], 'https://api.openai.com/v1/models/gpt-6.1-sol');
  assert.equal(ok.fetched[0][1].headers.authorization, 'Bearer sk-test');
});

test('an unreachable provider does not block the switch but says so; unknown names change nothing', async () => {
  const r = await checkAccess('openai/gpt-6.1-sol', {OPENAI_API_KEY: 'x'}, async () => { throw new Error('offline'); });
  assert.equal(r.ok, true); assert.match(r.warning, /could not reach/);
  assert.deepEqual(await checkAccess('anthropic/claude-opus-5-5', {}, async () => { throw new Error('no'); }), {ok: true, checked: false});
  const {models, calls} = fixture();
  assert.match(await models.switchTo('gpt4'), /Unknown model "gpt4"/);
  assert.equal(calls.length, 0);
});

test('commands are owner-only: /ai, /ai NAME and one-tap /ai_NAME', async () => {
  const {models} = fixture();
  const cmds = [];
  registerModelSwitch({registerCommand: c => cmds.push(c)}, models, ctx => { if (!ctx.owner) throw new Error('Only the owner.'); });
  assert.deepEqual(cmds.map(c => c.name), ['ai', 'ai_sonnet', 'ai_opus', 'ai_sol', 'ai_terra']);
  assert.match((await cmds[0].handler({owner: false})).text, /Only the owner/);
  assert.match((await cmds[0].handler({owner: true, args: ''})).text, /AI model for PROCURATOR/);
  assert.match((await cmds[0].handler({owner: true, args: 'opus'})).text, /Switching PROCURATOR to Claude Opus 5\.5/);
  await models.lastApply; assert.match(await models.ready, /now uses Claude Opus 5\.5/);
  assert.match((await cmds[1].handler({owner: true})).text, /Switching PROCURATOR to Claude Sonnet 5\.5/);
  await models.lastApply; assert.match(await models.ready, /now uses Claude Sonnet 5\.5/);
  assert.equal(Object.keys(PRESETS).length, 4);
});

test('a patch call that times out is judged by the saved config: applied = success, not applied = clear failure', async () => {
  // Live Nov 5: config.patch timed out after 30 s but may have been saved.
  let cfg = baseConfig(); const sends = [];
  const applied = new ModelSwitch(async (m, p) => {
    if (m === 'config.get') return {config: cfg, hash: 'h'};
    if (m === 'config.patch') { cfg = {...cfg, agents: {...cfg.agents, entries: {...cfg.agents.entries, main: {model: {primary: 'openai/gpt-6.1-terra', fallbacks: ['anthropic/claude-sonnet-5-5']}}}}}; throw new Error('gateway timeout after 180000ms'); }
    if (m === 'send') sends.push(p.message);
    return {};
  }, {env: {OPENAI_API_KEY: 'k'}, fetchImpl: async () => ({status: 200}), defer: fn => fn(), verifyTries: 2, verifyDelayMs: 1});
  await applied.switchTo('terra');
  await applied.lastApply; assert.match(await applied.ready, /now uses GPT-6\.1 Terra/);
  let cfg2 = baseConfig();
  const lost = new ModelSwitch(async (m) => { if (m === 'config.get') return {config: cfg2, hash: 'h'}; if (m === 'config.patch') throw new Error('gateway timeout'); return {}; },
    {env: {OPENAI_API_KEY: 'k'}, fetchImpl: async () => ({status: 200}), defer: fn => fn(), verifyTries: 2, verifyDelayMs: 1});
  await lost.switchTo('terra');
  assert.match(await lost.lastApply, /did not take effect \(gateway timeout\)\. PROCURATOR still uses Claude Sonnet 5\.5/);
  assert.equal(lost.pending, null, 'a later switch is not blocked');
});

test('0.24.0: /ai packets and /ai composer set the broker\'s choices (owner-only, access-checked); bare /ai shows all three', async () => {
  const {models} = fixture();
  const cmds = [], calls = [];
  let state = {composer: {model: 'sonnet', label: 'Claude Sonnet 5.5'}, packets: {model: 'main', label: 'same as the chat (/ai)'}};
  const labels = {sonnet: 'Claude Sonnet 5.5', opus: 'Claude Opus 5.5', sol: 'GPT-6.1 Sol', terra: 'GPT-6.1 Terra', main: 'same as the chat (/ai)'};
  const call = async (a, args, p) => { calls.push([a, args, p]); if (args?.set) for (const [k, v] of Object.entries(args.set)) state = {...state, [k]: {model: v, label: labels[v]}}; return state; };
  registerModelSwitch({registerCommand: c => cmds.push(c)}, models, ctx => { if (!ctx.owner) throw new Error('Only the owner.'); }, call);
  const ai = cmds[0];
  assert.match((await ai.handler({owner: false, args: 'packets sonnet'})).text, /Only the owner/);
  assert.equal(calls.length, 0);
  const s = (await ai.handler({owner: true, args: ''})).text;
  assert.match(s, /AI model for PROCURATOR/); assert.match(s, /Packet work sessions .*: same as the chat/); assert.match(s, /Packet Composer: Claude Sonnet 5\.5/);
  assert.match((await ai.handler({owner: true, args: 'packets sonnet'})).text, /now use Claude Sonnet 5\.5/);
  assert.deepEqual(calls.at(-1), ['model_settings', {set: {packets: 'sonnet'}}, true]);
  assert.match((await ai.handler({owner: true, args: 'composer opus'})).text, /Packet Composer now uses Claude Opus 5\.5/);
  assert.match((await ai.handler({owner: true, args: 'packets main'})).text, /same as the chat/);
  assert.match((await ai.handler({owner: true, args: 'composer gpt4'})).text, /Unknown model "gpt4"/);
  assert.match((await ai.handler({owner: true, args: 'composer'})).text, /Packet Composer: Claude Opus 5\.5/);
  // The chat switch is untouched by the scoped forms.
  assert.match((await ai.handler({owner: true, args: 'terra'})).text, /Switching PROCURATOR to GPT-6\.1 Terra/);
});

test('0.24.1: a GPT Composer needs the broker\'s own OpenAI key', async () => {
  const {models} = fixture();
  const cmds = [];
  let keys = {anthropic: true, openai: false};
  const call = async (a, args) => ({composer: {model: 'sonnet', label: 'Claude Sonnet 5.5'}, packets: {model: 'main', label: 'same as the chat (/ai)'}, keys, ...(args?.set ? {composer: {model: args.set.composer, label: 'GPT-6.1 Terra'}} : {})});
  registerModelSwitch({registerCommand: c => cmds.push(c)}, models, () => {}, call);
  assert.match((await cmds[0].handler({owner: true, args: 'composer terra'})).text, /bin\/set-web-key\.sh openai/);
  assert.match((await cmds[0].handler({owner: true, args: 'packets terra'})).text, /now use/);      // packet sessions run in the gateway, which has the key
  keys = {anthropic: true, openai: true};
  assert.match((await cmds[0].handler({owner: true, args: 'composer terra'})).text, /Packet Composer now uses GPT-6\.1 Terra/);
});

test('0.24.3: a restart that ends the process is finished by the next one, and Ready is sent exactly once', async () => {
  const fs = await import('node:fs'), os = await import('node:os'), path = await import('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'models-'));
  const a = fixture();a.models.stateDir = dir;
  await a.models.switchTo('opus');await a.models.lastApply;
  // A second process (after the restart) starts while the first is still waiting: only one Ready goes out.
  const b = fixture({config: a.cfg()});b.models.stateDir = dir;
  const [x, y] = await Promise.all([a.models.ready, b.models.resume()]);
  assert.equal([x, y].filter(Boolean).length, 1);
  assert.equal([...a.sent(), ...b.sent()].filter(m => /^Ready:/.test(m)).length, 1);
  assert.equal(fs.existsSync(path.join(dir, 'model-switch-pending.json')), false);
  assert.equal(b.models.resume(), null, 'nothing pending any more');
});
