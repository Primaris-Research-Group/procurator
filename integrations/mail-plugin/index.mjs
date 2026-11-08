import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';
import { registerMail } from './register.mjs';
import { withConnectorGate } from './connector-gate.mjs';
export default definePluginEntry({id:'procurator-mail', name:'PROCURATOR Mail', description:'Private drafts and exact-content Telegram owner approval for Gmail sending.', register(api){ registerMail(withConnectorGate(api,{plugin:'procurator-mail'})); }});
