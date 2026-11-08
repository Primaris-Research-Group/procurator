// /connectors and assistant_connectors (0.25.0): what PROCURATOR is connected to, per connector manifest
// (integrations/web/connectors/*.json): its tools by risk class, which ones reach third parties (they need their approval),
// and any manifest problem the broker found at start-up. Read-only; it grants nothing.
const RISK_WORDS = { read: 'look up', draft: 'prepare', own_write: 'change their own data', external: 'reach others (after approval)', public: 'post publicly (after the publish preview)', owner: 'notify or ask them' };

export function connectorsText(s) {
  const rows = (s?.connectors || []).map(c => {
    const risks = Object.entries(c.risks || {}).map(([k, n]) => `${n} ${RISK_WORDS[k] || k}`).join(', ');
    return `• ${c.title}${c.class === 'health' ? ' (health)' : ''}${c.source === 'pack' ? ' [pack]' : ''} — ${c.tools ? risks : 'service connector'}`;
  });
  const out = [`Connectors (${rows.length}, gate: ${s?.mode || 'unknown'}):`, ...rows];
  if (s?.problems?.length) out.push('', 'Manifest problems (those connectors are not loaded):', ...s.problems.slice(0, 8).map(p => '• ' + p));
  if (s?.refused_recent?.length) out.push('', 'Recently refused (undeclared): ' + s.refused_recent.join(', '));
  out.push('', 'Everything that reaches another person (send, call, submit, book, share, text) or the public (a post) still waits for your approval in Telegram.');
  return out.join('\n');
}

export function registerConnectors(api, call, ownerContext) {
  api.registerTool({
    name: 'assistant_connectors', label: 'assistant_connectors',
    description: 'List the connectors PROCURATOR has (mail, calendar, browser, phone, health, research…), the tools each one provides by risk class (look up, prepare, change their own data, reach others after approval) and any connector that failed to load. Use when the owner asks what you are connected to or what an integration can do.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    async execute() { try { return { content: [{ type: 'text', text: JSON.stringify(await call('connector_list', {})) }] }; } catch (e) { return { content: [{ type: 'text', text: e.message }], isError: true }; } }
  });
  api.registerCommand({
    name: 'connectors', description: 'What PROCURATOR is connected to, and what each connector may do', channels: ['telegram'], requireAuth: true, acceptsArgs: false,
    async handler(ctx) { try { ownerContext(ctx); return { text: connectorsText(await call('connector_list', {}, true)) }; } catch (e) { return { text: String(e?.message || e) }; } }
  });
}

// /mcprun ID (0.25.0): start a task an MCP client (Claude Desktop, Claude Code, ChatGPT) queued through bin/procurator-mcp.py.
// The request text then runs in their own conversation like anything the owner types; approvals are unchanged.
export function registerMcpRun(api, call, ownerContext, wake) {
  api.registerCommand({
    name: 'mcprun', description: 'Start a task Claude Desktop or another MCP client asked PROCURATOR to do: /mcprun ID', channels: ['telegram'], requireAuth: true, acceptsArgs: true,
    async handler(ctx) {
      try {
        ownerContext(ctx);
        const id = String(ctx.args || '').trim();
        if (!/^[a-f0-9]{8}$/.test(id)) return { text: 'Use /mcprun ID (the 8-character id in the request message).' };
        const r = await call('mcp_accept', { id }, true);
        await wake?.(`REQUEST FROM ${String(r.client).toUpperCase()} (via MCP), started by the owner with /mcprun ${r.request_id}. Treat it as if the owner typed it; every send, call, booking or submission still needs their approval as usual:\n\n${r.text}`, 'procurator-mcp:' + r.request_id, 'mcp request');
        return { text: `Started request ${r.request_id} from ${r.client}. I'll work on it here.` };
      } catch (e) { return { text: String(e?.message || e) }; }
    }
  });
}
