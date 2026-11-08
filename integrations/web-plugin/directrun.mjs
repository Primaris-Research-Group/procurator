import {ownerId} from './owner-config.mjs';
// Direct assistant runs in their Telegram conversation (web 0.19.2, Nov 6 compatibility run).
// The 2026.10.9 runtime only offers enqueueSystemEvent + requestHeartbeat to wake the assistant, and with the heartbeat
// cadence off (cost controls, every "0m") the gateway can drop those wakes (OpenClaw issue #166508): an approved
// browser task sat at "wake_requested" with zero steps. Mission work sessions never had this problem because they run
// through the gateway's agent RPC and send the reply themselves. This does the same for task continuations, the morning
// brief, reminders, mission wakes and sign-in wakes: run one turn in their conversation's session, then send its reply.

const OWNER = ownerId();
const CHUNK = 3800;

const textOfContent = c => typeof c === 'string' ? c : Array.isArray(c) ? c.map(x => typeof x === 'string' ? x : (x && (x.type === 'text' || x.type === 'output_text') && typeof x.text === 'string') ? x.text : '').join('\n') : '';
const fromPayloads = p => (Array.isArray(p?.payloads) ? p.payloads : []).filter(x => x && !x.isError && !x.isReasoning).map(x => x.text ?? '').join('\n').trim();

// The final reply of a run, from whichever shape the gateway returned: payloads (agent with a final result) or
// terminalReply (agent.wait; a string, {text}, {payloads} or {content}).
export function replyText(r) {
  const p = r?.result ?? r;
  const direct = fromPayloads(p);
  if (direct) return direct;
  const tr = p?.terminalReply;
  if (typeof tr === 'string') return tr.trim();
  if (tr && typeof tr === 'object') return (fromPayloads(tr) || (typeof tr.text === 'string' ? tr.text : '') || textOfContent(tr.content)).trim();
  return '';
}

// Nov 6 (Leaveco, live): the gateway `agent` RPC answers at once with {runId, status: 'accepted'}; the outcome comes from
// `agent.wait` (status ok | error | timeout, or 'accepted' again while the run is still going when the wait deadline
// passes). 0.19.2 treated 'accepted' as a failure, so the task worker, the main assistant and packet sessions were all
// reported as failed while they were still running. awaitRun resolves {status:'ok', text} or throws.
// 0.20.7 (Nov 7 browser batteries): `agent.wait` answers {status:'timeout'} when ITS wait window (timeoutMs) ends while the run
// is still going — it is wait-only and does not stop the run (OpenClaw agent-loop docs). 0.20.1 treated that as a failed run,
// so every case still working after the 5-minute slice was reported failed and its browser was closed under the actor
// (Hotels.com, CVS, Walmart, Home Depot, Georgia Power). Now: keep waiting on the same runId until OUR deadline; only then
// stop the run (chat.abort) so it does not keep working on a browser that is about to be closed, and report it.
const RUNNING = new Set(['accepted', 'started', 'pending', 'timeout', 'running', 'queued']);
export async function awaitRun(gateway, first, { timeoutMs = 900000, sessionKey, now = () => Date.now(), waitSlice = 300000, stopOnDeadline = true } = {}) {
  const p = first?.result ?? first;
  const runId = p?.runId ?? first?.runId;
  let status = p?.status ?? first?.status;
  let final = first;
  let unknown = false;
  if ((RUNNING.has(status) || (!status && !fromPayloads(p))) && runId) {
    const deadline = now() + timeoutMs;
    do {
      const slice = Math.max(1000, Math.min(waitSlice, deadline - now()));
      final = await gateway('agent.wait', { runId, timeoutMs: slice }, slice + 15000);
      const r = final?.result ?? final;
      status = r?.status ?? final?.status;
      unknown = r?.unknownRun === true;
      if (unknown) break;   // the gateway has never heard of this run (it restarted): it will never finish
    } while (RUNNING.has(status) && now() < deadline);
  }
  if (status && status !== 'ok') {
    const err = (final?.result ?? final)?.error ?? final?.error;
    const stillRunning = RUNNING.has(status) && !unknown;
    if (stillRunning && sessionKey && stopOnDeadline) {
      try { await gateway('chat.abort', { sessionKey, runId }, 20000); } catch { }
    }
    throw new Error('run ' + (stillRunning ? 'still running after ' + Math.round(timeoutMs / 60000) + ' minutes (stopped)' : unknown ? 'lost (the gateway restarted)' : status) + (err ? ': ' + String(err?.message || err).slice(0, 120) : ''));
  }
  let text = replyText(final) || replyText(first);
  if (!text && sessionKey) {
    // Last resort: the newest assistant message of that session (only if it is the newest message at all).
    try {
      const h = await gateway('chat.history', { sessionKey, limit: 4 }, 20000);
      const ms = (h?.result ?? h)?.messages;
      const last = Array.isArray(ms) ? ms[ms.length - 1] : null;
      if (last?.role === 'assistant') text = textOfContent(last.content ?? last.text).trim();
    } catch { }
  }
  return { status: 'ok', text };
}

// 0.20.7 (Nov 7 browser batteries): OpenClaw 2026.10.9 keeps one set of stdio MCP helper processes (Google, gog, workspace)
// per SESSION, retired only on session reset/deletion (never on idle, OpenClaw #166017/#161311). Every test case and every
// task-worker session is a new session, so the gateway hit 256 threads and 1.49 of 1.5 GiB on Nov 7. A background session
// that is finished is deleted (reset if delete is refused); their own conversation (agent:main:main, Telegram) never is.
export async function retireSession(gateway, sessionKey) {
  if (typeof sessionKey !== 'string' || !/^agent:main:(webbench|webtask|mission)-/.test(sessionKey)) return 'kept';
  try { await gateway('sessions.delete', { key: sessionKey }, 20000); return 'deleted'; } catch { }
  try { await gateway('sessions.reset', { key: sessionKey }, 20000); return 'reset'; } catch { }
  return 'failed';
}

export function chunks(text, size = CHUNK) {
  const out = [];
  let rest = String(text || '');
  while (rest.length > size) {
    let cut = rest.lastIndexOf('\n', size);
    if (cut < size / 2) cut = size;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/, '');
  }
  if (rest) out.push(rest);
  return out;
}

// chatRunner(gateway)(text, {sessionKey, key, onSettled}) resolves within quickMs to
//   'completed' (the turn already finished and its reply was sent), 'started' (the turn is running), or 'failed: why'.
// onSettled(status) is called once when the turn ends: 'completed' or 'failed: why'.
export function chatRunner(gateway, { owner = OWNER, quickMs = 5000, timeoutMs = 900000, sleep = ms => new Promise(r => setTimeout(r, ms).unref?.()) } = {}) {
  return async (text, { sessionKey, key, onSettled } = {}) => {
    if (typeof sessionKey !== 'string' || !sessionKey.startsWith('agent:main:')) return 'failed: no conversation to run in';
    // One attempt = one key. A retry of the same task event (/webcontinue after a stale run) must not be answered from
    // the gateway's idempotency cache, nor have its reply dropped as "already sent".
    key = String(key) + ':' + Date.now().toString(36);
    const run = gateway('agent', { agentId: 'main', sessionKey, message: text, deliver: false, idempotencyKey: 'procurator-run:' + key }, 60000)
      // Their own conversation: a run past the wait is reported, never stopped (a long form fill keeps going; its reply is lost).
      .then(first => awaitRun(gateway, first, { timeoutMs, sessionKey, stopOnDeadline: false }))
      .then(async ({ text: out }) => {
        if (out && !/^NO_REPLY\b/.test(out)) {
          const parts = chunks(out);
          for (let i = 0; i < parts.length; i++) {
            await gateway('send', { channel: 'telegram', to: owner, message: parts[i], idempotencyKey: `procurator-reply:${key}:${i}` });
          }
        }
        return 'completed';
      });
    run.then(s => onSettled?.(s), e => onSettled?.('failed: ' + String(e?.message || e).slice(0, 160)));
    return Promise.race([run, sleep(quickMs).then(() => 'started')]).catch(e => 'failed: ' + String(e?.message || e).slice(0, 160));
  };
}
