// Urgent-care notice (0.25.0, F5). When the owner's own message describes what may be a medical emergency happening to them or
// someone with them, a FIXED notice goes to their Telegram at once: the words are written here, not by a model. The assistant is
// told the notice went out, to stop any health task in that turn and not to assess symptoms. PROCURATOR handles health
// logistics; it does not triage. First-person or present-tense framing is required so clinical work (a HEART score, a
// literature search about chest pain) does not trigger it.
import crypto from 'node:crypto';
import {sessionKind} from './recall.mjs';

export const NOTICE = 'If this could be an emergency, call 911 now (or your local emergency number).\n' +
  'If you are thinking about suicide or self-harm, call or text 988 (Suicide & Crisis Lifeline, US).\n' +
  'PROCURATOR handles appointments and paperwork and cannot assess symptoms. Health tasks are paused for this message.';

const WHO = String.raw`(?:\bi\b|\bi'?m\b|\bi am\b|\bme\b|\bmy\b|\bwe\b|\bour\b|\bhe'?s\b|\bshe'?s\b|\bhe is\b|\bshe is\b|\bthey'?re\b|\bmy (?:wife|husband|son|daughter|child|kid|mom|mother|dad|father|partner|baby|friend))`;
const NEAR = String.raw`[^.?!\n]{0,40}?`;
const SIGNS = [
  String.raw`chest (?:pain|pressure|tightness)|chest (?:hurts|is tight)|heart attack`,
  String.raw`can'?t breathe|cannot breathe|trouble breathing|struggling to breathe|not breathing|choking`,
  String.raw`face (?:is )?drooping|slurred speech|(?:sudden(?:ly)? )?(?:weak|numb)(?:ness)? (?:on|in) (?:one|the (?:left|right)) side|having a stroke|stroke symptoms`,
  String.raw`(?:won'?t|will not|can'?t) stop bleeding|bleeding (?:heavily|a lot|badly)`,
  String.raw`passed out|unconscious|unresponsive|won'?t wake up|having a seizure|seizing`,
  String.raw`overdos(?:e|ed|ing)|took too many (?:pills|tablets)`,
  String.raw`throat (?:is )?(?:closing|swelling)|tongue (?:is )?swelling|anaphyla`,
];
const DIRECT = [
  String.raw`\b(?:kill|hurt|harm) (?:myself|my self)\b`, String.raw`\bend (?:my|it all|my life)\b`, String.raw`\bwant to die\b`,
  String.raw`\bsuicid(?:e|al)\b(?![^.\n]{0,30}\b(?:screen|scale|risk assessment|score|patients?|study|article)\b)`,
];
const SIGN_RE = new RegExp(String.raw`${WHO}${NEAR}(?:${SIGNS.join('|')})|(?:${SIGNS.join('|')})${NEAR}${WHO}`, 'i');
const DIRECT_RE = new RegExp(DIRECT.join('|'), 'i');
const CLINICAL = /\b(?:score|calculator|calc|patient|pt\b|differential|guideline|evidence|study|studies|paper|trial|criteria|workup|case of)\b/i;

export function urgentMatch(text) {
  const t = String(text || '').slice(0, 4000);
  if (!t.trim()) return false;
  if (DIRECT_RE.test(t)) return true;
  return SIGN_RE.test(t) && !CLINICAL.test(t);
}

export function lastOwnerText(event) {
  if (typeof event?.prompt === 'string') return event.prompt;
  const msgs = Array.isArray(event?.messages) ? event.messages : [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m?.role !== 'user') continue;
    if (typeof m.content === 'string') return m.content;
    if (Array.isArray(m.content)) return m.content.filter(p => p?.type === 'text').map(p => p.text).join('\n');
  }
  return '';
}

export function registerUrgentCare(api, { gateway, owner = '100000001', now = () => Date.now() } = {}) {
  const sent = new Map();
  api.on?.('before_prompt_build', async (event, ctx) => {
    if (ctx?.agentId !== 'main' || sessionKind(ctx?.sessionKey) !== 'chat') return;
    const text = lastOwnerText(event);
    if (!urgentMatch(text)) return;
    const key = crypto.createHash('sha256').update(text).digest('hex').slice(0, 16);
    for (const [k, t] of sent) if (now() - t > 600000) sent.delete(k);
    if (!sent.has(key)) {
      sent.set(key, now());
      try { await gateway?.('send', { channel: 'telegram', to: owner, message: NOTICE, idempotencyKey: 'procurator-urgent:' + key }); } catch { }
    }
    return { appendContext: '\n[URGENT-CARE NOTICE SENT] Their message may describe a medical emergency. The fixed emergency notice (911 / 988) was already sent to them. ' +
      'In this reply: do not assess symptoms or give medical advice, do not start or continue any health booking or call, and keep it short. If the owner says it is not an emergency or is about their clinical work, continue normally next turn.' };
  }, { priority: 99, registrationId: 'procurator-urgent-care' });
}
