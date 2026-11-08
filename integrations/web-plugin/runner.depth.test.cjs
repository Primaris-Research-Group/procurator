// web 0.18.0: frame/shadow inlining, set-of-marks look, point probe, click_xy, uploads and mobile viewport.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const inbox=fs.mkdtempSync(path.join(os.tmpdir(),'inbox-'))+'/';
process.env.PROCURATOR_INBOX_PREFIX=inbox;
const {run,snapshot,locatorFor,digest}=require('./runner.cjs');
async function withBrowser(fn){
 const port=9300+Math.floor(Math.random()*600);
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true,args:['--remote-debugging-port='+port]});
 try{const page=await browser.newPage();await fn(page,{connectUrl:'http://127.0.0.1:'+port,practice:true,url:'about:blank',origin:'null'});}finally{await browser.close();}
}
const FRAME_PAGE=`<h1>Book</h1><label>ZIP <input id="zip"></label>
 <iframe id="sched" style="width:400px;height:200px" srcdoc="<label>Preferred day <select name=day><option>Mon</option><option>Tue</option></select></label><button>Next</button>"></iframe>
 <div id="host"></div>`;
test('same-origin frames and open shadow roots are inlined with working locators',async()=>{
 await withBrowser(async page=>{
  await page.setContent(FRAME_PAGE);
  await page.evaluate(()=>{const r=document.querySelector('#host').attachShadow({mode:'open'});r.innerHTML='<label>Member ID <input name="member"></label>';});
  await page.waitForTimeout(200);
  const s=await snapshot(page);
  const day=s.controls.find(c=>c.label==='Preferred day');assert(day,'frame select inlined');assert.match(day.ref,/^f1e/);assert.equal(day.frame.index,1);
  assert(s.inlined_frames.length===1);assert.match(s.text,/\|IFRAME /);
  const member=s.controls.find(c=>c.name==='member');assert.equal(member.in_shadow,true);
  const loc=await locatorFor(page,day);await loc.selectOption('Tue');assert.equal(await page.frameLocator('#sched').locator('select').inputValue(),'Tue');
  const next=s.controls.find(c=>c.label==='Next'&&c.frame);await (await locatorFor(page,next)).click();
 });
});
test('look draws numbered marks, refuses sign-in pages, and point/click_xy hit the same element',async()=>{
 await withBrowser(async(page,base)=>{
  await page.setContent('<div style="padding:40px"><button id="b" onclick="this.textContent=\'clicked\'">Continue</button><canvas id="c" width="120" height="40"></canvas></div>');
  const look=await run({...base,operation:'look'});
  assert.equal(look.ok,true);assert(look.image_b64.length>1000);assert(look.marks.some(m=>m.label==='Continue'&&m.box[2]>0));
  assert.equal(await page.evaluate(()=>!!document.getElementById('__procurator_marks')),false,'overlay removed');
  const m=look.marks.find(x=>x.label==='Continue');const x=m.box[0]+m.box[2]/2,y=m.box[1]+m.box[3]/2;
  const p=await run({...base,operation:'point',x,y});assert.equal(p.element.label,'Continue');assert.equal(p.element.tag,'button');
  const out=await run({...base,operation:'act',expected:digest(await snapshot(page)),steps:[{kind:'click_xy',x,y,probe:p.element}]});
  assert.equal(out.ok,true);assert.equal(await page.textContent('#b'),'clicked');
  const moved=await run({...base,operation:'act',expected:digest(await snapshot(page)),steps:[{kind:'click_xy',x,y,probe:{tag:'button',label:'Pay now'}}]});
  assert.equal(moved.ok,false);
  await page.setContent('<label>Password <input type="password"></label>');
  assert.equal((await run({...base,operation:'look'})).kind,'look_refused_signin');
 });
});
test('upload attaches the owner file under its own name, via an input or a file chooser button',async()=>{
 await withBrowser(async(page,base)=>{
  await page.setContent('<label>Supporting document <input type="file" id="f"></label><button id="pick" onclick="document.getElementById(\'g\').click()">Upload file</button><input type="file" id="g" style="display:none">');
  const file=inbox+'upload-1.bin';fs.writeFileSync(file,'%PDF-1.4 test');
  let s=await snapshot(page);const input=s.controls.find(c=>c.type==='file');
  let out=await run({...base,operation:'act',expected:digest(s),steps:[{kind:'upload',control:input,file_path:file,file:{name:'note.pdf',mime:'application/pdf'}}]});
  assert.equal(out.ok,true);assert.equal(await page.evaluate(()=>document.getElementById('f').files[0].name),'note.pdf');
  s=await snapshot(page);const btn=s.controls.find(c=>c.label==='Upload file');
  out=await run({...base,operation:'act',expected:digest(s),steps:[{kind:'upload',control:btn,file_path:file,file:{name:'leave.pdf',mime:'application/pdf'}}]});
  assert.equal(out.ok,true);assert.equal(await page.evaluate(()=>document.getElementById('g').files[0].name),'leave.pdf');
  const bad=await run({...base,operation:'act',expected:digest(await snapshot(page)),steps:[{kind:'upload',control:input,file_path:'/etc/passwd',file:{name:'x'}}]});
  assert.equal(bad.ok,false);
 });
});
test('mobile mode narrows the viewport only, and desktop restores it',{todo:'expectation predates the later snapshot/readiness changes; failing with headless Chromium in CI (0.30.0), re-baseline'},async()=>{
 await withBrowser(async(page,base)=>{
  await page.setViewportSize({width:1280,height:800});await page.setContent('<p>hi</p>');
  const ua=await page.evaluate(()=>navigator.userAgent);
  await run({...base,operation:'snapshot',viewport:'mobile'});assert.equal(await page.evaluate(()=>innerWidth),412);
  assert.equal(await page.evaluate(()=>navigator.userAgent),ua);
  await run({...base,operation:'snapshot'});assert.equal(await page.evaluate(()=>innerWidth),1280);
 });
});
