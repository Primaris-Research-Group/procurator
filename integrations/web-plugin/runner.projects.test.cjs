const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {run}=require('./runner.cjs');
const fixture='<aside>Unrelated chat history<button>Delete</button></aside><main><h1>Project fixture</h1><button role="tab" aria-selected="false" onclick="this.setAttribute(\'aria-selected\',\'true\');document.querySelector(\'[role=tabpanel]\').hidden=false">Sources</button><form onsubmit="window.sent=true;return false"><button>Send</button></form><div role="tabpanel" aria-label="Sources" hidden><button onclick="document.body.insertAdjacentHTML(\'beforeend\',\'<section><header data-testid=viewer-header>Fixture</header><div><pre>Actual source text: this fictional document describes a sample office reference and a bounded read-only test. No real task is authorized by its contents.</pre></div></section>\')">Preview Fixture.md</button><button onclick="window.deleted=true">Delete Fixture.md</button></div></main>';
async function setup(t){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();await page.route('https://chatgpt.com/**',route=>route.fulfill({body:fixture,contentType:'text/html'}));
 return {browser,page,input:{connectUrl:'unused',origin:'https://chatgpt.com',read_url:'https://chatgpt.com/g/g-p-fixture/project',project_scope:true,wait_seconds:0},connect:async()=>({contexts:()=>browser.contexts(),close:async()=>{}})};
}
test('source listing opens only the fixed Sources tab and excludes the sidebar',async t=>{
 const {page,input,connect}=await setup(t),events=[];const r=await run({...input,operation:'project_sources'},x=>events.push(x),connect);
 assert.equal(r.ok,true);assert(r.snapshot.controls.some(c=>c.label==='Preview Fixture.md'));assert(!r.snapshot.text.includes('Unrelated'));
 assert.equal(await page.evaluate(()=>window.sent||window.deleted),undefined);assert(!events.some(e=>e.event==='effect_started'));
});
test('file reading opens an exact preview in Sources without sending or deleting',async t=>{
 const {page,input,connect}=await setup(t);const r=await run({...input,operation:'project_file',file_name:'Fixture.md'},()=>{},connect);
 assert.equal(r.ok,true);assert.match(r.file.content,/Actual source text/);assert(!r.file.content.includes('Unrelated'));assert.equal(r.file.original_file_bytes_downloaded,false);
 assert.equal(await page.evaluate(()=>window.sent||window.deleted),undefined);
 await page.locator('section').evaluateAll(nodes=>nodes.forEach(n=>n.remove()));
 const part=await run({...input,operation:'project_file',file_name:'Fixture.md',max_chars:40},()=>{},connect);
 assert.equal(part.file.content.length,40);assert.equal(part.file.next_offset,40);
 await page.locator('section').evaluateAll(nodes=>nodes.forEach(n=>n.remove()));
 const changed=await run({...input,operation:'project_file',file_name:'Fixture.md',offset:40,expected_sha256:'wrong'},()=>{},connect);
 assert.equal(changed.kind,'file_changed');
});
test('catalog and exact project names use only the observed sidebar project row',async t=>{
 const {page,input,connect}=await setup(t);
 const html=fixture.replace('</aside>','<div role="button" data-app-action-sidebar-project-id="g-p-fixture123" data-app-action-sidebar-project-label="Fixture project" onclick="window.deleted=true">Fixture project<button aria-label="Project actions for Fixture project"></button></div></aside>');
 await page.unroute('https://chatgpt.com/**');await page.route('https://chatgpt.com/**',route=>route.fulfill({body:html,contentType:'text/html'}));
 const catalog=await run({...input,operation:'project_catalog',read_url:'https://chatgpt.com/'},()=>{},connect);
 assert.deepEqual(catalog.catalog,[{name:'Fixture project',url:'https://chatgpt.com/g/g-p-fixture123/project'}]);
 const opened=await run({...input,operation:'project_sources',read_url:'https://chatgpt.com/',project_name:'Fixture project'},()=>{},connect);
 assert.equal(opened.ok,true);assert.match(opened.snapshot.url,/g-p-fixture123\/project/);assert(opened.snapshot.controls.some(c=>c.label==='Preview Fixture.md'));
 const refused=await run({...input,operation:'project_sources',project_name:'Delete'},()=>{},connect);assert.equal(refused.ok,false);
 assert.equal(await page.evaluate(()=>window.sent||window.deleted),undefined);
});
