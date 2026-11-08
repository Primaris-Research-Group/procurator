import test from 'node:test';
import assert from 'node:assert/strict';
import { registerSocial, SOCIAL_TOOLS } from './social.mjs';
import { gateTool, loadPolicy } from './connector-gate.mjs';
import { normalizeOwner, isOwner, canApprove } from './owner-config.mjs';

function setup(answers = {}) {
  const tools = [], commands = [], calls = [];
  const call = async (action, args, priv) => { calls.push([action, args, !!priv]); const a = answers[action]; return typeof a === 'function' ? a(args) : a ?? {}; };
  registerSocial({ registerTool: t => tools.push(t), registerCommand: c => commands.push(c) }, call, ctx => { if (!ctx.owner) throw new Error('Only the owner.'); },
    { run: async () => '{}', write: () => {} , inbox: '/tmp' });
  const cmd = name => async (args, owner = true) => (await commands.find(c => c.name === name).handler({ args, owner })).text;
  return { tools, commands, calls, cmd };
}

test('every social tool is declared in the policy with its risk and audience', () => {
  const pol = loadPolicy();
  const { tools } = setup();
  for (const t of tools) assert.ok(pol.tools[t.name], t.name + ' missing from connector-policy.json');
  assert.equal(pol.tools.social_post_draft.audience, 'public');
  assert.equal(pol.tools.social_post_draft.account, 'account');
  assert.equal(pol.tools.social_post_review.risk, 'public');
  assert.equal(SOCIAL_TOOLS.length + 1, tools.length);
});

test('/postsend takes only the exact review line, owner only, and reports honestly', async () => {
  const { cmd, calls } = setup({ social_post_send: a => a.id === 'p_aaaaaaaaaaaa' ? { status: 'posted', account: 'acme-engineering', provider: 'mock-social', url: 'https://x/1' } : { status: 'uncertain', id: a.id, provider: 'mock-social' } });
  assert.match(await cmd('postsend')('p_aaaaaaaaaaaa'), /ENTIRE line/);
  assert.match(await cmd('postsend')('p_aaaaaaaaaaaa 0123456789abcdef', false), /Only the owner/);
  assert.match(await cmd('postsend')('p_aaaaaaaaaaaa 0123456789abcdef'), /Posted as acme-engineering/);
  assert.match(await cmd('postsend')('p_bbbbbbbbbbbb 0123456789abcdef'), /may have been published\. Do not resend/);
  assert.deepEqual(calls.filter(c => c[0] === 'social_post_send').map(c => c[2]), [true, true]);
});

test('/eventroute and /connections call owner actions', async () => {
  const { cmd, calls } = setup({ events_route: a => a.list ? { routes: [] } : { route: `${a.source} ${a.kind} -> ${a.mission}`, note: 'n' },
    oauth_list: { grants: [], accounts: [], providers: [{ id: 'linkedin', status: 'planned' }] } });
  assert.match(await cmd('eventroute')(''), /No event routes/);
  assert.match(await cmd('eventroute')('slack dm replies'), /slack dm -> replies/);
  assert.match(await cmd('connections')(''), /linkedin \(planned\)/);
  assert.ok(calls.every(c => c[2]));
});

test('the gate sends the audience with outbound text; a public hard block refuses', async () => {
  const pol = loadPolicy();
  const seen = [];
  const tool = gateTool({ name: 'social_post_draft', execute: async () => ({ content: [{ type: 'text', text: 'drafted' }] }) }, pol,
    { check: async a => { seen.push(a); return { tainted: true, allowed: false, hard_block: true, reason: 'Health details never go there.' }; } });
  const r = await tool.execute('1', { request_id: 'r1234567', provider: 'mock-social', account: 'me', text: 'about my visit' });
  assert.equal(r.isError, true);
  assert.match(r.content[0].text, /never go there/);
  assert.equal(seen[0].audience, 'public');
});

test('owner.json schema 2: channels, schema 1 read, approvals Telegram-only', () => {
  const o = normalizeOwner({ telegram_id: 42 });
  assert.deepEqual(o.channels, [{ type: 'telegram', id: '42', primary: true }]);
  const two = normalizeOwner({ channels: [{ type: 'telegram', id: '1', primary: true }, { type: 'slack', team: 'T1', id: 'U1' }] });
  assert.ok(isOwner(two, 'slack', 'U1', 'T1'));
  assert.ok(!isOwner(two, 'slack', 'U1', 'T9'));
  assert.ok(!canApprove(two, 'slack', 'U1', 'T1'));
  assert.ok(canApprove(two, 'telegram', 1));
  assert.throws(() => normalizeOwner({ channels: [{ type: 'slack', team: 'T', id: 'U', primary: true }] }), /primary channel is telegram/);
});
