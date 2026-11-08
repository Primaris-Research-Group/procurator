import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {OwnerProfile} from './profile.mjs';
import {Welcome, registerWelcome, PRONOUNS} from './welcome.mjs';

const setup = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'welcome-'));
  const profile = new OwnerProfile(dir);
  return {dir, profile, w: new Welcome(dir, profile, () => new Date('2026-11-08T20:00:00Z'))};
};
const fakeApi = () => {
  const tools = {}, hooks = {}, cmds = {};
  return {tools, hooks, cmds, api: {
    registerTool(f, o) { tools[o.name] = f; }, on(n, f) { (hooks[n] ||= []).push(f); }, registerCommand(c) { cmds[c.name] = c; }}};
};
const turn = (hooks) => hooks.before_prompt_build[0]({}, {agentId: 'main'});

test('an existing box is never interrupted: no welcome.json, no interview', () => {
  const {w} = setup();
  assert.equal(w.pending(), false);
  const f = fakeApi(); registerWelcome(f.api, w, () => {});
  assert.equal(turn(f.hooks), undefined);
});

test('setup marks it pending; the guidance asks one question at a time in order and saves only what was said', async () => {
  const {dir, w} = setup();
  fs.writeFileSync(path.join(dir, 'welcome.json'), JSON.stringify({status: 'pending'}));
  const f = fakeApi(); registerWelcome(f.api, w, () => {});
  const ctx = turn(f.hooks).appendContext;
  assert.match(ctx, /WELCOME INTERVIEW/);
  assert.match(ctx, /ONE short question per message/);
  assert.ok(ctx.indexOf('what they would like to be called') < ctx.indexOf('he/him, she/her or they/them'));
  const tool = f.tools.owner_welcome_save({agentId: 'main'});
  let r = JSON.parse((await (tool.execute('1', {preferred_name: 'Maria', pronouns: 'she/her'}))).content[0].text);
  assert.deepEqual(r.saved, ['preferred_name', 'pronouns']);
  assert.equal(r.status, 'pending');
  assert.deepEqual(r.still_to_ask, ['city_or_timezone', 'occupation', 'help_with', 'email']);
  const after = turn(f.hooks);
  assert.match(after.appendSystemContext, /call them "Maria"; refer to them as she\/her \(her\)/);
  assert.doesNotMatch(after.appendContext, /would like to be called/, 'answered questions are not asked again');
  r = JSON.parse((await (tool.execute('2', {city: 'Chicago', timezone: 'America/Chicago', occupation: 'Family physician',
    help_with: 'insurance calls and prior authorizations', email: 'maria@example.com', done: true}))).content[0].text);
  assert.equal(r.status, 'done');
  const done = turn(f.hooks);
  assert.equal(done.appendContext, undefined, 'no more interview once done');
  assert.match(done.appendSystemContext, /work: Family physician; wants help with: insurance calls/);
});

test('bad answers are refused and nothing is guessed', () => {
  const {dir, w} = setup();
  fs.writeFileSync(path.join(dir, 'welcome.json'), JSON.stringify({status: 'pending'}));
  assert.throws(() => w.save({pronouns: 'xe/xem'}), /he\/him, she\/her or they\/them/);
  assert.throws(() => w.save({timezone: 'Mars/Olympus'}), /timezone/);
  assert.throws(() => w.save({email: 'not-an-email'}), /email/);
  assert.throws(() => w.save({help_with: 'a\u0000b'}), /plain-text/);
  assert.deepEqual(Object.keys(PRONOUNS), ['he/him', 'she/her', 'they/them']);
});

test('/welcome restarts the interview, owner only', async () => {
  const {w} = setup();
  const f = fakeApi();
  registerWelcome(f.api, w, ctx => { if (!ctx.owner) throw new Error('owner only'); });
  assert.match((await f.cmds.welcome.handler({owner: false})).text, /owner only/);
  assert.equal(w.pending(), false);
  assert.match((await f.cmds.welcome.handler({owner: true})).text, /Welcome interview on/);
  assert.equal(w.pending(), true);
});

