// Welcome interview (0.30.0). Right after first-run setup the assistant introduces itself and asks a few short questions, one
// at a time: what to call the owner, how to refer to them (he/him, she/her, they/them), where they are (time zone), what they
// do, what they want help with first, and the email they use most. Answers go to the owner's private profile (profile.mjs) and
// to welcome.json beside it. From then on every turn carries one line telling the model the owner's name and pronouns, so the
// assistant (and what it writes or says on calls about the owner) uses them — the built-in prompts themselves say "the owner".
//
// Pending only when welcome.json says so: setup (integrations/web/setup_core.py) writes {"status":"pending"} for a new owner.
// An existing box is never interrupted; /welcome starts (or restarts) the interview at any time.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const PRONOUNS = {'he/him': ['he', 'him', 'his'], 'she/her': ['she', 'her', 'her'], 'they/them': ['they', 'them', 'their']};
const TEXT = /^[^\u0000-\u001f\u007f‪-‮⁦-⁩]{1,400}$/;

export class Welcome {
  constructor(directory, profile, now = () => new Date()) {
    this.directory = directory; this.profile = profile; this.now = now;
    this.file = path.join(directory, 'welcome.json');
  }
  read() {
    try {
      const st = fs.lstatSync(this.file);
      if (st.isSymbolicLink() || st.size > 8000) return {status: 'none'};
      const d = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      return d && typeof d === 'object' ? d : {status: 'none'};
    } catch { return {status: 'none'}; }
  }
  write(d) {
    fs.mkdirSync(this.directory, {recursive: true, mode: 0o700});
    const tmp = this.file + '.' + crypto.randomBytes(6).toString('hex') + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(d, null, 1) + '\n', {mode: 0o600});
    fs.renameSync(tmp, this.file);
    return d;
  }
  pending() { return this.read().status === 'pending'; }
  start() { const d = this.read(); return this.write({...d, status: 'pending', started_at: this.now().toISOString()}); }

  facts() {
    try { return this.profile.view().facts; } catch { return {}; }
  }

  // The one line every turn carries once the owner is known (and during the interview, what is still missing).
  ownerLine() {
    const f = this.facts(), w = this.read();
    const name = f.preferred_name?.value || f.first_name?.value || '';
    const pro = f.pronouns?.value || '';
    const parts = [];
    if (name) parts.push('call them "' + name + '"');
    if (PRONOUNS[pro]) parts.push('refer to them as ' + PRONOUNS[pro].slice(0, 2).join('/') + ' (' + PRONOUNS[pro][2] + ')');
    if (f.occupation?.value) parts.push('work: ' + f.occupation.value);
    if (w.help_with) parts.push('wants help with: ' + w.help_with);
    return parts.length ? 'OWNER: ' + parts.join('; ') + '. When you speak or write about the owner to anyone else, use these pronouns.' : '';
  }

  guidance() {
    const f = this.facts(), w = this.read();
    const have = {name: !!(f.preferred_name?.value), pronouns: !!(f.pronouns?.value), place: !!(f.timezone?.value || f.city?.value),
      work: !!(f.occupation?.value), help: !!w.help_with, email: !!(f.email?.value)};
    const ask = [['name', 'what they would like to be called'], ['pronouns', 'how you should refer to them: he/him, she/her or they/them'],
      ['place', 'the city they live in (you work out the time zone; confirm it)'], ['work', 'what they do, in a line'],
      ['help', 'the first things they want help with'], ['email', 'the email address they use most']].filter(([k]) => !have[k]).map(([, q]) => q);
    return 'WELCOME INTERVIEW (first setup, still open). You were just set up for this owner. Greet them briefly (one sentence: you are their ' +
      'PROCURATOR assistant; anything that sends, books, pays or calls waits for their approval). Then ask ONE short question per message, in this order: ' +
      (ask.length ? ask.join('; ') : 'nothing left — call owner_welcome_save with done=true') + '. ' +
      'Accept "skip" for any question. Save every answer at once with owner_welcome_save (only what they said; never guess). ' +
      'When all are answered or they want to stop, call owner_welcome_save with done=true and reply with a three-line summary: what you will call them, ' +
      'the next step (send /panel and connect Apple or Google on the Accounts tab), and two example requests that fit what they want help with. ' +
      'If their first message is an urgent task, do that first and offer to finish the introduction afterwards.';
  }

  save(args, source = 'Owner answered the welcome interview') {
    const saved = [];
    const put = (field, v) => { this.profile.set(field, v, source); saved.push(field); };
    if (args.preferred_name != null) put('preferred_name', String(args.preferred_name).trim());
    if (args.pronouns != null) {
      if (!PRONOUNS[args.pronouns]) throw new Error('pronouns is he/him, she/her or they/them.');
      put('pronouns', args.pronouns);
    }
    if (args.timezone != null) put('timezone', String(args.timezone).trim());
    if (args.city != null) put('city', String(args.city).trim());
    if (args.occupation != null) put('occupation', String(args.occupation).trim());
    if (args.email != null) put('email', String(args.email).trim());
    const w = this.read();
    if (args.help_with != null) {
      const h = String(args.help_with).trim();
      if (!TEXT.test(h)) throw new Error('help_with is one short plain-text line (400 characters at most).');
      w.help_with = h; saved.push('help_with');
    }
    if (args.done === true) { w.status = 'done'; w.finished_at = this.now().toISOString(); }
    else if (w.status !== 'done') w.status = 'pending';
    this.write(w);
    return {saved, status: w.status, still_to_ask: w.status === 'done' ? [] : this.missing()};
  }

  missing() {
    const f = this.facts(), w = this.read();
    return [['preferred_name', f.preferred_name], ['pronouns', f.pronouns], ['city_or_timezone', f.timezone || f.city], ['occupation', f.occupation],
      ['help_with', w.help_with ? {value: 1} : null], ['email', f.email]].filter(([, v]) => !v?.value).map(([k]) => k);
  }
}

export function registerWelcome(api, welcome, ownerContext) {
  api.registerTool(ctx => ctx.agentId === 'main' ? {
    name: 'owner_welcome_save', label: 'Save welcome answers',
    description: 'Save what the owner just told you in the welcome interview (only their own words, never guesses): preferred_name, pronouns ' +
      '(he/him | she/her | they/them), city, timezone (IANA, e.g. America/Chicago — confirm with them), occupation, help_with (one line), email. ' +
      'Set done=true when the interview is finished or they want to stop. Returns what is still to ask.',
    parameters: {type: 'object', additionalProperties: false, properties: {
      preferred_name: {type: 'string', maxLength: 80}, pronouns: {type: 'string', enum: Object.keys(PRONOUNS)},
      city: {type: 'string', maxLength: 120}, timezone: {type: 'string', maxLength: 60}, occupation: {type: 'string', maxLength: 200},
      help_with: {type: 'string', maxLength: 400}, email: {type: 'string', maxLength: 200}, done: {type: 'boolean'}}},
    async execute(_id, args) {
      try { ctx.assertInvocationCurrent?.(); return {content: [{type: 'text', text: JSON.stringify(welcome.save(args ?? {}))}]}; }
      catch (e) { return {content: [{type: 'text', text: String(e?.message || e)}], isError: true}; }
    }
  } : undefined, {optional: true, name: 'owner_welcome_save'});

  api.on?.('before_prompt_build', (_event, ctx) => {
    if (ctx.agentId !== 'main') return;
    ctx.hookInvocation?.assertActive();
    const line = welcome.ownerLine();
    const out = {};
    if (line) out.appendSystemContext = line;                 // changes only when the owner's answers change (cache-friendly)
    if (welcome.pending()) out.appendContext = welcome.guidance();
    return Object.keys(out).length ? out : undefined;
  }, {priority: 82, registrationId: 'procurator-welcome'});

  api.registerCommand?.({name: 'welcome', description: 'Start (or redo) the short welcome interview: your name, pronouns, city, work, what you want help with.',
    channels: ['telegram'], requireAuth: true, acceptsArgs: false,
    async handler(ctx) {
      try {
        ownerContext(ctx);
        welcome.start();
        return {text: 'Welcome interview on. Send any message (for example "hi") and I will ask a few quick questions, one at a time. Say "skip" to pass on any of them.'};
      } catch (e) { return {text: String(e?.message || e)}; }
    }});
}
