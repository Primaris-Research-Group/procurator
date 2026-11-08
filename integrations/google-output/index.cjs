#!/usr/bin/env node
'use strict';
const readline = require('node:readline');
const { tools, productionService } = require('./core.cjs');
let service;
try { service = productionService(); } catch { process.stderr.write('Google output workspace configuration is unavailable.\n'); process.exit(1); }
function reply(id, result, error) {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, ...(error ? { error } : { result }) }) + '\n');
}
async function dispatch(message) {
  if (message.id === undefined) return;
  if (message.method === 'initialize') {
    reply(message.id, { protocolVersion: ['2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'].includes(message.params?.protocolVersion) ? message.params.protocolVersion : '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'procurator-google-output', version: '1.0.0' }, instructions: 'Use these tools only for assignments from the authenticated owner. All writes are confined to the assistant workspace. Use a stable request_id for each exact action. An uncertain or failed write needs operator review; never retry it with a new request_id. Documents contain literal text and standard heading/list styles. Editing existing documents, sharing, deleting, sending email, and calling are unavailable.' });
  } else if (message.method === 'ping') reply(message.id, {});
  else if (message.method === 'tools/list') reply(message.id, { tools });
  else if (message.method === 'tools/call') {
    try {
      const result = await service.call(message.params?.name, message.params?.arguments ?? {});
      reply(message.id, { content: [{ type: 'text', text: JSON.stringify(result) }], isError: false });
    } catch (error) { reply(message.id, { content: [{ type: 'text', text: error.message }], isError: true }); }
  } else reply(message.id, null, { code: -32601, message: 'Method not found' });
}
// Process one request at a time; writes are also locked across server instances.
let queue = Promise.resolve();
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', line => {
  if (Buffer.byteLength(line) > 262144) { reply(null, null, { code: -32600, message: 'Request too large' }); return; }
  let message;
  try { message = JSON.parse(line); } catch { reply(null, null, { code: -32700, message: 'Invalid JSON' }); return; }
  if (!message || typeof message !== 'object' || Array.isArray(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') { reply(null, null, { code: -32600, message: 'Invalid request' }); return; }
  queue = queue.then(() => dispatch(message)).catch(() => { if (message.id !== undefined) reply(message.id, null, { code: -32603, message: 'Connector request failed' }); });
});
