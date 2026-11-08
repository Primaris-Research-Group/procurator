const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot,digest,run}=require('./runner.cjs');

async function open(t,html){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();await page.setContent(html);return {browser,page};
}
const drive=(browser,input)=>run({connectUrl:'unused',practice:true,...input},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));

const FORM=`<form aria-label="Patient details"><fieldset><legend>Contact</legend>
 <label>Phone <input name="phone" type="tel" placeholder="(555) 555-5555" required></label>
 <label>Birth date <input name="dob" type="date"></label>
 <label>Email <input name="email" aria-invalid="true" aria-describedby="email-err"></label><span id="email-err">Enter a valid email address</span>
</fieldset><label>Pharmacy <input role="combobox" aria-expanded="false" name="store"></label>
<button type="button" id="more" onclick="document.querySelector('form').insertAdjacentHTML('beforeend','<button type=button>Added later</button>')">Show more</button>
<button type="button" id="noop">Does nothing</button></form>`;

test('snapshots carry stable ids, new-element markers, section and field hints',async t=>{
 const {page}=await open(t,FORM);
 const a=await snapshot(page);
 const phone=a.controls.find(c=>c.name==='phone'),dob=a.controls.find(c=>c.name==='dob'),email=a.controls.find(c=>c.name==='email'),store=a.controls.find(c=>c.name==='store');
 assert.equal(phone.placeholder,'(555) 555-5555');assert.equal(phone.required,true);assert.equal(phone.group,'Contact');
 assert.equal(dob.format,'YYYY-MM-DD');assert.equal(email.error,'Enter a valid email address');
 assert.equal(store.combobox,true);assert.equal(store.expanded,false);assert.equal(store.group,'Patient details');
 assert.deepEqual(a.new_refs,[]);assert.equal(typeof a.scroll.more_below,'boolean');
 await page.click('#more');
 const b=await snapshot(page);
 assert.equal(b.controls.find(c=>c.name==='phone').stable_id,phone.stable_id);
 const added=b.controls.find(c=>c.label==='Added later');assert.deepEqual(b.new_refs,[added.ref]);
 assert.equal(digest({...b,new_refs:[]}),digest(b));  // change markers never invalidate an approval
});

test('a fill swallowed by a keyboard-only mask is retyped and verified',async t=>{
 // The widget keeps its own model updated only from key presses and re-renders on input,
 // like masked React inputs; a bulk fill is reverted.
 const {browser,page}=await open(t,`<label>Member ID <input id="m"></label><script>
  const el=document.getElementById('m');let model='';
  el.addEventListener('keydown',e=>{if(e.key.length===1)model+=e.key;});
  el.addEventListener('input',()=>{queueMicrotask(()=>{el.value=model;});});</script>`);
 const before=await snapshot(page);const control=before.controls.find(c=>c.label==='Member ID');
 const r=await drive(browser,{operation:'act',expected:digest(before),steps:[{kind:'fill',control,value:'ABC123'}]});
 assert.equal(r.ok,true,JSON.stringify(r).slice(0,400));
 assert.equal(await page.inputValue('#m'),'ABC123');assert.equal(r.fills[0].ok,true);assert.equal(r.fills[0].method,'keyboard');
});

test('every action reports what changed, so no-op clicks are visible',async t=>{
 const {browser,page}=await open(t,FORM);
 let before=await snapshot(page);
 let r=await drive(browser,{operation:'act',expected:digest(before),steps:[{kind:'click',control:before.controls.find(c=>c.label==='Does nothing')}]});
 assert.equal(r.ok,true);assert.equal(r.progress.changed,false);
 before=await snapshot(page);
 r=await drive(browser,{operation:'act',expected:digest(before),steps:[{kind:'click',control:before.controls.find(c=>c.label==='Show more')}]});
 assert.equal(r.progress.changed,true);assert.equal(r.progress.new_controls,1);
 assert.deepEqual(r.progress.field_errors.map(e=>e.error),['Enter a valid email address']);
});

test('screenshot operation returns a JPEG of the current viewport',async t=>{
 const {browser}=await open(t,FORM);
 const r=await drive(browser,{operation:'screenshot'});
 assert.equal(r.ok,true);const img=Buffer.from(r.image_b64,'base64');
 assert.equal(img[0],0xff);assert.equal(img[1],0xd8);assert(img.length>500);
});
