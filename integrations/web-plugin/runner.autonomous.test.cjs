const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot,digest,run}=require('./runner.cjs');
const launch=()=>chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME||undefined,headless:true});

test('a button covered by an overlay is reported as not clicked, with no effect announced',async()=>{
 const browser=await launch();
 try{
  const page=await browser.newPage();
  await page.setContent('<button id="add" onclick="window.clicked=true">Add to Order</button><div style="position:fixed;inset:0;background:rgba(0,0,0,.4)">Cookie notice overlay</div>');
  const before=await snapshot(page);const control=before.controls.find(c=>c.label==='Add to Order');
  const events=[];
  const started=Date.now();
  const result=await run({connectUrl:'unused',practice:true,operation:'act',expected:digest(before),steps:[{kind:'click',control}]},e=>events.push(e),async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));
  assert.equal(result.ok,false);assert.equal(result.kind,'not_dispatched');
  assert(events.some(e=>e.event==='protocol'&&e.version===2));
  assert(!events.some(e=>e.event==='effect_started'));
  assert.equal(await page.evaluate(()=>window.clicked),undefined);
  assert(Date.now()-started<40000);
 }finally{await browser.close();}
});
test('a clickable button announces its effect before clicking and completes',async()=>{
 const browser=await launch();
 try{
  const page=await browser.newPage();
  await page.setContent('<button onclick="document.body.insertAdjacentHTML(\'beforeend\',\'<p>Added</p>\')">Add to Order</button>');
  const before=await snapshot(page);const control=before.controls.find(c=>c.label==='Add to Order');
  const events=[];
  const result=await run({connectUrl:'unused',practice:true,operation:'act',expected:digest(before),steps:[{kind:'click',control}]},e=>events.push(e),async()=>browser);
  assert.equal(result.ok,true);assert(result.snapshot.text.includes('Added'));
  const order=events.map(e=>e.event);
  assert(order.indexOf('effect_started')>-1&&order.indexOf('effect_started')<order.indexOf('action_dispatched'));
 }finally{await browser.close();}
});
test('repeated buttons carry nearby item text; unique buttons do not',async()=>{
 const browser=await launch();
 try{
  const page=await browser.newPage();
  await page.setContent('<div class="card"><h3>Pepperoni Pizza</h3><p>$15.99</p><button>Add to Order</button></div><div class="card"><h3>Cheese Pizza</h3><p>$13.99</p><button>Add to Order</button></div><button>Checkout</button><label>ZIP<input></label><label>ZIP<input></label>');
  const s=await snapshot(page);
  const adds=s.controls.filter(c=>c.label==='Add to Order');
  assert.equal(adds.length,2);
  assert(adds[0].near.includes('Pepperoni'));assert(adds[1].near.includes('Cheese'));
  assert.equal(s.controls.find(c=>c.label==='Checkout').near,undefined);
  assert(s.controls.filter(c=>c.label==='ZIP').every(c=>c.near===undefined));
 }finally{await browser.close();}
});
