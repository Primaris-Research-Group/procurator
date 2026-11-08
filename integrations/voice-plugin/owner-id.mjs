// The owner's Telegram user id (0.30.0). Same rule as web-plugin/owner-config.mjs ownerId(): PROCURATOR_OWNER_ID from the
// environment (compose passes it from .env), else the primary channel in config/owner.json, else '' so every owner check
// fails closed and nothing is sent. Kept dependency-free so the mail and voice plugins stay single-file.
import fs from 'node:fs';

export const OWNER_FILE = process.env.PROCURATOR_OWNER_FILE || '/opt/procurator/config/owner.json';

export function ownerId(file = OWNER_FILE) {
  const env = String(process.env.PROCURATOR_OWNER_ID || '').trim();
  if (/^\d+$/.test(env)) return env;
  try {
    const d = JSON.parse(fs.readFileSync(file, 'utf8'));
    const chans = Array.isArray(d.channels) && d.channels.length ? d.channels : [{ id: d.telegram_id, primary: true }];
    const prim = chans.find(c => c && c.primary) || chans[0];
    if (prim && prim.id != null) return String(prim.id);
  } catch { /* fall through */ }
  // Under `node --test` with no configured owner, the fixture id the test suites use; never set outside the test runner.
  return process.env.NODE_TEST_CONTEXT ? '100000001' : '';
}
