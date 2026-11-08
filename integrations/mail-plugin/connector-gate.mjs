// Connector gate (0.25.0; audiences 0.27.0). Source of truth: integrations/web-plugin/connector-gate.mjs; mail-plugin and voice-plugin carry
// byte-identical copies (connector-gate.test.mjs checks). Policy: connector-policy.json in the same directory, generated from
// integrations/web/connectors/*.json by `python3 integrations/web/connectors.py policy`.
//
// Every tool a plugin registers goes through here:
//  - a tool no connector manifest declares is still registered (so the model sees the refusal) but refuses to run;
//  - an outbound tool (its arguments may carry text to a third party) first asks the web broker whether the text carries
//    labeled health values and, if so, whether its destination is a recipient a health grant allows (health_taint_check).
//    If the check cannot run, the tool refuses: the health promise does not fail open.
import fs from 'node:fs';
import net from 'node:net';

export function loadPolicy(url = new URL('./connector-policy.json', import.meta.url)) {
  try { return JSON.parse(fs.readFileSync(url, 'utf8')); } catch { return null; }
}

export function webCheck(args, socketPath = '/run/procurator-web/tools.sock', timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const s = net.createConnection(socketPath); let data = '';
    const timer = setTimeout(() => { s.destroy(); reject(new Error('health check timed out')); }, timeoutMs);
    s.on('connect', () => s.write(JSON.stringify({ action: 'health_taint_check', args }) + '\n'));
    s.on('data', d => { data += d; if (data.includes('\n')) { clearTimeout(timer); s.end(); try { const r = JSON.parse(data.split('\n')[0]); r.ok ? resolve(r.result) : reject(new Error(String(r.error))); } catch (e) { reject(e); } } });
    s.on('error', e => { clearTimeout(timer); reject(e); });
  });
}

const refuse = text => ({ content: [{ type: 'text', text }], isError: true });
export const edition = () => (String(process.env.PROCURATOR_EDITION || 'standard').toLowerCase() === 'x' ? 'x' : 'standard');
const SESSION_DEST = new Set(['id', 'session_id']);

// Text of the arguments without the request id and other bookkeeping, so the check sees what would actually leave.
export function outboundText(args) {
  const skip = new Set(['request_id', 'id', 'session_id', 'look_id', 'target', 'ref']);
  const parts = [];
  const walk = (v, k, depth) => {
    if (depth > 6 || parts.length > 400) return;
    if (typeof v === 'string') { if (!skip.has(k)) parts.push(v); }
    else if (Array.isArray(v)) v.forEach(x => walk(x, k, depth + 1));
    else if (v && typeof v === 'object') for (const [kk, vv] of Object.entries(v)) walk(vv, kk, depth + 1);
  };
  walk(args, '', 0);
  return parts.join('\n').slice(0, 60000);
}

export function gateTool(tool, policy, { plugin = 'plugin', check = webCheck } = {}) {
  if (!tool || typeof tool !== 'object' || !tool.name || typeof tool.execute !== 'function') return tool;
  const spec = policy?.tools?.[tool.name];
  const audit = policy?.mode === 'audit';
  const run = tool.execute.bind(tool);
  if (!spec) {
    if (audit) { console.warn(`[connectors] ${plugin}: tool ${tool.name} is not declared (audit mode)`); return tool; }
    return { ...tool, async execute() { return refuse(`Tool ${tool.name} is not declared by any connector manifest, so it does not run (deny by default). Nothing was done.`); } };
  }
  // Editions (0.30.0): a tool from an "edition": "x" connector (the coding orchestrator) exists only on PROCURATOR-X.
  if (spec.edition === 'x' && edition() !== 'x') {
    return { ...tool, async execute() { return refuse(`Tool ${tool.name} is part of PROCURATOR-X (the developer edition); this box runs the standard edition, so it does not run. Nothing was done.`); } };
  }
  if (!spec.outbound) return tool;
  return {
    ...tool,
    async execute(id, args, ...rest) {
      const a = args && typeof args === 'object' ? args : {};
      const key = spec.destination;
      const destination = key ? a[key] : undefined;
      // An own-account change with no other recipient (a calendar event without guests) has no one to disclose to.
      const empty = destination === undefined || destination === null || destination === '' || (Array.isArray(destination) && !destination.length);
      if (spec.risk === 'own_write' && empty && !['public', 'workspace'].includes(spec.audience)) return run(id, args, ...rest);
      let verdict;
      try {
        // 0.27.0: the audience goes with the text; for public and workspace audiences labeled health text is refused outright.
        verdict = await check({ tool: tool.name, text: outboundText(a), destination: empty ? null : destination, destination_kind: SESSION_DEST.has(key) ? 'session' : undefined, ...(spec.audience ? { audience: spec.audience } : {}) });
      } catch (e) {
        if (audit) return run(id, args, ...rest);
        return refuse(`The health-data check could not run (${String(e?.message || e).slice(0, 80)}), so ${tool.name} did not run. Nothing was sent. Try again in a minute.`);
      }
      if (verdict?.tainted && !verdict.allowed) {
        if (audit) { console.warn(`[connectors] ${plugin}: ${tool.name} would carry health details to an unlisted recipient (audit mode)`); return run(id, args, ...rest); }
        return refuse(verdict.reason || 'This would send health details to a recipient no health grant allows. Nothing was sent.');
      }
      return run(id, args, ...rest);
    }
  };
}

export function withConnectorGate(api, { plugin = 'plugin', policy = loadPolicy(), check = webCheck } = {}) {
  const pol = policy || { mode: 'enforce', tools: {} };
  if (!policy) console.warn(`[connectors] ${plugin}: connector-policy.json missing; every tool refuses until it is deployed`);
  const wrap = t => Array.isArray(t) ? t.map(x => gateTool(x, pol, { plugin, check })) : gateTool(t, pol, { plugin, check });
  return new Proxy(api, {
    get(target, prop) {
      if (prop === 'registerTool') return (tool, opts) => target.registerTool(typeof tool === 'function' ? (ctx => wrap(tool(ctx))) : wrap(tool), opts);
      const v = Reflect.get(target, prop);
      return typeof v === 'function' ? v.bind(target) : v;
    }
  });
}
