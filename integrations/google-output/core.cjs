'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { promisify } = require('node:util');
const execFile = promisify(require('node:child_process').execFile);
const BASE = '/opt/procurator';
const FOLDER = 'application/vnd.google-apps.folder';
const DOC = 'application/vnd.google-apps.document';
const tools = [
  { name: 'workspace_info', description: 'Get the assistant output folder link and write boundaries.', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'workspace_create_document', description: 'Create a NEW Google Doc in the assistant workspace. Supply structured literal text, not HTML or Markdown. Saves a request record and verifies the document text and folder. Reuse request_id for retries; never issue a new request_id to retry an uncertain outcome. Does not edit an existing document.', inputSchema: { type: 'object', properties: {
    request_id: { type: 'string', description: 'Stable unique ID for this exact owner-requested action; reuse unchanged on retries.' },
    title: { type: 'string' }, intro: { type: 'string' }, folder_id: { type: 'string', description: 'Optional destination inside the assistant workspace; omit for its root.' },
    sections: { type: 'array', minItems: 1, maxItems: 20, items: { type: 'object', properties: { heading: { type: 'string' }, paragraphs: { type: 'array', items: { type: 'string' } }, bullets: { type: 'array', items: { type: 'string' } } }, additionalProperties: false } },
  }, required: ['request_id', 'title', 'sections'], additionalProperties: false } },
  { name: 'workspace_create_folder', description: 'Create a subfolder inside the assistant workspace. Reuse the same request_id on retries.', inputSchema: { type: 'object', properties: { request_id: { type: 'string' }, name: { type: 'string' }, parent_folder_id: { type: 'string' } }, required: ['request_id', 'name'], additionalProperties: false } },
  { name: 'workspace_move_file', description: 'Move one existing file between folders INSIDE the assistant workspace. Cannot import from outside, move the workspace root, change sharing, or delete anything. Reuse request_id on retries.', inputSchema: { type: 'object', properties: { request_id: { type: 'string' }, file_id: { type: 'string' }, destination_folder_id: { type: 'string' } }, required: ['request_id', 'file_id', 'destination_folder_id'], additionalProperties: false } },
];
for (const tool of tools) tool.annotations = {
  readOnlyHint: tool.name === 'workspace_info',
  destructiveHint: tool.name === 'workspace_move_file',
  idempotentHint: true,
  openWorldHint: true,
};

function object(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(k => !allowed.includes(k))) throw new Error('Unexpected or invalid tool fields.');
}
function text(value, label, max = 20000) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(value)) throw new Error(`Invalid ${label}.`);
  return value;
}
function id(value) { if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{10,200}$/.test(value)) throw new Error('Invalid Drive identifier.'); return value; }
function escape(value) { return value.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function canonical(value) { return JSON.stringify(value && typeof value === 'object' ? Array.isArray(value) ? value.map(v => JSON.parse(canonical(v))) : Object.fromEntries(Object.keys(value).sort().map(k => [k, JSON.parse(canonical(value[k]))])) : value); }
function strings(value) { return typeof value === 'string' ? [value] : value && typeof value === 'object' ? Object.values(value).flatMap(strings) : []; }
function normalized(value) { return value.normalize('NFC').replace(/\s+/g, ' ').trim(); }
function documentHtml(args) {
  text(args.title, 'title', 200);
  if (!Array.isArray(args.sections) || !args.sections.length || args.sections.length > 20) throw new Error('Provide 1 to 20 document sections.');
  const expected = [args.title];
  let html = `<h1>${escape(args.title)}</h1>`;
  const paragraph = s => { text(s, 'document paragraph'); expected.push(s); return `<p>${escape(s).replace(/\r?\n/g, '<br>')}</p>`; };
  if (args.intro !== undefined) html += paragraph(args.intro);
  for (const section of args.sections) {
    object(section, ['heading', 'paragraphs', 'bullets']);
    if (section.heading !== undefined) { text(section.heading, 'heading', 200); expected.push(section.heading); html += `<h2>${escape(section.heading)}</h2>`; }
    if (!['paragraphs', 'bullets'].some(k => Array.isArray(section[k]) && section[k].length)) throw new Error('Each section needs paragraphs or bullets.');
    for (const key of ['paragraphs', 'bullets']) {
      if (section[key] === undefined) continue;
      if (!Array.isArray(section[key]) || section[key].length > 100) throw new Error('Invalid section text list.');
      if (key === 'paragraphs') html += section[key].map(paragraph).join('');
      else html += '<ul>' + section[key].map(s => { text(s, 'bullet'); expected.push(s); return `<li>${escape(s)}</li>`; }).join('') + '</ul>';
    }
  }
  if (expected.join('').length > 60000) throw new Error('Document exceeds the first-version 60,000-character limit.');
  return { html: '<html><head><meta charset="utf-8"><style>body{font-family:Arial;font-size:11pt}h1,h2{page-break-after:avoid}</style></head><body>' + html + '</body></html>', expected };
}

function makeService({ root, stateDir, run }) {
  id(root);
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  async function metadata(fileId) {
    const data = await run(['drive', 'get', id(fileId), '--fields', 'id,name,mimeType,parents,trashed,driveId']);
    const file = data.file || data;
    if (file.id !== fileId || file.trashed || file.driveId || file.mimeType === 'application/vnd.google-apps.shortcut') throw new Error('File is unavailable or outside the supported personal workspace.');
    return file;
  }
  async function within(fileId, folderOnly = false) {
    let current = await metadata(fileId);
    const first = current;
    if (folderOnly && current.mimeType !== FOLDER) throw new Error('Destination must be a folder.');
    const seen = new Set();
    for (let n = 0; n < 32; n++) {
      if (seen.has(current.id)) break;
      seen.add(current.id);
      if (current.id === root) {
        if (current.mimeType !== FOLDER) throw new Error('Configured workspace is not a folder.');
        return first;
      }
      if (!Array.isArray(current.parents) || current.parents.length !== 1) break;
      current = await metadata(current.parents[0]);
    }
    throw new Error('Operation refused: file or folder is outside the assistant workspace.');
  }
  function save(file, data) {
    const tmp = `${file}.${crypto.randomUUID()}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data), { mode: 0o600 });
    fs.renameSync(tmp, file);
  }
  async function once(name, args, action) {
    if (typeof args.request_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_:-]{7,79}$/.test(args.request_id)) throw new Error('request_id must be a stable 8 to 80 character action identifier.');
    const digest = crypto.createHash('sha256').update(canonical({ name, args })).digest('hex');
    const key = crypto.createHash('sha256').update(args.request_id).digest('hex');
    const file = path.join(stateDir, `${key}.json`);
    const lock = `${file}.lock`;
    let fd;
    try { fd = fs.openSync(lock, 'wx', 0o600); } catch (e) { if (e.code === 'EEXIST') throw new Error('This request is in progress or needs operator recovery. Do not retry with a new request_id.'); throw e; }
    try {
      if (fs.existsSync(file)) {
        const old = JSON.parse(fs.readFileSync(file, 'utf8'));
        if (old.digest !== digest) throw new Error('request_id is already bound to different action details.');
        if (old.status === 'completed') return { ...old.result, reused: true };
        throw new Error(`Previous request needs operator review; do not repeat it with a new ID.${old.created_id ? ' Created item: ' + old.created_id : ''}`);
      }
      const record = { request_id: args.request_id, digest, tool: name, status: 'started', started_at: new Date().toISOString() };
      save(file, record);
      const checkpoint = itemId => { record.created_id = id(itemId); save(file, record); };
      try {
        const result = await action(checkpoint);
        record.status = 'completed'; record.result = result; record.completed_at = new Date().toISOString(); save(file, record);
        return result;
      } catch (error) {
        record.status = 'needs_review'; save(file, record);
        throw new Error(`${error.message} Request saved for review; do not retry with a new ID.${record.created_id ? ' Created item: ' + record.created_id : ''}`);
      }
    } finally { fs.closeSync(fd); fs.unlinkSync(lock); }
  }
  async function call(name, args) {
    const spec = tools.find(t => t.name === name);
    if (!spec) throw new Error('Unknown tool.');
    object(args, Object.keys(spec.inputSchema.properties));
    if (name === 'workspace_info') {
      await within(root, true);
      return { folder_id: root, url: `https://drive.google.com/drive/folders/${root}`, allowed: ['create documents', 'create subfolders', 'move files within this workspace'], disallowed: ['outside-workspace writes', 'editing existing documents', 'deletion', 'sharing changes', 'email sending', 'calls'] };
    }
    if (name === 'workspace_create_document') {
      const { html, expected } = documentHtml(args);
      const parent = args.folder_id === undefined ? root : id(args.folder_id);
      await within(parent, true);
      return once(name, args, async checkpoint => {
        const temp = fs.mkdtempSync(path.join(stateDir, 'document-'));
        const source = path.join(temp, 'content.html');
        fs.writeFileSync(source, html, { mode: 0o600 });
        try {
          const created = await run(['drive', 'upload', source, '--name', args.title, '--parent', parent, '--mime-type', 'text/html', '--convert-to', 'doc']);
          const fileId = id((created.file || created).id); checkpoint(fileId);
          const file = await within(fileId);
          if (file.mimeType !== DOC || !file.parents.includes(parent)) throw new Error('Saved document metadata did not match the requested destination.');
          const content = normalized(strings(await run(['docs', 'cat', fileId, '--max-bytes', '500000'])).join('\n'));
          if (!expected.every(s => content.includes(normalized(s)))) throw new Error('Saved document content could not be verified.');
          return { id: fileId, title: file.name, folder_id: parent, url: `https://docs.google.com/document/d/${fileId}/edit`, content_verified: true };
        } finally { fs.rmSync(temp, { recursive: true, force: true }); }
      });
    }
    if (name === 'workspace_create_folder') {
      text(args.name, 'folder name', 200);
      if (args.name.startsWith('-')) throw new Error('Folder name cannot start with a dash.');
      const parent = args.parent_folder_id === undefined ? root : id(args.parent_folder_id);
      await within(parent, true);
      return once(name, args, async checkpoint => {
        const created = await run(['drive', 'mkdir', args.name, '--parent', parent]);
        const fileId = id((created.folder || created.file || created).id); checkpoint(fileId);
        const file = await within(fileId, true);
        if (!file.parents.includes(parent)) throw new Error('Folder destination could not be verified.');
        return { id: fileId, name: file.name, url: `https://drive.google.com/drive/folders/${fileId}` };
      });
    }
    id(args.file_id); id(args.destination_folder_id);
    if (args.file_id === root) throw new Error('The assistant workspace root cannot be moved.');
    const file = await within(args.file_id);
    await within(args.destination_folder_id, true);
    if (file.mimeType === FOLDER) throw new Error('Moving folders is not enabled in this version. Move individual files instead.');
    return once(name, args, async () => {
      if (!file.parents.includes(args.destination_folder_id)) await run(['drive', 'move', args.file_id, '--parent', args.destination_folder_id]);
      const after = await within(args.file_id);
      if (!after.parents.includes(args.destination_folder_id)) throw new Error('File destination could not be verified.');
      return { id: after.id, name: after.name, folder_id: args.destination_folder_id, url: `https://drive.google.com/file/d/${after.id}/view` };
    });
  }
  return { call };
}

function productionService() {
  const google = path.join(BASE, 'google');
  const account = JSON.parse(fs.readFileSync(path.join(google, 'account.json'), 'utf8')).email;
  const root = JSON.parse(fs.readFileSync(path.join(google, 'workspace.json'), 'utf8')).folder_id;
  const env = { ...process.env, GOG_HOME: path.join(google, 'home'), GOG_CLIENT: 'procurator-admin', GOG_ACCOUNT: account, GOG_KEYRING_BACKEND: 'file', GOG_KEYRING_PASSWORD: fs.readFileSync(path.join(google, 'keyring-password'), 'utf8') };
  for (const key of ['GOG_ACCESS_TOKEN', 'GOG_AUTH_MODE', 'GOG_GOOGLE_API_BASE_URL']) delete env[key];
  const commands = 'drive.get,drive.mkdir,drive.upload,drive.move,docs.cat';
  const rootArgs = ['--home', env.GOG_HOME, '--client', env.GOG_CLIENT, '--account', account, '--json', '--no-input', '--gmail-no-send', '--enable-commands-exact', commands];
  async function run(args) {
    let output;
    try { output = await execFile(path.join(BASE, 'vendor/gog-0.47.1'), [...rootArgs, ...args], { env, timeout: 45000, maxBuffer: 1048576 }); }
    catch (error) {
      const diagnostics = path.join(google, 'operations', 'diagnostics');
      fs.mkdirSync(diagnostics, { recursive: true, mode: 0o700 });
      fs.writeFileSync(path.join(diagnostics, `${crypto.randomUUID()}.json`), JSON.stringify({ at: new Date().toISOString(), command: args.slice(0, 2), code: error.code, signal: error.signal, stderr: error.stderr }), { mode: 0o600 });
      throw new Error('Google request failed or timed out. The operator must check the saved request before retrying.');
    }
    try { return JSON.parse(output.stdout); } catch { return output.stdout; }
  }
  return makeService({ root, stateDir: path.join(google, 'operations'), run });
}
module.exports = { tools, makeService, productionService, documentHtml };
