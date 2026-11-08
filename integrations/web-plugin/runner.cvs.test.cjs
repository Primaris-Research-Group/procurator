const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot,digest,run}=require('./runner.cjs');

// Modeled on the Nov 4 CVS scheduler: a live-region result count, several store cards each
// offering "2:30 PM", and time-slot radios whose real <input> is hidden behind a styled label.
const store=(name,addr,id)=>`<section class="store"><h3>${name}</h3><p>${addr}</p><div class="slots">
 <input type="radio" name="slot" id="${id}a" value="02:30 PM" style="position:absolute;opacity:0;width:0;height:0;pointer-events:none"><label for="${id}a" class="pill">2:30 PM</label>
 <input type="radio" name="slot" id="${id}b" value="02:45 PM" style="position:absolute;opacity:0;width:0;height:0;pointer-events:none"><label for="${id}b" class="pill">2:45 PM</label></div></section>`;
const CVS=`<div role="alert">5 of 18 location(s) near 31901, GA</div>${store('CVS Pharmacy Macon Rd','3127 Macon Rd, Columbus, GA · 2.2 mi','m')}${store('CVS Pharmacy Wynnton Rd','2401 Wynnton Rd, Columbus, GA · 3.1 mi','w')}<button>Continue</button>`;

async function open(t,html){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();await page.setContent(html);return {browser,page};
}
const act=(browser,before,steps)=>run({connectUrl:'unused',practice:true,operation:'act',expected:digest(before),steps},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));

test('result-count announcements are notices, real failures stay errors',async t=>{
 const {page}=await open(t,CVS);let s=await snapshot(page);
 assert.deepEqual(s.page_errors,[]);assert.deepEqual(s.page_notices,['5 of 18 location(s) near 31901, GA']);
 await page.setContent('<div role="alert">Please select a time to continue</div><div aria-live="assertive">Something went wrong. Try again.</div><button>Continue</button>');
 s=await snapshot(page);assert.equal(s.page_errors.length,2);assert.deepEqual(s.page_notices,[]);
});
test('duplicate time slots carry their own store as context',async t=>{
 const {page}=await open(t,CVS);const s=await snapshot(page);
 const slots=s.controls.filter(c=>c.type==='radio'&&c.value==='02:30 PM');
 assert.equal(slots.length,2);
 assert.match(slots[0].near,/Macon Rd/);assert.doesNotMatch(slots[0].near,/Wynnton/);
 assert.match(slots[1].near,/Wynnton Rd/);assert.doesNotMatch(slots[1].near,/Macon/);
});
test('a hidden radio is selected through its label, only the chosen store slot',async t=>{
 const {browser,page}=await open(t,CVS);const before=await snapshot(page);
 const macon=before.controls.find(c=>c.type==='radio'&&c.value==='02:30 PM'&&/Macon/.test(c.near));
 const r=await act(browser,before,[{kind:'click',control:macon}]);
 assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(await page.locator('#ma').isChecked(),true);assert.equal(await page.locator('#wa').isChecked(),false);
});
test('a click that cannot happen reports not_dispatched with a reason, and nothing changes',async t=>{
 const {browser,page}=await open(t,'<input type="radio" name="x" id="r" style="display:none"><span>2:30 PM</span><button>Go</button>');
 const before=await snapshot(page);
 const ghost={...before.controls.find(c=>c.label==='Go'),selector:'input#r',type:'radio',tag:'input',label:'Go'};
 const r=await act(browser,before,[{kind:'click',control:ghost}]);
 assert.equal(r.ok,false);assert.equal(r.kind,'not_dispatched');assert.ok(['page_changed','control_not_clickable'].includes(r.reason),r.reason);
 assert.equal(await page.locator('#r').isChecked(),false);
});
