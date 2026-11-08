import {ownerId} from './owner-config.mjs';
import fs from 'node:fs';
import path from 'node:path';
// AI model switching from Telegram (Nov 5 2026): /ai shows the main assistant's model and the choices; /ai NAME or the
// one-tap /ai_NAME switches. The switch is checked first (the provider API must list the model for the server's key),
// then written as the MAIN agent's default (agents.entries.main.model) with a fallback model, and the main chat's
// session pin is cleared so the change applies from the next message. The phone agent, call summaries and the
// realtime voice are not changed. OpenClaw's own /model command still works; /ai is the guarded shortcut.
export const PRESETS = {
  sonnet: {ref: 'anthropic/claude-sonnet-5-5', label: 'Claude Sonnet 5.5', note: 'the default: fast, strong with tools · $2 in / $10 out per million tokens', fallback: 'anthropic/claude-opus-5-5',
    params: {anthropicServerCompaction: true, anthropicCompactThreshold: 80000}},
  opus: {ref: 'anthropic/claude-opus-5-5', label: 'Claude Opus 5.5', note: 'strongest Claude, slower · $4 in / $20 out per million tokens (about 2x Sonnet)', fallback: 'anthropic/claude-sonnet-5-5',
    params: {anthropicServerCompaction: true, anthropicCompactThreshold: 80000}},
  sol: {ref: 'openai/gpt-6.1-sol', label: 'GPT-6.1 Sol', note: 'OpenAI flagship · $2 in / $10 out per million tokens (double above 272K input) · billed to your OpenAI account', fallback: 'anthropic/claude-sonnet-5-5',
    params: {responsesServerCompaction: true, responsesCompactThreshold: 80000}, runtime: 'openclaw'},
  // GPT-6.1 Terra (Oct 26 2026) is newer than OpenClaw 2026.10.9's catalog; OpenClaw runs listed-but-uncatalogued
  // OpenAI models with conservative defaults (128K context), which the 80K compaction threshold stays under (0.20.4: 120K before).
  terra: {ref: 'openai/gpt-6.1-terra', label: 'GPT-6.1 Terra', note: 'OpenAI balanced tier, half the price of Sol · $1 in / $5 out per million tokens · billed to your OpenAI account', fallback: 'anthropic/claude-sonnet-5-5',
    params: {responsesServerCompaction: true, responsesCompactThreshold: 80000}, runtime: 'openclaw'},
};
const MAIN_SESSION = 'agent:main:main';
const OWNER = ownerId();
const nameOf = ref => Object.keys(PRESETS).find(k => PRESETS[k].ref === ref);
const labelOf = ref => PRESETS[nameOf(ref)]?.label ?? ref;
const primaryOf = m => typeof m === 'string' ? m : m?.primary;

export function currentModel(config) {
  const entry = config?.agents?.entries?.main?.model;
  return {ref: primaryOf(entry) || primaryOf(config?.agents?.defaults?.model) || 'unknown', fallbacks: (typeof entry === 'object' && Array.isArray(entry?.fallbacks)) ? entry.fallbacks : [], explicit: !!primaryOf(entry)};
}

// Ask the provider whether this server's key can use the model. Never prints or returns the key.
export async function checkAccess(ref, env = process.env, fetchImpl = globalThis.fetch) {
  const [provider, id] = [ref.slice(0, ref.indexOf('/')), ref.slice(ref.indexOf('/') + 1)];
  const req = provider === 'openai' ? (env.OPENAI_API_KEY ? {url: `https://api.openai.com/v1/models/${encodeURIComponent(id)}`, headers: {authorization: `Bearer ${env.OPENAI_API_KEY}`}} : null)
    : provider === 'anthropic' ? (env.ANTHROPIC_API_KEY ? {url: `https://api.anthropic.com/v1/models/${encodeURIComponent(id)}`, headers: {'x-api-key': env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'}} : null) : null;
  if (!req) return provider === 'openai' ? {ok: false, reason: 'The server has no OpenAI API key for text models (the phone voice key is OPENAI_API_KEY; it is missing from the gateway). Nothing changed.'}
    : {ok: true, checked: false};
  try {
    const r = await fetchImpl(req.url, {headers: req.headers, signal: AbortSignal.timeout(12000)});
    if (r.status === 200) return {ok: true, checked: true};
    if (r.status === 404) return {ok: false, reason: `${labelOf(ref)} is not available to your ${provider === 'openai' ? 'OpenAI' : 'Anthropic'} account (the API does not list ${id}). Nothing changed.`};
    if (r.status === 401 || r.status === 403) return {ok: false, reason: `The ${provider === 'openai' ? 'OpenAI' : 'Anthropic'} key on the server was refused (${r.status}). Nothing changed.`};
    return {ok: true, checked: false, warning: `could not confirm access (HTTP ${r.status}); switched anyway with a fallback`};
  } catch (e) {
    return {ok: true, checked: false, warning: 'could not reach the provider to confirm access; switched anyway with a fallback'};
  }
}

export function patchFor(name, config) {
  const p = PRESETS[name];
  const entry = {...(config?.agents?.defaults?.models?.[p.ref] ?? {})};
  entry.alias ??= name;
  entry.params = {...p.params, ...(entry.params ?? {})};
  if (p.runtime && !entry.agentRuntime) entry.agentRuntime = {id: p.runtime};
  return {agents: {entries: {main: {model: {primary: p.ref, fallbacks: [p.fallback]}}}, defaults: {models: {[p.ref]: entry}}}};
}

// 0.24.3 (B10): a saved model change restarts the gateway, and this plugin restarts with it, so "Switched" used to arrive
// while commands were still queued for minutes. Now the reply says the change is saved and a restart is under way; "Ready"
// is sent only once the gateway answers again on the new model — by this process if it survives, or by the restarted one
// (a marker file in stateDir carries the pending switch across the restart; renaming it claims it, so Ready is sent once).
export class ModelSwitch {
  constructor(gateway, {env = process.env, fetchImpl = globalThis.fetch, defer = fn => setTimeout(fn, 100), verifyTries = 8, verifyDelayMs = 5000,
                        stateDir = null, readyDelayMs = 45000, readyEveryMs = 10000, readyTries = 30} = {}) {
    this.gateway = gateway; this.env = env; this.fetchImpl = fetchImpl; this.defer = defer; this.verifyTries = verifyTries; this.verifyDelayMs = verifyDelayMs; this.pending = null;
    this.stateDir = stateDir; this.readyDelayMs = readyDelayMs; this.readyEveryMs = readyEveryMs; this.readyTries = readyTries;
  }
  markerPath() { return this.stateDir ? path.join(this.stateDir, 'model-switch-pending.json') : null; }
  // True when the gateway answers a session read and the saved config names the model.
  async answersOn(ref) {
    try { await this.gateway('sessions.describe', {key: MAIN_SESSION}, 15000); return currentModel((await this.config()).config).ref === ref; } catch { return false; }
  }
  claim() {
    const m = this.markerPath();
    if (!m) return true;
    try { const tmp = m + '.' + process.pid + '.' + Date.now(); fs.renameSync(m, tmp); const v = JSON.parse(fs.readFileSync(tmp, 'utf8')); try { fs.unlinkSync(tmp); } catch {} return v; }
    catch { return null; }
  }
  // Wait for the restart to finish, then send Ready once. Returns the message sent (or null if another process sent it).
  async confirmReady(info) {
    const p = PRESETS[info.key];
    await new Promise(r => setTimeout(r, this.readyDelayMs));
    let ok = false;
    for (let i = 0; i < this.readyTries && !ok; i++) {
      ok = await this.answersOn(p.ref) && (await new Promise(r => setTimeout(r, Math.min(this.readyEveryMs, 3000))), await this.answersOn(p.ref));
      if (!ok) await new Promise(r => setTimeout(r, this.readyEveryMs));
    }
    const claimed = this.claim();
    if (!claimed) return null;
    const msg = ok ? [`Ready: PROCURATOR is answering again and now uses ${p.label}${info.from ? ` (was ${labelOf(info.from)})` : ''}.`, p.note,
      `If ${p.label} fails, it answers with ${labelOf(p.fallback)} instead.`, ...(info.notes || []),
      'For a clean start on the new model, send /new. Back to the default: /ai_sonnet'].join('\n')
      : `The switch to ${p.label} is saved, but PROCURATOR has not answered normally yet after its restart. Replies may still be delayed; send /ai to check.`;
    await this.notify(msg, info.key + '-ready').catch(() => {});
    return msg;
  }
  // Called once at startup: finish a switch whose restart interrupted the previous process.
  resume() {
    const m = this.markerPath();
    if (!m || !fs.existsSync(m)) return null;
    let info; try { info = JSON.parse(fs.readFileSync(m, 'utf8')); } catch { try { fs.unlinkSync(m); } catch {} return null; }
    if (!PRESETS[info?.key] || Date.now() - (info.at || 0) > 3600000) { try { fs.unlinkSync(m); } catch {} return null; }
    return this.confirmReady(info);
  }
  async config() { const r = await this.gateway('config.get', {}); return {config: r?.config ?? r?.parsed ?? {}, hash: r?.hash}; }
  async status() {
    const {config} = await this.config(), cur = currentModel(config);
    const lines = [`AI model for PROCURATOR: ${labelOf(cur.ref)}` + (cur.fallbacks.length ? ` (fallback: ${cur.fallbacks.map(labelOf).join(', ')})` : ''), '', 'Tap to switch (applies from your next message and stays after the nightly reset):'];
    for (const [k, p] of Object.entries(PRESETS)) lines.push(`/ai_${k} — ${p.label}${p.ref === cur.ref ? ' (current)' : ''}: ${p.note}`);
    lines.push('', 'Phone calls keep their own voice and summary models. Missions, the morning brief and reminders follow this setting.');
    return lines.join('\n');
  }
  // Nov 5 live test: awaiting config.patch inside the command handler timed out after 30 s. The gateway applies a
  // config write only after in-flight work settles, and this command was that work. So the handler checks access,
  // replies at once, and applies the change in the background, then confirms by reading the saved config back.
  async switchTo(name) {
    const key = String(name ?? '').trim().toLowerCase();
    if (!PRESETS[key]) return `Unknown model "${name}". Choices: ${Object.keys(PRESETS).join(', ')}. Send /ai to see them.`;
    if (this.pending) return `Still switching to ${PRESETS[this.pending].label}; I'll confirm here in a moment.`;
    const p = PRESETS[key];
    const {config} = await this.config();
    const cur = currentModel(config);
    if (cur.ref === p.ref && cur.explicit && cur.fallbacks[0] === p.fallback) return `PROCURATOR already uses ${p.label}.`;
    const access = await checkAccess(p.ref, this.env, this.fetchImpl);
    if (!access.ok) return `Not switched: ${access.reason}`;
    this.pending = key;
    this.lastApply = new Promise(resolve => this.defer(() => {
      this.apply(key, cur, access)
        .catch(e => `Model switch to ${p.label} failed: ${String(e?.message || e).slice(0, 200)}. Send /ai to see the current model.`)
        .then(msg => this.notify(msg, key).catch(() => {}).then(() => msg))
        .finally(() => { this.pending = null; })
        .then(resolve);
    }));
    return `Switching PROCURATOR to ${p.label}${access.checked ? ' (your account has access)' : ''}… I'll confirm here in a few seconds.`;
  }
  async apply(key, cur, access) {
    const p = PRESETS[key];
    let error;
    try {
      const {config, hash} = await this.config();
      await this.gateway('config.patch', {raw: JSON.stringify(patchFor(key, config)), ...(hash ? {baseHash: hash} : {}), replacePaths: ['agents.entries.main.model.fallbacks'], note: `/ai ${key}`}, 180000);
    } catch (e) { error = e; }
    // Whatever the patch call returned, the saved config decides.
    let now = null;
    for (let i = 0; i < this.verifyTries; i++) {
      try { now = currentModel((await this.config()).config); if (now.ref === p.ref) break; } catch {}
      await new Promise(r => setTimeout(r, this.verifyDelayMs));
    }
    if (now?.ref !== p.ref) return `Model switch to ${p.label} did not take effect${error ? ` (${String(error.message || error).slice(0, 160)})` : ''}. PROCURATOR still uses ${labelOf(now?.ref ?? cur.ref)}.`;
    const pin = await this.clearPin();
    const notes = [...(access.warning ? ['Note: ' + access.warning + '.'] : []),
      ...(pin ? [] : ['This chat may still be pinned to an earlier /model choice; send /model default if replies keep using it.'])];
    const info = {key, from: cur.ref, at: Date.now(), notes};
    const m = this.markerPath();
    if (m) try { fs.mkdirSync(path.dirname(m), {recursive: true}); fs.writeFileSync(m, JSON.stringify(info)); } catch {}
    this.ready = this.confirmReady(info).catch(() => null);
    return [`Saved: PROCURATOR will use ${p.label} (was ${labelOf(cur.ref)}).`,
      'It restarts to load the new model, so replies and commands can pause for a few minutes. I\'ll send "Ready" here when it answers again.'].join('\n');
  }
  async notify(message, key) {
    let accountId;
    try { const s = (await this.gateway('sessions.describe', {key: MAIN_SESSION}))?.session; const r = s?.deliveryContext; if ((r?.channel ?? s?.lastChannel) === 'telegram' && String(r?.to ?? s?.lastTo ?? '').replace(/^telegram:/, '') === OWNER) accountId = r?.accountId; } catch {}
    await this.gateway('send', {channel: 'telegram', to: OWNER, ...(accountId ? {accountId} : {}), message, idempotencyKey: `procurator-ai:${key}:${Date.now()}`});
  }
  async clearPin() { try { await this.gateway('sessions.patch', {key: MAIN_SESSION, model: null}, 60000); return true; } catch { return false; } }
}

// 0.24.0: two more choices, kept by the broker (web/model_settings.py) because the broker runs them:
//   /ai composer MODEL  the Packet Composer (default Claude Sonnet 5.5)
//   /ai packets MODEL   packet work sessions for packets WITHOUT their own Model line (default main = the chat's /ai model)
// MODEL is sonnet | opus | sol | terra | main. A packet's own Model line (/mission model NAME MODEL) always wins.
export const SCOPES = {composer: 'Packet Composer', packets: 'Packet work sessions (packets without their own Model)'};
export async function scopedStatus(call) {
  try {
    const d = await call('model_settings', {}, true);
    return ['', `${SCOPES.packets}: ${d.packets.label} — /ai packets MODEL`, `${SCOPES.composer}: ${d.composer.label} — /ai composer MODEL`,
      'MODEL: sonnet · opus · sol · terra · main (same as the chat). A packet\'s own Model line always wins (/mission model NAME MODEL).'].join('\n');
  } catch { return ''; }
}
export async function switchScope(call, scope, name, env = process.env, fetchImpl = globalThis.fetch) {
  const key = String(name ?? '').trim().toLowerCase();
  if (!key) return (await scopedStatus(call)).trim();
  if (key !== 'main' && !PRESETS[key]) return `Unknown model "${name}". Choices for ${SCOPES[scope]}: sonnet, opus, sol, terra, main.`;
  if (key !== 'main') {
    const access = await checkAccess(PRESETS[key].ref, env, fetchImpl);
    if (!access.ok) return `Not switched: ${access.reason}`;
    // The Composer is called by the server's browser broker, not the gateway: it needs its own OpenAI key for GPT models.
    if (scope === 'composer' && PRESETS[key].ref.startsWith('openai/')) {
      const now = await call('model_settings', {}, true).catch(() => null);
      if (now?.keys && !now.keys.openai) return `Not switched: the Packet Composer runs on the server's broker, which has no OpenAI key yet. Add it once on the Mac with: bash bin/set-web-key.sh openai — then send /ai composer ${key} again.`;
    }
  }
  const d = await call('model_settings', {set: {[scope]: key}}, true);
  const label = d[scope].label;
  return scope === 'packets'
    ? `${SCOPES.packets} now use ${label}. Packets with their own Model line keep it.` + (key === 'main' ? '' : ' They now run in their own session (not your chat); a browser task one of them starts continues in your main chat after you approve it, as for packets with a Model line.')
    : `The Packet Composer now uses ${label} for new replies (a session can still pick its own model on its page).`;
}

export function registerModelSwitch(api, models, ownerContext, call = null) {
  const run = fn => async ctx => { try { ownerContext(ctx); return {text: await fn(ctx)}; } catch (e) { return {text: 'Model switch failed: ' + String(e?.message || e) + ' Nothing else changed; /ai shows the current model.'}; } };
  api.registerCommand?.({name: 'ai', description: 'Show or switch AI models: /ai, /ai sonnet|opus|sol|terra (chat) · /ai packets MODEL · /ai composer MODEL', channels: ['telegram'], requireAuth: true, acceptsArgs: true,
    handler: run(async ctx => {
      const parts = String(ctx.args ?? '').trim().split(/\s+/).filter(Boolean);
      if (call && parts[0] && SCOPES[parts[0].toLowerCase()]) return switchScope(call, parts[0].toLowerCase(), parts[1], models.env, models.fetchImpl);
      if (parts[0]) return models.switchTo(parts[0]);
      return (await models.status()) + (call ? await scopedStatus(call) : '');
    })});
  for (const [k, p] of Object.entries(PRESETS)) api.registerCommand?.({name: 'ai_' + k, description: 'Switch the AI model to ' + p.label, channels: ['telegram'], requireAuth: true, acceptsArgs: false,
    handler: run(() => models.switchTo(k))});
  return models;
}
