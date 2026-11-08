// Owner identity file, schema 2 (0.27.0): the plugin-side reader of config/owner.json. Same rules as
// integrations/web/owner_config.py: channels:[{type,id,primary,team|tenant}], one primary channel and it is telegram,
// schema 1 ({telegram_id}) read as one primary telegram channel. Approvals stay Telegram-only.
import fs from 'node:fs';

export const OWNER_FILE = process.env.PROCURATOR_OWNER_FILE || '/opt/procurator/config/owner.json';
const TYPES = new Set(['telegram', 'slack', 'teams']);

export function normalizeOwner(raw) {
  const fail = m => { throw new Error('owner.json: ' + m); };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('not an object');
  const d = { ...raw };
  if (!d.channels && d.telegram_id != null) d.channels = [{ type: 'telegram', id: String(d.telegram_id), primary: true }];
  delete d.telegram_id;
  if (!Array.isArray(d.channels) || d.channels.length < 1 || d.channels.length > 6) fail('channels is a list of 1-6 chat channels');
  const seen = new Set();
  d.channels = d.channels.map(c => {
    if (!c || !TYPES.has(c.type)) fail('each channel has a type: telegram, slack, teams');
    const id = String(c.id ?? '');
    if (!/^[A-Za-z0-9:_.@-]{1,80}$/.test(id)) fail('channel id is not a plain id');
    if (c.type === 'telegram' && !/^\d+$/.test(id)) fail('a telegram channel id is the numeric user id');
    const scope = c.type === 'slack' ? 'team' : c.type === 'teams' ? 'tenant' : null;
    if (scope && !(typeof c[scope] === 'string' && c[scope])) fail(`a ${c.type} channel names its ${scope}`);
    const key = `${c.type}|${id}|${c.team || c.tenant || ''}`;
    if (seen.has(key)) fail('a channel is listed twice');
    seen.add(key);
    const out = { type: c.type, id, primary: !!c.primary };
    for (const k of ['team', 'tenant', 'label']) if (c[k] != null) out[k] = String(c[k]);
    return out;
  });
  const prim = d.channels.filter(c => c.primary);
  if (prim.length !== 1) fail('exactly one channel is primary');
  if (prim[0].type !== 'telegram') fail('the primary channel is telegram (approvals are Telegram-only in this release)');
  d.schema = 2;
  return d;
}

export function loadOwner(file = OWNER_FILE, read = f => fs.readFileSync(f, 'utf8')) { return normalizeOwner(JSON.parse(read(file))); }

export function isOwner(owner, type, id, team) {
  return owner.channels.some(c => c.type === type && c.id === String(id) && (type === 'telegram' || (c.team || c.tenant) === team));
}

export const canApprove = (owner, type, id, team) => type === 'telegram' && isOwner(owner, type, id, team);
export const primaryChannel = owner => owner.channels.find(c => c.primary);

// The owner's Telegram user id for owner checks and notifications: PROCURATOR_OWNER_ID (set by the installer / Setup pages, passed
// into the gateway container by compose.web.yaml), else the primary channel in owner.json, else '' (every owner check fails closed).
export function ownerId() {
  const env = String(process.env.PROCURATOR_OWNER_ID || '').trim();
  if (/^\d+$/.test(env)) return env;
  try { return String(primaryChannel(loadOwner()).id); } catch { /* fall through */ }
  // Under `node --test` with no configured owner, the fixture id the test suites use; never set outside the test runner.
  return process.env.NODE_TEST_CONTEXT ? '100000001' : '';
}
