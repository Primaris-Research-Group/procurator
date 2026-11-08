import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerAccounts, ACCOUNTS_LINE} from './accounts.mjs';

test('accounts tools: four read/own_write tools that forward to the broker and never carry a password', async () => {
  const calls = [], tools = [];
  const call = async (a, args, p = false) => { calls.push([a, args, p]); if (a === 'accounts_list') return { accounts: [{ provider: 'icloud', email: 'u@icloud.com', services: { mail: 'ok', calendar: 'ok' } }] }; return { ok: true, action: a }; };
  registerAccounts({ registerTool: t => tools.push(t) }, call, () => {});
  assert.deepEqual(tools.map(t => t.name), ['accounts_list', 'calendar_apple_events', 'contacts_apple_search', 'calendar_apple_create_event']);
  const listed = await tools[0].execute('1', {});
  assert.match(listed.content[0].text, /u@icloud.com/); assert.doesNotMatch(listed.content[0].text, /password/i);
  await tools[1].execute('2', { days: '3' }); assert.deepEqual(calls.at(-1), ['calendar_apple_events', { days: '3' }, false]);
  await tools[2].execute('3', { query: 'ana' }); assert.deepEqual(calls.at(-1), ['contacts_apple_search', { query: 'ana' }, false]);
  const create = tools[3];
  assert.deepEqual(create.parameters.required, ['summary', 'start']);
  assert.ok(!('attendees' in create.parameters.properties), 'no attendee parameter: invites are a mail step');
  await create.execute('4', { summary: 'Dentist', start: '2026-11-20T09:00:00-05:00' });
  assert.equal(calls.at(-1)[0], 'calendar_apple_create_event');
  const failing = async () => { throw new Error('No Apple account with calendar is connected.'); };
  const t2 = []; registerAccounts({ registerTool: t => t2.push(t) }, failing, () => {});
  const r = await t2[1].execute('5', {});
  assert.equal(r.isError, true); assert.match(r.content[0].text, /No Apple account/);
  assert.ok(!/`/.test(ACCOUNTS_LINE));
});
