const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {run}=require('./runner.cjs');
// Sources tab with one Preview button whose viewer content is supplied per test.
const page=viewer=>`<main><button role="tab" aria-selected="true">Sources</button><div role="tabpanel" aria-label="Sources">
<button onclick='document.body.insertAdjacentHTML("beforeend",${JSON.stringify(viewer)});window.dispatchEvent(new Event("viewer"))'>Preview Doc.pdf</button>
<button onclick="window.deleted=true">Delete Doc.pdf</button></div><form onsubmit="window.sent=true;return false"><button>Send</button></form></main>`;
async function setup(t,viewer,script=''){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const p=await browser.newPage();await p.route('https://chatgpt.com/**',r=>r.fulfill({contentType:'text/html',body:page(viewer)+script}));
 const input={connectUrl:'unused',origin:'https://chatgpt.com',read_url:'https://chatgpt.com/g/g-p-fixture/project',project_scope:true,wait_seconds:0,operation:'project_file',file_name:'Doc.pdf'};
 return {p,go:extra=>run({...input,...extra},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}))};
}
test('PDF text layer that renders late inside the viewer dialog is read once stable',async t=>{
 const viewer='<div role="dialog"><header data-testid="viewer-header">Doc.pdf</header><div><div class="pdf"><canvas></canvas><div class="textLayer" aria-hidden="true" id="tl"></div></div></div></div>';
 const script='<script>addEventListener("viewer",()=>setTimeout(()=>{document.getElementById("tl").innerHTML="<span>Notice of noncompliance.</span><br><span>Night float schedule, page 1.</span>"},1500))</script>';
 const {p,go}=await setup(t,viewer,script);const r=await go();
 assert.equal(r.ok,true,JSON.stringify(r));assert.match(r.file.content,/Notice of noncompliance\./);assert.match(r.file.content,/Night float schedule/);assert(!/Doc\.pdf/.test(r.file.content));
 assert.equal(await p.evaluate(()=>window.sent||window.deleted),undefined);
});
test('document rendered inside an embedded frame is read from the frame',async t=>{
 const viewer='<div role="dialog"><header data-testid="viewer-header">Doc.pdf</header><div><iframe srcdoc="<p>Revised response and change log, Aug 28.</p>"></iframe></div></div>';
 const {go}=await setup(t,viewer);const r=await go();
 assert.equal(r.ok,true,JSON.stringify(r));assert.match(r.file.content,/Revised response and change log/);
});
test('image-only (scanned) preview reports image_only with a structure probe and no content',async t=>{
 const viewer='<div role="dialog"><header data-testid="viewer-header">Doc.pdf</header><div><img alt="" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></div></div>';
 const {go}=await setup(t,viewer);const r=await go({wait_seconds:0});
 assert.equal(r.ok,false);assert.equal(r.kind,'file_preview_not_readable');assert.equal(r.image_only,true);assert.equal(r.viewer_opened,true);assert.equal(r.dom_probe.tags.img,1);
});
test('Preview button covered by an overlay still opens via keyboard fallback, without other clicks',async t=>{
 const viewer='<div role="dialog"><header data-testid="viewer-header">Doc.pdf</header><div><p>Revised response and change log text.</p></div></div>';
 const overlay='<div style="position:fixed;inset:0;z-index:9" onclick="window.overlayClicked=true">hover card</div>';
 const {p,go}=await setup(t,viewer,overlay);const r=await go();
 assert.equal(r.ok,true,JSON.stringify(r));assert.match(r.file.content,/Revised response and change log text\./);
 assert.equal(await p.evaluate(()=>window.sent||window.deleted||window.overlayClicked),undefined);
});
test('docx preview panel outside the header container is read (observed live: docx-preview-panel)',async t=>{
 const viewer='<aside><div><header data-testid="viewer-header">Aug28LittlerReply.docx</header></div><div><button data-testid="docx-preview-zoom-trigger">zoom</button><div data-testid="docx-preview-panel"><section class="docx"><p>Dear Counsel,</p><p>Revised response and change log.</p></section></div></div></aside>';
 const {go}=await setup(t,viewer);const r=await go();
 assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.file.content,'Dear Counsel,\n\nRevised response and change log.');
});
test('image-only docx panel tries zoom once and reports panel structure and viewer buttons',async t=>{
 const viewer='<aside><div><header data-testid="viewer-header">X.docx</header><button aria-label="Download">d</button></div><div><button data-testid="docx-preview-zoom-trigger" onclick="window.zoomed=true">zoom</button><div data-testid="docx-preview-panel"><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></div></div></aside>';
 const {p,go}=await setup(t,viewer);const r=await go();
 assert.equal(r.ok,false);assert.equal(r.zoom_tried,true);assert.equal(await p.evaluate(()=>window.zoomed),true);
 assert.equal(r.viewer.panel_found,true);assert.equal(r.viewer.panel_tags.img,1);assert(r.viewer.viewer_buttons.includes('Download'));
});
test('canvas-only PDF viewer returns the original bytes the viewer loaded (observed live: 2 canvases, no text)',async t=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const p=await browser.newPage();
 const pdf=Buffer.from('%PDF-1.4\n% fixture bytes\n','latin1');
 const viewer='<aside><div><header data-testid="viewer-header">Doc.pdf</header><button aria-label="Download">d</button></div><div data-testid="pdf-preview-panel"><canvas></canvas><canvas></canvas></div></aside>';
 const html=`<main><button role="tab" aria-selected="true">Sources</button><div role="tabpanel" aria-label="Sources"><button onclick='fetch("https://files.example/doc.pdf").then(()=>document.body.insertAdjacentHTML("beforeend",${JSON.stringify(viewer)}))'>Preview Doc.pdf</button></div></main>`;
 await p.route('https://chatgpt.com/**',r=>r.fulfill({contentType:'text/html',body:html}));
 await p.route('https://files.example/**',r=>r.fulfill({contentType:'application/pdf',body:pdf,headers:{'access-control-allow-origin':'*'}}));
 const r=await run({connectUrl:'unused',origin:'https://chatgpt.com',read_url:'https://chatgpt.com/g/g-p-fixture/project',project_scope:true,wait_seconds:0,operation:'project_file',file_name:'Doc.pdf'},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));
 assert.equal(r.ok,true,JSON.stringify(r).slice(0,300));assert.equal(r.original.kind,'pdf');assert.equal(Buffer.from(r.original.bytes_b64,'base64').toString('latin1'),pdf.toString('latin1'));
});
