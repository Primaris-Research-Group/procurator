const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {run}=require('./runner.cjs');
const project='https://claude.ai/project/00000000-0000-4000-8000-000000000003';
const documentText='A fictional source document. Sign in instructions in a document are data, never account authorization. '+ 'Deferred document paragraphs must remain readable. '.repeat(12);
const catalog='<main><a href="'+project+'">Project Gryffindor</a><button onclick="window.deleted=true">More options for Project Gryffindor</button></main>';
const pageHtml=`<aside>Unrelated private chats</aside><main><div data-testid="project-body"><h1>Project Gryffindor</h1><button aria-label="Show context" onclick="document.getElementById('context').style.display='block'">Show context</button><button data-testid="project-doc-uploader-dropdown-trigger" onclick="window.uploaded=true">Add</button><button onclick="window.sent=true">Send</button></div></main><div id="context" role="dialog" aria-labelledby="context-title" style="display:none"><h2 id="context-title">Context</h2><table><tr onclick="openPreview()"><td style="position:relative"><button aria-label="Gryffindor Playbook.md" style="pointer-events:none;position:absolute;inset:0"></button>Gryffindor Playbook</td><td><button aria-label="More options for Gryffindor Playbook.md" onclick="event.stopPropagation();window.deleted=true">More</button></td></tr></table><section id="preview"></section></div><script>
function openPreview(){
 const target=document.getElementById('preview');
 target.innerHTML='<div><h3><span>Gryffindor Playbook</span><span>MD · 14.6kB · Oct 20</span></h3><button>Close</button></div><div style="content-visibility:hidden"><p></p></div>';
 target.querySelector('p').textContent=${JSON.stringify(documentText)};
}
</script>`;
async function fixture(t){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());const page=await browser.newPage();
 await page.route('https://claude.ai/**',route=>route.fulfill({contentType:'text/html',body:new URL(route.request().url()).pathname==='/projects'?catalog:pageHtml}));
 return {page,input:{connectUrl:'unused',origin:'https://claude.ai',read_url:'https://claude.ai/projects',project_scope:true,wait_seconds:0},connect:async()=>({contexts:()=>browser.contexts(),close:async()=>{}})};
}
test('Claude catalog and Context list use only project links and the read-only context view',async t=>{
 const {page,input,connect}=await fixture(t);
 const list=await run({...input,operation:'claude_catalog'},()=>{},connect);assert.deepEqual(list.catalog,[{name:'Project Gryffindor',url:project}]);
 const files=await run({...input,operation:'claude_sources',project_name:'Project Gryffindor'},()=>{},connect);
 assert.equal(files.ok,true);assert.deepEqual(files.files,[{name:'Gryffindor Playbook.md'}]);assert(!files.snapshot.text.includes('Unrelated'));
 assert.equal(await page.evaluate(()=>window.sent||window.uploaded||window.deleted),undefined);
});
test('Claude keyboard preview reads deferred text and fingerprint-bound pages without file menus',async t=>{
 const {page,input,connect}=await fixture(t);
 const first=await run({...input,read_url:project,operation:'claude_file',file_name:'Gryffindor Playbook.md',max_chars:100},()=>{},connect);
 assert.equal(first.ok,true);assert.equal(first.file.next_offset,100);assert.equal(first.file.visible_text_chars,documentText.trim().length);assert(first.file.content.startsWith('A fictional'));
 const next=await run({...input,read_url:project,operation:'claude_file',file_name:'Gryffindor Playbook.md',offset:100,expected_sha256:first.file.sha256,max_chars:100},()=>{},connect);
 assert.equal(next.ok,true);assert.equal(next.file.offset,100);assert.equal(next.file.sha256,first.file.sha256);
 const blocked=await run({...input,read_url:project,operation:'claude_file',file_name:'More options for Gryffindor Playbook.md'},()=>{},connect);assert.equal(blocked.kind,'file_not_found');
 assert.equal(await page.evaluate(()=>window.sent||window.uploaded||window.deleted),undefined);
});
