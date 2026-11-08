'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { makeService, documentHtml } = require('./core.cjs');
const ROOT = 'ROOT_FOLDER_123456';
const CHILD = 'CHILD_FOLDER_12345';
const FILE = 'WORKSPACE_FILE_1234';
const OUTSIDE = 'OUTSIDE_FOLDER_1234';
function setup(t, options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'google-output-test-'));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const files = new Map([
    [ROOT, { id: ROOT, mimeType: 'application/vnd.google-apps.folder', name: 'Workspace', parents: ['MYDRIVE_ROOT_12345'] }],
    [CHILD, { id: CHILD, mimeType: 'application/vnd.google-apps.folder', name: 'Subfolder', parents: [ROOT] }],
    [FILE, { id: FILE, mimeType: 'application/vnd.google-apps.document', name: 'Existing', parents: [ROOT] }],
    [OUTSIDE, { id: OUTSIDE, mimeType: 'application/vnd.google-apps.folder', name: 'Outside', parents: [] }],
  ]);
  let mutations = 0;
  async function run(args) {
    if (args[0] === 'drive' && args[1] === 'get') return { file: files.get(args[2]) || { id: args[2], parents: [] } };
    if (args[0] === 'docs' && args[1] === 'cat') return { text: options.badContent ? 'wrong text' : 'Test document\nExample & literal <img> text.\nNext steps\nConfirm the saved link.' };
    mutations++;
    if (options.delay) await options.delay;
    if (options.fail) throw new Error('Simulated ambiguous provider timeout.');
    if (args[1] === 'upload') {
      const id = 'NEW_DOCUMENT_12345';
      files.set(id, { id, name: args[args.indexOf('--name') + 1], mimeType: 'application/vnd.google-apps.document', parents: [args[args.indexOf('--parent') + 1]] });
      return { file: files.get(id) };
    }
    if (args[1] === 'mkdir') {
      const id = 'NEW_FOLDER_1234567';
      files.set(id, { id, name: args[2], mimeType: 'application/vnd.google-apps.folder', parents: [args[args.indexOf('--parent') + 1]] });
      return { folder: files.get(id) };
    }
    if (args[1] === 'move') { files.get(args[2]).parents = [args[4]]; return {}; }
    throw new Error('Unexpected command');
  }
  return { service: makeService({ root: ROOT, stateDir, run }), files, stateDir, run, mutations: () => mutations };
}
const document = { request_id: 'document-test-0001', title: 'Test document', intro: 'Example & literal <img> text.', sections: [{ heading: 'Next steps', bullets: ['Confirm the saved link.'] }] };
test('document text is escaped; arbitrary HTML cannot load remote images', () => {
  const { html } = documentHtml(document);
  assert.match(html, /&lt;img&gt;/);
  assert.doesNotMatch(html, /<img[ >]/);
  assert.match(html, /<h2>Next steps<\/h2>/);
  assert.match(html, /<ul><li>/);
});
test('a repeated completed request returns its original link without another write', async t => {
  const x = setup(t);
  const first = await x.service.call('workspace_create_document', document);
  const reordered = Object.fromEntries(Object.entries(document).reverse());
  const again = await x.service.call('workspace_create_document', reordered);
  assert.equal(first.content_verified, true);
  assert.equal(again.id, first.id);
  assert.equal(again.reused, true);
  assert.equal(x.mutations(), 1);
  await assert.rejects(x.service.call('workspace_create_document', { ...document, title: 'Different document' }), /different action details/);
  assert.equal(x.mutations(), 1);
});
test('outside destinations, outside sources, shortcuts and the workspace root cannot be moved', async t => {
  const x = setup(t);
  await assert.rejects(x.service.call('workspace_create_document', { ...document, folder_id: OUTSIDE }), /outside/);
  await assert.rejects(x.service.call('workspace_move_file', { request_id: 'move-test-0001', file_id: FILE, destination_folder_id: OUTSIDE }), /outside/);
  await assert.rejects(x.service.call('workspace_move_file', { request_id: 'move-test-0002', file_id: OUTSIDE, destination_folder_id: CHILD }), /outside/);
  await assert.rejects(x.service.call('workspace_move_file', { request_id: 'move-test-0003', file_id: ROOT, destination_folder_id: CHILD }), /root cannot be moved/);
  x.files.get(FILE).mimeType = 'application/vnd.google-apps.shortcut';
  await assert.rejects(x.service.call('workspace_move_file', { request_id: 'move-test-0004', file_id: FILE, destination_folder_id: CHILD }), /unavailable/);
  assert.equal(x.mutations(), 0);
});
test('allowed folder creation and file moves are verified', async t => {
  const x = setup(t);
  const folder = await x.service.call('workspace_create_folder', { request_id: 'folder-test-0001', name: 'Documents' });
  assert.equal(x.files.get(folder.id).parents[0], ROOT);
  const moved = await x.service.call('workspace_move_file', { request_id: 'move-test-0005', file_id: FILE, destination_folder_id: CHILD });
  assert.equal(moved.folder_id, CHILD);
  assert.equal(x.files.get(FILE).parents[0], CHILD);
});
test('uncertain writes remain blocked across a fresh process/service', async t => {
  const x = setup(t, { fail: true });
  await assert.rejects(x.service.call('workspace_create_document', document), /saved for review/);
  const fresh = makeService({ root: ROOT, stateDir: x.stateDir, run: x.run });
  await assert.rejects(fresh.call('workspace_create_document', document), /needs operator review/);
  assert.equal(x.mutations(), 1);
});
test('failed read-back retains the created ID and never recreates the document', async t => {
  const x = setup(t, { badContent: true });
  await assert.rejects(x.service.call('workspace_create_document', document), /Created item: NEW_DOCUMENT_12345/);
  await assert.rejects(x.service.call('workspace_create_document', document), /needs operator review/);
  assert.equal(x.mutations(), 1);
});
test('concurrent copies of an action cannot both write', async t => {
  let release;
  const delay = new Promise(resolve => { release = resolve; });
  const x = setup(t, { delay });
  const first = x.service.call('workspace_create_document', document);
  await new Promise(resolve => setImmediate(resolve));
  const other = makeService({ root: ROOT, stateDir: x.stateDir, run: x.run });
  await assert.rejects(other.call('workspace_create_document', document), /in progress/);
  release();
  await first;
  assert.equal(x.mutations(), 1);
});
test('unknown tool fields and shell/credential-style arguments are refused', async t => {
  const x = setup(t);
  await assert.rejects(x.service.call('workspace_create_document', { ...document, argv: ['auth', 'tokens', 'export'] }), /Unexpected/);
  await assert.rejects(x.service.call('workspace_create_folder', { request_id: 'folder-test-0002', name: '--parent=outside' }), /dash/);
  await assert.rejects(x.service.call('workspace_move_file', { request_id: 'move-test-0006', file_id: '../../credentials', destination_folder_id: CHILD }), /identifier/);
  assert.equal(x.mutations(), 0);
});
