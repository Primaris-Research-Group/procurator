import {ownerId} from './owner-config.mjs';
// Code builds on the owner's own machines (Nov 5 2026): their always-on Windows PC (WSL2) and their Mac. The assistant
// writes a build packet, the owner approves the exact packet with /buildapprove, a builder pulls it over its own restricted
// SSH key, runs Claude Code or Codex with their subscriptions on a fresh clone, runs the repo's own test command from
// that machine's config, and opens a DRAFT PR. The droplet never runs anything on their machines; nothing is merged or
// deployed. Files (state/procurator-build):
//   jobs.json, queue/<id>.json, cancel/<id>, notes/<id>/   this plugin
//   claims/, reports/, builders/<name>.json, progress/<id>.jsonl   build-relay.py (the name comes from the builder's SSH key)
// Live builds (0.31.0, Nov 8): a packet can be a BUILD.md (parsePacketMarkdown) and name the model; the builder streams
// progress, the owner steers with /buildnote ID text (queued in notes/, delivered once by the relay), a QUESTION from the
// agent puts the build in waiting_owner and reaches the owner at once; /buildlog ID shows the recent progress.
import fs from 'node:fs';
import {edition} from './connector-gate.mjs';
import path from 'node:path';
import crypto from 'node:crypto';

export const OWNER = ownerId();
const OWNER_SESSION = 'agent:main:main';
const REPO = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/;
const BRANCH = /^[A-Za-z0-9._/-]{1,100}$/;
const PATHSPEC = /^[A-Za-z0-9._/*-]{1,200}$/;
const PR = /^https:\/\/github\.com\/[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}\/pull\/\d{1,7}$/;
const ID = /^[a-f0-9]{16}$/;
const NAME = /^[a-z0-9-]{1,20}$/;
const MODEL = /^[A-Za-z0-9._:/-]{2,64}$/;
const REVIEWERS = ['auto', 'claude', 'codex', 'off'];
const PENDING_MS = 30 * 60000, ONLINE_MS = 3 * 60000, OFFLINE_ALERT_MS = 10 * 60000, QUEUE_MAX_MS = 24 * 3600000;
const AGENTS = {claude: 'Claude Code', codex: 'Codex'};
const ACTIVE = ['queued', 'claimed', 'preparing', 'running', 'waiting_owner', 'testing', 'pushing', 'cancel_requested'];
const FINAL = ['done', 'failed', 'cancelled', 'expired', 'rejected'];
const WORKING = ['claimed', 'preparing', 'running', 'waiting_owner', 'testing', 'pushing'];
const PROGRESS_DIGEST_MS = 2 * 60000;

const digest = v => crypto.createHash('sha256').update(JSON.stringify(v)).digest('hex').slice(0, 16);
function atomic(file, value) {
  const tmp = file + '.' + crypto.randomBytes(6).toString('hex') + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(value), {mode: 0o600});
  fs.renameSync(tmp, file);
}
const clean = (v, max) => String(v ?? '').replace(/[\u0000-\u0008\u000b-\u001f\u007f\u200b-\u200f\u2028\u2029\ufeff]/g, '').trim().slice(0, max);
const ago = ms => ms < 90000 ? 'just now' : ms < 3600000 ? Math.round(ms / 60000) + ' min ago' : ms < 172800000 ? Math.round(ms / 3600000) + ' h ago' : Math.round(ms / 86400000) + ' days ago';
const listOr = (xs, none) => xs.length ? xs.join(', ') : none;
const where = b => 'your ' + (b?.label || b?.name || 'builder');

// builders: [{name,label,online,repos,agents,default_agent,branches}] from builders/*.json
// A BUILD.md packet (0.31.0): optional front matter between --- lines (repo, base, agent, model, builder, paths, max_minutes),
// the first "# " heading as the title, "## Goal" as the goal, "## Acceptance" / "## Done when" bullets as checks, any other
// section appended to the goal. Explicit tool arguments win over the file.
export function parsePacketMarkdown(md) {
  const text = String(md ?? '').replace(/\r\n?/g, '\n');
  if (text.length > 20000) throw new Error('BUILD.md is longer than 20,000 characters.');
  const out = {};
  let body = text;
  const fm = /^---\n([\s\S]*?)\n---\n?/.exec(text);
  if (fm) {
    body = text.slice(fm[0].length);
    for (const line of fm[1].split('\n')) {
      const m = /^([A-Za-z_]+)\s*:\s*(.*)$/.exec(line.trim());
      if (!m) continue;
      const key = m[1].toLowerCase(), raw = m[2].replace(/\s+#.*$/, '').trim();
      const value = raw.replace(/^["']|["']$/g, '');
      if (key === 'paths' || key === 'acceptance') out[key] = value.replace(/^\[|\]$/g, '').split(',').map(x => x.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
      else if (key === 'max_minutes') out.max_minutes = Number(value);
      else if (['repo', 'base', 'agent', 'model', 'review', 'review_model', 'builder', 'title', 'mission', 'step', 'request_id'].includes(key)) out[key] = value;
    }
  }
  const sections = {};
  let current = '_', title = null;
  for (const line of body.split('\n')) {
    const h1 = /^#\s+(.+)$/.exec(line), h2 = /^##\s+(.+)$/.exec(line);
    if (h1 && title === null) { title = h1[1].trim(); continue; }
    if (h2) { current = h2[1].trim().toLowerCase(); sections[current] ??= []; continue; }
    (sections[current] ??= []).push(line);
  }
  if (title && !out.title) out.title = title;
  const take = names => { for (const n of Object.keys(sections)) if (names.some(x => n.startsWith(x))) { const v = sections[n].join('\n').trim(); delete sections[n]; return v; } return ''; };
  const goal = take(['goal', 'what', 'task']);
  const done = take(['acceptance', 'done when', 'done', 'checks']);
  if (done) out.acceptance = [...(out.acceptance ?? []), ...done.split('\n').map(l => l.replace(/^\s*(?:[-*]|\d+[.)])\s*(?:\[[ xX]\]\s*)?/, '').trim()).filter(Boolean)];
  const rest = Object.entries(sections).filter(([k, v]) => v.join('').trim()).map(([k, v]) => (k === '_' ? '' : k.replace(/\b\w/g, c => c.toUpperCase()) + ':\n') + v.join('\n').trim()).join('\n\n');
  out.goal = [goal, rest].filter(Boolean).join('\n\n').trim();
  return out;
}

export function validatePlan(a, builders) {
  let args = a ?? {};
  if (args.markdown != null) {
    const {markdown, ...explicit} = args;
    const parsed = parsePacketMarkdown(markdown);
    args = {...parsed, ...Object.fromEntries(Object.entries(explicit).filter(([, v]) => v != null && v !== ''))};
  }
  const allowed = ['repo', 'base', 'agent', 'model', 'review', 'review_model', 'builder', 'title', 'goal', 'acceptance', 'paths', 'max_minutes', 'request_id', 'mission', 'step'];
  const extra = Object.keys(args).filter(k => !allowed.includes(k));
  if (extra.length) throw new Error('Unknown field(s): ' + extra.join(', '));
  const repo = String(args.repo ?? '').trim();
  if (!REPO.test(repo)) throw new Error('repo must be "owner/name" on GitHub.');
  const list = builders ?? [];
  if (!list.length) throw new Error('No builder has checked in yet. The owner sets one up once: bin/setup-builder.sh on their Mac, or the WSL installer on their Windows PC.');
  const target = args.builder == null || args.builder === '' ? 'any' : String(args.builder).trim().toLowerCase();
  if (target !== 'any' && !NAME.test(target)) throw new Error('builder must be "any" or a builder name (' + list.map(b => b.name).join(', ') + ').');
  const pool = target === 'any' ? list : list.filter(b => b.name === target);
  if (!pool.length) throw new Error(`No builder named ${target}. Builders: ${list.map(b => `${b.name} (${b.label})`).join(', ')}.`);
  const eligible = pool.filter(b => b.repos.includes(repo));
  if (!eligible.length) throw new Error(`${repo} is not on the allowed list of ${target === 'any' ? 'any builder' : pool[0].label} (${pool.map(b => `${b.label}: ${listOr(b.repos, 'none yet')}`).join('; ')}). The owner adds it on that machine (Windows PC: in the Ubuntu window; Mac: in Terminal): python3 ~/Procurator/builder/worker.py add-repo ${repo}   (add --test "<command>" if the repo has tests)`);
  const preferred = eligible.find(b => b.online) ?? eligible[0];
  const base = args.base == null || args.base === '' ? (preferred.branches?.[repo] || 'main') : String(args.base).trim();
  if (!BRANCH.test(base) || base.includes('..') || base.startsWith('-') || base.startsWith('/')) throw new Error('base must be a branch name.');
  const agent = args.agent == null || args.agent === '' ? (preferred.default_agent || 'claude') : String(args.agent);
  if (!AGENTS[agent]) throw new Error('agent must be "claude" or "codex".');
  if (!eligible.some(b => b.agents?.[agent] !== false)) throw new Error(`${AGENTS[agent]} is not installed on ${eligible.map(b => b.label).join(' or ')}.`);
  const model = args.model == null || args.model === '' ? '' : String(args.model).trim();
  if (model && !MODEL.test(model)) throw new Error('model must be a short model name the agent accepts (e.g. opus, sonnet, gpt-5.3-codex).');
  const offered = eligible.flatMap(b => b.models?.[agent] ?? []);
  if (model && offered.length && !offered.includes(model)) throw new Error(`${model} is not on the builder's model list for ${AGENTS[agent]} (${[...new Set(offered)].join(', ')}). Leave model empty for the builder's default.`);
  const review = args.review == null || args.review === '' ? '' : String(args.review).trim().toLowerCase();
  if (review && !REVIEWERS.includes(review)) throw new Error('review must be auto (the other installed agent reviews the diff), claude, codex or off.');
  const reviewModel = args.review_model == null || args.review_model === '' ? '' : String(args.review_model).trim();
  if (reviewModel && !MODEL.test(reviewModel)) throw new Error('review_model must be a short model name.');
  const title = clean(args.title, 200).replace(/\s+/g, ' ');
  if (title.length < 3 || title.length > 100) throw new Error('title must be 3-100 characters.');
  const goal = clean(args.goal, 4001);
  if (goal.length < 10 || goal.length > 4000) throw new Error('goal must be 10-4000 characters: what to build or fix, precisely.');
  const acceptance = args.acceptance == null ? [] : args.acceptance;
  if (!Array.isArray(acceptance) || acceptance.length > 10) throw new Error('acceptance must be a list of up to 10 checks.');
  const checks = acceptance.map(x => clean(x, 301)).filter(Boolean);
  if (checks.some(x => x.length > 300)) throw new Error('each acceptance check must be under 300 characters.');
  const paths = args.paths == null ? [] : args.paths;
  if (!Array.isArray(paths) || paths.length > 10 || !paths.every(p => typeof p === 'string' && PATHSPEC.test(p) && !p.includes('..') && !p.startsWith('/') && !p.startsWith('-'))) throw new Error('paths must be up to 10 repo-relative folders or file patterns (e.g. "src/", "docs/*.md").');
  const max = args.max_minutes == null ? 30 : Number(args.max_minutes);
  if (!Number.isInteger(max) || max < 5 || max > 60) throw new Error('max_minutes must be 5-60.');
  const requestId = String(args.request_id ?? '');
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(requestId)) throw new Error('request_id must be 8-64 letters, digits, _ or - (reuse it when retrying the same build).');
  // Optional link to a task packet step (Nov 6): the dashboard shows it and the finished build is written back as evidence.
  const mission = args.mission == null || args.mission === '' ? null : String(args.mission).toLowerCase();
  if (mission !== null && !/^[a-z0-9-]{2,24}$/.test(mission)) throw new Error('mission must be a packet name from mission_list.');
  const step = args.step == null || args.step === '' ? null : String(args.step);
  if (step !== null && !/^[A-Za-z0-9._:-]{1,48}\/s\d{1,2}$/.test(step)) throw new Error('step must look like THREAD/s2.');
  return {requestId, plan: {repo, base, agent, ...(model ? {model} : {}), ...(review ? {review} : {}), ...(reviewModel ? {review_model: reviewModel} : {}), builder: target, title, goal, acceptance: checks, paths, max_minutes: max}, mission, step};
}

export class Builds {
  constructor(dir, gateway, now = () => Date.now()) {
    this.dir = dir; this.gateway = gateway; this.now = now;
    fs.mkdirSync(dir, {recursive: true, mode: 0o700});
    for (const d of ['queue', 'cancel', 'claims', 'reports', 'builders', 'notes', 'progress', 'packets']) fs.mkdirSync(path.join(dir, d), {recursive: true, mode: 0o700});
    this.file = path.join(dir, 'jobs.json');
  }
  read() { try { return JSON.parse(fs.readFileSync(this.file, 'utf8')); } catch (e) { if (e.code === 'ENOENT') return {jobs: []}; throw e; } }
  save(j) { j.jobs = j.jobs.slice(-200); atomic(this.file, j); }
  find(j, id) { const r = j.jobs.find(x => x.id === String(id ?? '').trim()); if (!r) throw new Error('No build with that id.'); return r; }
  builders() {
    let names = [];
    try { names = fs.readdirSync(path.join(this.dir, 'builders')).filter(f => f.endsWith('.json')).map(f => f.slice(0, -5)).filter(n => NAME.test(n)); } catch {}
    const out = [];
    for (const name of names.sort()) {
      let b; try { b = JSON.parse(fs.readFileSync(path.join(this.dir, 'builders', name + '.json'), 'utf8')); } catch { continue; }
      if (!b || typeof b !== 'object') continue;
      const seen = Number(b.seen_at) || 0;
      out.push({name, label: clean(b.label, 30) || name, online: this.now() - seen < ONLINE_MS, seen_at: seen, last_seen: seen ? ago(this.now() - seen) : 'never',
        repos: Array.isArray(b.repos) ? b.repos.filter(r => typeof r === 'string' && REPO.test(r)) : [],
        agents: {claude: b.agents?.claude === true, codex: b.agents?.codex === true}, default_agent: AGENTS[b.default_agent] ? b.default_agent : null,
        models: Object.fromEntries(Object.keys(AGENTS).map(k => [k, Array.isArray(b.models?.[k]) ? b.models[k].filter(m => typeof m === 'string' && MODEL.test(m)).slice(0, 20) : []])),
        default_models: Object.fromEntries(Object.keys(AGENTS).map(k => [k, typeof b.defaults?.models?.[k] === 'string' && MODEL.test(b.defaults.models[k]) ? b.defaults.models[k] : null])),
        live: b.live !== false,
        branches: b.branches && typeof b.branches === 'object' ? b.branches : {}, busy: ID.test(b.busy ?? '') ? b.busy : null, host: clean(b.host, 60)});
    }
    return out;
  }
  eligible(plan) { return this.builders().filter(b => (plan.builder === 'any' || !plan.builder || b.name === plan.builder) && b.repos.includes(plan.repo)); }
  report(id) {
    let r; try { r = JSON.parse(fs.readFileSync(path.join(this.dir, 'reports', id + '.json'), 'utf8')); } catch { return null; }
    if (!r || typeof r !== 'object') return null;
    return {status: String(r.status ?? ''), builder: NAME.test(r.builder ?? '') ? r.builder : null, message: clean(r.message, 600), summary: clean(r.summary, 3000), pr_url: PR.test(r.pr_url ?? '') ? r.pr_url : null,
      branch: /^procurator\/[a-f0-9]{16}$/.test(r.branch ?? '') ? r.branch : null,
      tests: r.tests && typeof r.tests === 'object' ? {status: clean(r.tests.status, 20), command: clean(r.tests.command, 200), output_tail: clean(r.tests.output_tail, 2000)} : null,
      files: Array.isArray(r.files) ? r.files.slice(0, 60).map(f => clean(f, 200)) : [], file_count: Number.isInteger(r.file_count) ? r.file_count : undefined,
      insertions: Number.isInteger(r.insertions) ? r.insertions : undefined, deletions: Number.isInteger(r.deletions) ? r.deletions : undefined,
      duration_s: Number.isInteger(r.duration_s) ? r.duration_s : undefined, cost_usd: typeof r.cost_usd === 'number' ? r.cost_usd : undefined,
      question: clean(r.question, 1500) || null, model: MODEL.test(r.model ?? '') ? r.model : null, progress_count: Number.isInteger(r.progress_count) ? r.progress_count : 0,
      verification: this.verificationOf(r.verification), pr_state: ['OPEN', 'MERGED', 'CLOSED'].includes(r.pr_state) ? r.pr_state : null, merged_at: typeof r.merged_at === 'string' ? clean(r.merged_at, 40) : null,
      updated_at: Number(r.updated_at) || 0};
  }
  verificationOf(v) {
    if (!v || typeof v !== 'object') return null;
    const checks = (Array.isArray(v.checks) ? v.checks : []).slice(0, 10).map(c => ({n: Number(c.n) || 0, text: clean(c.text, 300), status: clean(c.status, 12), output: clean(c.output, 600)}));
    const rv = v.review && typeof v.review === 'object' ? {verdict: clean(v.review.verdict, 20), agent: AGENTS[v.review.agent] ? v.review.agent : '', model: MODEL.test(v.review.model ?? '') ? v.review.model : '',
      summary: clean(v.review.summary, 1500), issues: (Array.isArray(v.review.issues) ? v.review.issues : []).slice(0, 12).map(i => ({severity: clean(i.severity, 8), file: clean(i.file, 200), text: clean(i.text, 500)}))} : null;
    return {checks, review: rv, rounds: Number(v.rounds) || 0, unresolved: clean(v.unresolved, 20) || null, screenshots: Number(v.screenshots) || 0, screenshot_note: clean(v.screenshot_note, 300), passed: v.passed === true};
  }
  verificationLines(v, agent) {
    if (!v) return [];
    const out = [];
    if (v.checks.length) {
      const ok = v.checks.filter(c => c.status === 'passed').length;
      out.push(`Acceptance: ${ok}/${v.checks.length} checks passed` + (ok < v.checks.length ? ' — ' + v.checks.filter(c => c.status !== 'passed').map(c => `#${c.n} ${c.status.replace(/_/g, ' ')}`).join(', ') : ''));
    }
    if (v.review) out.push(`Review (${AGENTS[v.review.agent] || 'reviewer'}${v.review.model ? ', ' + v.review.model : ''}): ${v.review.verdict.replace(/_/g, ' ')}` + (v.review.summary ? ' — ' + v.review.summary.slice(0, 300) : ''));
    if (v.screenshots) out.push(`Screenshots: ${v.screenshots} in the PR`);
    else if (v.screenshot_note) out.push('Screenshots: none (' + v.screenshot_note + ')');
    if (v.rounds) out.push(`${v.rounds} fix round(s) were sent back to ${AGENTS[agent] || 'the agent'}`);
    out.push(v.passed ? 'Verification: all gates passed' : `Verification: OPEN ITEMS${v.unresolved ? ' (' + v.unresolved.toLowerCase() + ')' : ''} — read the PR's Verification section before merging`);
    return out;
  }
  // progress/<id>.jsonl (relay-written): the last n entries, each {t, text, name?, at}.
  progress(id, n = 15) {
    if (!ID.test(id ?? '')) return [];
    let lines; try { lines = fs.readFileSync(path.join(this.dir, 'progress', id + '.jsonl'), 'utf8').split('\n').filter(Boolean); } catch { return []; }
    return lines.slice(-n).map(l => { try { const e = JSON.parse(l); return {t: clean(e.t, 10), text: clean(e.text, 400), name: clean(e.name, 40) || null, at: Number(e.at) || 0}; } catch { return null; } }).filter(Boolean);
  }
  progressLine(e) {
    const t = e.at ? new Date(e.at).toISOString().slice(11, 16) + ' ' : '';
    if (e.t === 'tool') return `${t}▸ ${e.name || 'tool'}${e.text ? ' ' + e.text : ''}`;
    if (e.t === 'say') return `${t}${e.text}`;
    if (e.t === 'question') return `${t}❓ ${e.text}`;
    if (e.t === 'note') return `${t}You: ${e.text}`;
    if (e.t === 'result') return `${t}✔ ${e.text}`;
    return `${t}${e.text}`;
  }
  // /buildfrom SLUG: the BUILD.md the Packet Composer saved for a coding packet (packets/<slug>.md, written by the web broker).
  async fromPacket(slug, assertOwner = () => {}) {
    assertOwner();
    const name = String(slug ?? '').trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9-]{1,23}$/.test(name)) throw new Error('Use: /buildfrom SLUG — the packet name the Composer gave you (letters, digits, hyphens).');
    let md; try { md = fs.readFileSync(path.join(this.dir, 'packets', name + '.md'), 'utf8'); } catch { throw new Error(`No BUILD.md saved for "${name}". Save the coding packet in the Composer first (dashboard › Compose), or paste the BUILD.md here.`); }
    const out = await this.prepare({markdown: md, mission: name, request_id: 'buildfrom-' + name + '-' + digest(md).slice(0, 8)});
    return {...out, packet: name};
  }
  // /buildnote: the owner's message for the agent, queued for the relay to hand over with the builder's next report.
  note(id, text, assertOwner = () => {}) {
    assertOwner();
    const j = this.read(), r = this.find(j, id);
    const msg = clean(text, 2000);
    if (msg.length < 2) throw new Error('Write the message after the id: /buildnote ID what you want the agent to do or know.');
    if (!ACTIVE.includes(r.status) || r.status === 'queued') throw new Error(`Build ${r.id} is ${r.status.replace(/_/g, ' ')}; notes reach the agent only while it is working.`);
    const d = path.join(this.dir, 'notes', r.id);
    fs.mkdirSync(d, {recursive: true, mode: 0o700});
    const name = `${String(this.now()).padStart(13, '0')}-${crypto.randomBytes(4).toString('hex')}.json`;
    atomic(path.join(d, name), {text: msg, at: this.now()});
    r.notes = (r.notes ?? 0) + 1; this.save(j);
    return {id: r.id, status: r.status, queued: true, answers_question: r.status === 'waiting_owner'};
  }
  labelOf(name) { return this.builders().find(b => b.name === name)?.label || name || 'builder'; }

  async route() {
    try {
      const s = (await this.gateway('sessions.describe', {key: OWNER_SESSION}))?.session;
      const route = s?.deliveryContext;
      if ((route?.channel ?? s?.lastChannel) === 'telegram' && String(route?.to ?? s?.lastTo ?? '').replace(/^telegram:/, '') === OWNER) return route?.accountId ? {accountId: route.accountId} : {};
    } catch {}
    return {};
  }
  async notify(message, key) {
    await this.gateway('send', {channel: 'telegram', to: OWNER, ...(await this.route()), message, idempotencyKey: 'procurator-build:' + key});
  }

  async prepare(args) {
    const {requestId, plan, mission, step} = validatePlan(args, this.builders());
    const hash = digest(plan);
    const j = this.read();
    const same = j.jobs.find(x => x.requestId === requestId);
    if (same) {
      if (same.hash !== hash) throw new Error('That request_id was already used for a different build; use a new request_id for a revision.');
      if (same.status === 'pending' && same.expiresAt > this.now()) { await this.sendReview(same.id); return {id: same.id, status: 'awaiting_approval', review_sent: true, note: 'Same packet; review re-sent.'}; }
      return {id: same.id, status: same.status};
    }
    if (j.jobs.filter(x => ACTIVE.includes(x.status) || (x.status === 'pending' && x.expiresAt > this.now())).length >= 5) throw new Error('Five builds are already pending or running; finish or cancel one first.');
    const r = {id: crypto.randomBytes(8).toString('hex'), requestId, plan, hash, status: 'pending', createdAt: this.now(), expiresAt: this.now() + PENDING_MS, notified: {}, ...(mission ? {mission, step} : {})};
    j.jobs.push(r); this.save(j);
    await this.sendReview(r.id);
    const online = this.eligible(plan).filter(b => b.online);
    return {id: r.id, status: 'awaiting_approval', review_sent: true, runs_on: online.length ? online.map(b => b.label).join(' or ') + ' (online)' : 'no eligible builder is online; it waits until one is'};
  }
  runsOn(plan) {
    const el = this.eligible(plan);
    const desc = el.map(b => `${b.label} (${b.online ? 'online' : 'offline, last seen ' + b.last_seen})`);
    if (plan.builder && plan.builder !== 'any') return desc[0] ?? plan.builder;
    return el.length === 1 ? desc[0] : 'the first free of: ' + desc.join(', ');
  }
  review(r) {
    const p = r.plan;
    return [`BUILD REVIEW — ${r.id}`,
      `Repo: ${p.repo} (from ${p.base}) · ${AGENTS[p.agent]}${p.model ? ' (' + p.model + ')' : ''}` + (p.review === 'off' ? ' · no independent review' : ` · reviewed by ${p.review && p.review !== 'auto' ? AGENTS[p.review] : 'the other agent'}${p.review_model ? ' (' + p.review_model + ')' : ''}`),
      'Runs on: ' + this.runsOn(p),
      `Title: ${p.title}`, '', 'Goal:', p.goal,
      ...(p.acceptance.length ? ['', 'Done when:', ...p.acceptance.map(x => '• ' + x)] : []), '',
      'Files it may change: ' + (p.paths.length ? p.paths.join(', ') : 'any file in the repo'),
      `Time limit: ${p.max_minutes} min`, '',
      'While it works: I message you its progress; /buildnote ' + r.id + ' <text> steers it, and if it needs a decision it stops and asks you.',
      'Before the PR opens: its acceptance checks run, the tests run, and ' + (p.review === 'off' ? 'no second agent reviews it (review: off).' : 'a second agent reviews the diff; failures go back to it first.'), '',
      `What happens: the builder clones the repo into a fresh folder, runs ${AGENTS[p.agent]} with your own subscription on branch procurator/${r.id}, runs the test command saved in that machine's builder settings, and opens a DRAFT pull request for you to review. Nothing is merged or deployed, and secrets are blocked from the change.`, '',
      'Approve within 30 minutes with the entire plain-text line:', `/buildapprove ${r.id} ${r.hash}`, `Cancel: /buildcancel ${r.id}`].join('\n');
  }
  async sendReview(id) {
    const r = this.find(this.read(), id);
    if (r.status !== 'pending' || r.expiresAt <= this.now()) throw new Error('That build review is no longer pending; prepare it again.');
    await this.notify(this.review(r), `review:${r.id}:${r.hash}`);
    return {id: r.id, status: 'awaiting_approval', review_sent: true};
  }
  approve(id, hash, assertOwner = () => {}) {
    assertOwner();
    const j = this.read(), r = this.find(j, id);
    if (r.status !== 'pending') throw new Error(`Build ${r.id} is ${r.status.replace(/_/g, ' ')}; nothing to approve.`);
    if (r.expiresAt <= this.now()) { r.status = 'expired'; this.save(j); throw new Error('That build review expired; ask the assistant to prepare it again.'); }
    if (String(hash ?? '').trim() !== r.hash || digest(r.plan) !== r.hash) throw new Error('The approval code does not match this build packet. Nothing was queued.');
    const q = path.join(this.dir, 'queue', r.id + '.json');
    if (!fs.existsSync(q)) atomic(q, {id: r.id, approved_at: this.now(), plan: r.plan, hash: r.hash, version: 1});
    r.status = 'queued'; r.approvedAt = this.now(); this.save(j);
    const online = this.eligible(r.plan).filter(b => b.online);
    return {id: r.id, status: 'queued', online: online.map(b => b.label)};
  }
  cancel(id) {
    const j = this.read(), r = this.find(j, id);
    if (FINAL.includes(r.status)) return {id: r.id, status: r.status};
    if (r.status === 'pending') r.status = 'cancelled';
    else { fs.writeFileSync(path.join(this.dir, 'cancel', r.id), String(this.now()), {mode: 0o600}); r.status = fs.existsSync(path.join(this.dir, 'claims', r.id + '.json')) ? 'cancel_requested' : 'cancelled'; }
    this.save(j);
    return {id: r.id, status: r.status};
  }
  view(r) {
    const rep = this.report(r.id);
    const status = r.status === 'pending' && r.expiresAt <= this.now() ? 'expired' : r.status;
    return {id: r.id, status, repo: r.plan.repo, base: r.plan.base, agent: r.plan.agent, model: r.plan.model || undefined, title: r.plan.title, target: r.plan.builder || 'any', created: new Date(r.createdAt).toISOString(), ...(r.mission ? {mission: r.mission, step: r.step} : {}),
      ...(ACTIVE.includes(status) ? {progress: this.progress(r.id, 8).map(e => this.progressLine(e)), steer: `/buildnote ${r.id} <text>`} : {}),
      ...(rep ? {builder: rep.builder ? this.labelOf(rep.builder) : undefined, builder_status: rep.status, message: rep.message || undefined, question: rep.question || undefined, pr_url: rep.pr_url || undefined, branch: rep.branch || undefined, tests: rep.tests?.status || undefined,
        verification: rep.verification || undefined, pr_state: rep.pr_state || undefined, merged_at: rep.merged_at || undefined,
        files_changed: rep.file_count, summary: rep.summary || undefined, minutes: rep.duration_s ? Math.round(rep.duration_s / 60) : undefined} : {})};
  }
  status(id) {
    const j = this.read();
    const builders = this.builders().map(b => ({name: b.name, label: b.label, online: b.online, last_seen: b.last_seen, repos: b.repos, agents: Object.keys(AGENTS).filter(k => b.agents[k]), busy_with: b.busy}));
    const note = builders.length ? undefined : 'No builder set up yet (bin/setup-builder.sh on the Mac, or the WSL installer on the Windows PC).';
    if (id) return {build: this.view(this.find(j, id)), builders, note};
    return {builders, note, builds: j.jobs.slice(-10).reverse().map(r => this.view(r))};
  }

  // Every 30 s: follow the builders' reports and tell the owner about starts, results, offline builders and expiry.
  async sweep() {
    const j = this.read(); let changed = false;
    for (const r of j.jobs) {
      r.notified ??= {};
      if (r.status === 'pending' && r.expiresAt <= this.now()) { r.status = 'expired'; changed = true; continue; }
      if (r.status === 'done' && !r.merged) {
        // Long-horizon packets (0.32.0): the builder polls its pull requests; a merge unblocks packet steps waiting on it.
        const rep = this.report(r.id);
        if (rep?.pr_state === 'MERGED') {
          r.merged = rep.merged_at || new Date(this.now()).toISOString(); changed = true;
          try { await this.notify(`Build ${r.id} merged (${r.plan.title}, ${r.plan.repo}).` + (r.mission ? ` Packet ${r.mission}: the step waiting on this merge can start at its next work session.` : ''), `${r.id}:merged`); } catch {}
          try { await this.onMerge?.(r, rep); } catch {}
        } else if (rep?.pr_state === 'CLOSED' && !r.closedNotified) {
          r.closedNotified = true; changed = true;
          try { await this.notify(`Build ${r.id}'s pull request was closed without merging (${r.plan.title}).` + (r.mission ? ` Packet ${r.mission} steps waiting on it stay blocked; revise the packet or ask me to retry.` : ''), `${r.id}:closed`); } catch {}
        }
        continue;
      }
      if (!ACTIVE.includes(r.status)) continue;
      const rep = this.report(r.id);
      if (rep && rep.status && rep.status !== r.macStatus) {
        r.macStatus = rep.status; if (rep.builder) r.builder = rep.builder; changed = true;
        if (WORKING.includes(rep.status)) { r.lastWorking = rep.status; if (r.status !== 'cancel_requested') r.status = rep.status; }
        if (FINAL.includes(rep.status)) { r.status = rep.status; r.finishedAt = this.now(); }
      }
      if (FINAL.includes(r.status) && rep && !r.recorded) {
        // Once per build: usage meter (cost) and, for a packet step, the evidence write-back (Nov 6).
        r.recorded = this.now(); changed = true;
        try { await this.onFinish?.(r, rep); } catch {}
      }
      const send = async (kind, text) => { if (r.notified[kind]) return; try { await this.notify(text, `${r.id}:${kind}`); r.notified[kind] = this.now(); changed = true; } catch {} };
      const on = where({label: this.labelOf(r.builder)});
      if (rep && r.status === 'waiting_owner' && rep.question && rep.question !== r.askedQuestion) {
        r.askedQuestion = rep.question; r.notified['q:' + digest(rep.question)] = undefined; changed = true;
        await send('q:' + digest(rep.question), `BUILD QUESTION — ${r.id} (${r.plan.title})\n${AGENTS[r.plan.agent]} needs your decision:\n\n${rep.question}\n\nAnswer with: /buildnote ${r.id} your answer\n(It waits up to the builder's limit, then uses its judgement.)`);
      }
      if (rep && ['running', 'testing'].includes(r.status) && rep.progress_count > (r.digestedCount ?? 0) && this.now() - (r.lastDigest ?? 0) >= PROGRESS_DIGEST_MS) {
        const fresh = this.progress(r.id, 6);
        if (fresh.length) {
          r.digestedCount = rep.progress_count; r.lastDigest = this.now(); changed = true;
          try { await this.notify(`Build ${r.id} · ${AGENTS[r.plan.agent]} on ${r.plan.repo}:\n` + fresh.map(e => this.progressLine(e)).join('\n') + `\n\nSteer: /buildnote ${r.id} <text> · Log: /buildlog ${r.id}`, `${r.id}:digest:${rep.progress_count}`); } catch {}
        }
      }
      if (['preparing', 'running', 'waiting_owner', 'testing', 'pushing'].includes(r.status)) await send('started', `Build ${r.id} started on ${on}: ${AGENTS[r.plan.agent]}${r.plan.model ? ' (' + r.plan.model + ')' : ''} is working on ${r.plan.repo} (“${r.plan.title}”). I'll message you when the pull request is ready. Stop it: /buildcancel ${r.id}`);
      if (r.status === 'done' && rep) await send('final', this.doneText(r, rep));
      if (r.status === 'failed' && rep) await send('final', `BUILD FAILED — ${r.id} (${r.plan.repo}, ${on})\n${rep.message || 'No reason given.'}${rep.tests?.output_tail ? '\nLast test output:\n' + rep.tests.output_tail.slice(-600) : ''}\n` + (rep.pr_url ? `A draft pull request exists: ${rep.pr_url}` : rep.branch && r.lastWorking === 'pushing' ? `Branch ${rep.branch} may have been pushed without a pull request.` : 'Nothing was pushed to GitHub.') + ` Details: /buildstatus ${r.id}`);
      if (r.status === 'cancelled') await send('final', `Build ${r.id} stopped. Nothing was pushed.`);
      if (r.status === 'queued') {
        const el = this.eligible(r.plan), waited = this.now() - (r.approvedAt ?? this.now());
        if (waited > QUEUE_MAX_MS) {
          fs.writeFileSync(path.join(this.dir, 'cancel', r.id), String(this.now()), {mode: 0o600});
          r.status = 'expired'; changed = true;
          await send('final', `Build ${r.id} expired: no builder picked it up within 24 hours. Ask me to prepare it again when one is online.`);
        } else if (!el.some(b => b.online) && waited > OFFLINE_ALERT_MS)
          await send('offline', `Build ${r.id} is approved, but no builder that allows ${r.plan.repo} is online (${el.map(b => `${b.label}: last seen ${b.last_seen}`).join('; ') || 'none allows it now'}). It starts as soon as one is awake; it expires after 24 hours.`);
      }
    }
    if (changed) this.save(j);
  }
  doneText(r, rep) {
    const t = rep.tests?.status;
    return [`BUILD READY — ${r.id}`, `${r.plan.title} (${r.plan.repo}, built on ${where({label: this.labelOf(rep.builder || r.builder)})})`, `Draft pull request: ${rep.pr_url ?? '(link missing — check /buildstatus ' + r.id + ')'}`,
      'Tests: ' + (t === 'passed' ? 'passed' : t === 'failed' ? 'FAILED (see the PR before merging)' : t === 'timed_out' ? 'timed out' : 'none configured for this repo') + (rep.tests?.command ? ` (${rep.tests.command})` : ''),
      `Changed: ${rep.file_count ?? rep.files.length} file(s)` + (rep.insertions != null ? `, +${rep.insertions}/-${rep.deletions ?? 0} lines` : '') + (rep.duration_s ? `, ${Math.max(1, Math.round(rep.duration_s / 60))} min` : '') + (rep.cost_usd ? `, ~$${rep.cost_usd.toFixed(2)} of usage` : ''),
      ...this.verificationLines(rep.verification, r.plan.agent),
      ...(rep.summary ? ['', rep.summary.slice(0, 1500)] : []), '', 'Review it on GitHub; nothing is merged or deployed until you do it.'].join('\n');
  }
}

const text = v => ({content: [{type: 'text', text: JSON.stringify(v)}]});

export function registerBuilds(api, builds, ownerContext) {
  const tools = {
    build_prepare: {description: 'Prepare a code build on one of the owner\'s own machines (their always-on Windows PC or their Mac; Claude Code or Codex with their subscriptions) and send them the exact packet for /buildapprove. The builder clones the repo, works on a new branch, runs the repo\'s tests and opens a DRAFT pull request; nothing is merged or deployed. Only repos a builder allows: call build_status first and use the exact owner/name it lists; never guess a GitHub owner from an email address. If the repo is not allowed yet, tell the owner the exact add-repo command and which machine to run it on (the error names the builders). Write a precise goal and testable acceptance checks; never put passwords, keys or tokens in a build.',
      properties: {repo: {type: 'string', description: 'GitHub owner/name, e.g. example-user/website'}, base: {type: 'string', description: 'Branch to start from (default: the builder\'s setting, usually main)'},
        agent: {type: 'string', enum: ['claude', 'codex'], description: 'claude = Claude Code (default), codex = OpenAI Codex'},
        model: {type: 'string', description: 'Optional: the model the coding agent should use, as its CLI names it (Claude Code: opus, sonnet, or a full model id; Codex: e.g. gpt-5.3-codex). Empty = the builder\'s default.'},
        review: {type: 'string', enum: ['auto', 'claude', 'codex', 'off'], description: 'Who reviews the diff before the PR opens: auto (default: the other installed agent, else the same one fresh), claude, codex, or off.'},
        review_model: {type: 'string', description: 'Optional model for the reviewer.'},
        markdown: {type: 'string', description: 'Optional: the owner\'s BUILD.md packet as text. Front matter (repo, base, agent, model, builder, paths, max_minutes), "# title", "## Goal", "## Acceptance" bullets. Fields given explicitly override it.'},
        builder: {type: 'string', description: 'Which machine: "any" (default: the first free builder that allows the repo) or a builder name from build_status (e.g. "windows", "mac") when the owner asks for one'},
        title: {type: 'string', description: 'Short PR title, 3-100 characters'},
        goal: {type: 'string', description: 'What to build or fix, with the details the coding agent needs (up to 4000 characters)'},
        acceptance: {type: 'array', items: {type: 'string'}, description: 'Up to 10 checks that define done'},
        paths: {type: 'array', items: {type: 'string'}, description: 'Optional: only these repo folders/patterns may change (e.g. "src/", "docs/*.md")'},
        max_minutes: {type: 'integer', description: '5-60, default 30'}, request_id: {type: 'string', description: 'Your own id for this request (8-64 chars); reuse it if you retry the same packet'},
        mission: {type: 'string', description: 'Optional: the task packet (mission) this build belongs to, e.g. "sandbox"'}, step: {type: 'string', description: 'Optional: the packet step, e.g. "MAIN/s1"; the PR link and test result are written to it as evidence'}},
      required: ['request_id'], run: a => builds.prepare(a)},
    build_status: {description: 'Builders (their Windows PC, their Mac: online, allowed repos, installed agents, models) and recent builds with their pull request links, test results, summaries, and for a running build its live progress and any open question; pass id for one build.',
      properties: {id: {type: 'string'}}, required: [], run: a => builds.status(a?.id)},
    build_cancel: {description: 'Stop a pending, queued or running build (nothing is pushed). Say so to the owner.',
      properties: {id: {type: 'string'}}, required: ['id'], run: a => builds.cancel(a.id)},
  };
  for (const [name, d] of Object.entries(tools)) api.registerTool?.(ctx => ctx.agentId === 'main' ? {name, label: name, description: d.description,
    parameters: {type: 'object', properties: d.properties, required: d.required, additionalProperties: false},
    async execute(_id, args) { try { ctx.assertInvocationCurrent?.(); return text(await d.run(args ?? {})); } catch (e) { return {...text({error: String(e?.message || e)}), isError: true}; } }} : undefined, {optional: true, name});
  // Editions (0.30.0): the coding orchestrator ships only in PROCURATOR-X; on a standard box the commands stay registered
  // (so /manual can explain them) but answer with the edition note instead of touching the build store.
  const X_ONLY = 'Code builds are part of PROCURATOR-X, the developer edition. This box runs the standard edition; reinstall with `install.sh --edition x` to add the coding orchestrator.';
  const cmd = (name, description, fn) => api.registerCommand?.({name, description, channels: ['telegram'], requireAuth: true, acceptsArgs: true,
    async handler(ctx) { try { ownerContext(ctx); if (edition() !== 'x') return {text: X_ONLY}; return {text: await fn(String(ctx.args ?? '').trim(), ctx)}; } catch (e) { return {text: String(e?.message || e)}; } }});
  cmd('buildapprove', 'Approve an exact build packet: /buildapprove ID CODE', args => {
    const m = /^([a-f0-9]{16})\s+([a-f0-9]{16})$/.exec(args);
    if (!m) return 'Send the entire line from the BUILD REVIEW message: /buildapprove ID CODE';
    const out = builds.approve(m[1], m[2]);
    return `Build ${out.id} approved and queued. ` + (out.online.length ? `${out.online.join(' or ')} picks it up within a minute; I'll message you when it starts and when the pull request is ready.` : 'No builder that allows this repo is online right now; it starts when one wakes (within 24 hours).') + ` Stop it: /buildcancel ${out.id}`;
  });
  cmd('buildcancel', 'Stop a build: /buildcancel ID', args => {
    const out = builds.cancel(args);
    return out.status === 'cancel_requested' ? `Stopping build ${out.id}: the builder stops it within about 30 seconds; nothing is pushed.` : `Build ${out.id}: ${out.status.replace(/_/g, ' ')}.`;
  });
  cmd('buildstatus', 'Builders and recent builds: /buildstatus [ID]', args => {
    const s = builds.status(args || undefined);
    const head = s.builders.length ? s.builders.map(b => `${b.label} (${b.name}): ${b.online ? 'online' : 'offline, last seen ' + b.last_seen} · repos: ${b.repos.join(', ') || 'none allowed yet'} · agents: ${b.agents.join(', ') || 'none'}` + (b.busy_with ? ` · working on ${b.busy_with}` : '')).join('\n') : s.note;
    const line = b => `${b.id} · ${b.status.replace(/_/g, ' ')} · ${b.repo} · ${b.title}` + (b.builder ? ` · on ${b.builder}` : '') + (b.pr_url ? `\n  PR: ${b.pr_url}` : '') + (b.tests ? ` · tests ${b.tests}` : '') + (b.message && b.status !== 'done' ? `\n  ${b.message}` : '');
    if (s.build) return head + '\n\n' + line(s.build) + (s.build.summary ? '\n\n' + s.build.summary.slice(0, 1500) : '');
    return head + '\n\n' + (s.builds.length ? s.builds.map(line).join('\n') : 'No builds yet.');
  });
  cmd('buildreview', 'Re-show a pending build packet: /buildreview ID', args => builds.review(builds.find(builds.read(), args)));
  cmd('buildfrom', 'Start the build a coding packet describes: /buildfrom SLUG (the BUILD.md the Composer saved)', async args => {
    const out = await builds.fromPacket(args);
    return out.status === 'awaiting_approval' ? `BUILD REVIEW sent for packet ${out.packet} (build ${out.id}). Approve it with the line in that message.` : `Packet ${out.packet}: build ${out.id} is ${String(out.status).replace(/_/g, ' ')}.`;
  });
  cmd('buildnote', 'Steer a running build or answer its question: /buildnote ID your message', args => {
    const m = /^([a-f0-9]{16})\s+([\s\S]+)$/.exec(args);
    if (!m) return 'Use: /buildnote ID your message — the id is in the build messages; the text reaches the coding agent as your instruction.';
    const out = builds.note(m[1], m[2]);
    return out.answers_question ? `Answer sent to build ${out.id}; the agent continues with it within about 15 seconds.` : `Note queued for build ${out.id}; the agent gets it within about 15 seconds, as "OWNER: …".`;
  });
  cmd('buildlog', 'Recent progress of a build: /buildlog ID', args => {
    const r = builds.find(builds.read(), args);
    const lines = builds.progress(r.id, 20).map(e => builds.progressLine(e));
    const rep = builds.report(r.id);
    return [`Build ${r.id} · ${r.status.replace(/_/g, ' ')} · ${r.plan.repo} · ${r.plan.title}${r.plan.model ? ' · ' + r.plan.model : ''}`, ...(rep?.question ? ['', 'Waiting for you: ' + rep.question, `Answer: /buildnote ${r.id} …`] : []), '',
      ...(lines.length ? lines : ['No progress reported yet.']), ...(ACTIVE.includes(r.status) ? ['', `Steer: /buildnote ${r.id} <text> · Stop: /buildcancel ${r.id}`] : [])].join('\n');
  });
  const KEY = Symbol.for('owner.builds.sweep');
  if (globalThis[KEY]) clearInterval(globalThis[KEY]);
  let busy = false;
  globalThis[KEY] = setInterval(() => { if (busy) return; busy = true; builds.sweep().catch(() => {}).finally(() => { busy = false; }); }, 30000);
  globalThis[KEY].unref?.();
  return builds;
}
