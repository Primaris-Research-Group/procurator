#!/usr/bin/env node
// Fixed read-only MCP surface. Never pass model-supplied shell arguments.
'use strict';
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const base = '/opt/procurator';
const tools = [
  'gmail_search', 'gmail_get_message', 'gmail_get_thread',
  'gmail_list_labels', 'drive_search', 'drive_get', 'docs_get',
];
const extra = process.argv.slice(2);
if (extra.length && !(extra.length === 1 && extra[0] === '--list-tools')) {
  process.stderr.write('Unsupported Google connector option.\n');
  process.exit(2);
}
try {
  const account = JSON.parse(fs.readFileSync(`${base}/google/account.json`, 'utf8')).email;
  if (typeof account !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(account)) {
    throw new Error('Invalid account configuration');
  }
  const env = {
    ...process.env,
    GOG_HOME: `${base}/google/home`,
    GOG_CLIENT: 'procurator-admin',
    GOG_ACCOUNT: account,
    GOG_KEYRING_BACKEND: 'file',
    GOG_KEYRING_PASSWORD: fs.readFileSync(`${base}/google/keyring-password`, 'utf8'),
  };
  for (const key of ['GOG_ACCESS_TOKEN', 'GOG_AUTH_MODE', 'GOG_GOOGLE_API_BASE_URL']) delete env[key];
  const args = [
    '--home', env.GOG_HOME, '--client', env.GOG_CLIENT, '--account', account,
    '--readonly', '--gmail-no-send', '--no-input', '--wrap-untrusted',
    'mcp', '--allow-tool', tools.join(','), '--timeout-seconds', '30',
    '--max-output-bytes', '65536', ...extra,
  ];
  const child = spawn(`${base}/vendor/gog-0.47.1`, args, { env, stdio: 'inherit' });
  child.once('error', () => {
    process.stderr.write('Google connector failed to start.\n');
    process.exitCode = 1;
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
  child.once('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
} catch {
  process.stderr.write('Google connector configuration is unavailable.\n');
  process.exitCode = 1;
}
