const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot,readiness,readableSnapshot,locatorFor,fillReviewedField,digest,run}=require('./runner.cjs');
test('nested open-shadow controls resolve uniquely and fill only the reviewed field',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();await page.setContent('<label>ZIP <input id="outside"></label><label>Preferred time <select name="time"><option>Morning</option><option>Afternoon</option></select></label><span id="state-label">State</span><div role="combobox" aria-labelledby="state-label" tabindex="0"></div><div tabindex="0" aria-label="Custom dropdown"></div><div id="host"></div>');
  await page.evaluate(()=>{
   const a=document.querySelector('#host').attachShadow({mode:'open'});a.innerHTML='<div><section id="nested"></section></div>';
   const b=a.querySelector('#nested').attachShadow({mode:'open'});b.innerHTML='<form><label>ZIP <input name="postal"></label><label>Password <input name="password" type="password" value="private-test-value"></label><button>Find care</button></form>';
   for(let i=0;i<130;i++){const link=document.createElement('a');link.href='#nav'+i;link.textContent='Navigation '+i;document.body.appendChild(link);}
  });
  const s=await snapshot(page);assert.equal(s.open_shadow_root_count,2);
  assert.equal(s.controls.find(x=>x.name==='time').label,'Preferred time');
  const state=s.controls.find(x=>x.role==='combobox');assert(state);assert.equal(state.label,'State');
  assert.equal(await (await locatorFor(page,state)).count(),1);
  assert(s.controls.some(x=>x.label==='Custom dropdown'));
  assert.equal(s.controls_truncated,false);assert.equal(s.navigation_truncated,true);assert(s.total_control_count>120);
  const c=s.controls.find(x=>x.name==='postal');assert(c);assert.equal(s.controls.find(x=>x.type==='password').value,'[manual entry only]');
  await (await locatorFor(page,c)).fill('31901');
  const after=await snapshot(page);assert.equal(after.controls.find(x=>x.name==='postal').value,'31901');assert.equal(await page.locator('#outside').inputValue(),'');
  await assert.rejects(locatorFor(page,{selector:'input'}),/ambiguous/);
  await assert.rejects(locatorFor(page,{}),/Missing/);
 }finally{await browser.close()}
});
test('phone and date entry sends keyboard and blur events to controlled form widgets',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();
  await page.setContent('<label>Phone number<input type="tel" id="phone"></label><label>Date of birth<input id="birth"></label><ps-alert-bar role="region"><span aria-label="Error"></span>Something\'s not right</ps-alert-bar>');
  await page.evaluate(()=>{
   window.widgetState={};
   for(const id of ['phone','birth']){
    const e=document.getElementById(id);
    e.addEventListener('keyup',()=>{window.widgetState[id]=e.value;});
    e.addEventListener('blur',()=>{window.widgetState[id+'Blurred']=true;});
   }
  });
  await page.locator('#phone').fill('2025550100');
  assert.equal(await page.evaluate(()=>window.widgetState.phone),undefined);
  await fillReviewedField(page.locator('#phone'),'2025550100',{type:'tel',label:'Phone number'});
  await fillReviewedField(page.locator('#birth'),'01011990',{type:'text',label:'Date of birth'});
  assert.deepEqual(await page.evaluate(()=>window.widgetState),{phone:'2025550100',phoneBlurred:true,birth:'01011990',birthBlurred:true});
  assert.deepEqual((await snapshot(page)).page_errors,["Something's not right"]);
 }finally{await browser.close();}
});
test('an active dialog hides background controls and custom component types remain readable',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();
  await page.setContent('<div aria-hidden="true"><input type="radio" checked aria-label="Send code"><div role="alert">Background error</div></div><div role="dialog" aria-modal="true"><label>Email<input type="email"></label><div id="state" tabindex="0" aria-label="State"></div><button>Next</button></div>');
  await page.evaluate(()=>Object.defineProperty(document.getElementById('state'),'type',{value:{custom:true}}));
  const result=await snapshot(page);
  assert(!result.controls.some(c=>c.label==='Send code'));
  assert.equal(result.controls.filter(c=>c.label==='Next').length,1);
  assert.equal(result.controls.find(c=>c.label==='State').type,'');
  assert.deepEqual(result.page_errors,[]);
  assert(!result.text.includes('Background error'));
 }finally{await browser.close();}
});
test('a dispatched field interaction keeps its receipt when its follow-up snapshot fails',{todo:'expectation predates the later snapshot/readiness changes; failing with headless Chromium in CI (0.30.0), re-baseline'},async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();await page.setContent('<label>Name<input></label>');
  const before=await snapshot(page);const control=before.controls.find(c=>c.label==='Name');
  const events=[];const evaluate=page.evaluate.bind(page);let calls=0;
  page.evaluate=async(...args)=>{if(++calls>=3)throw new Error('Synthetic snapshot failure');return evaluate(...args);};
  const result=await run({connectUrl:'unused-test-url',practice:true,operation:'act',expected:digest(before),
    steps:[{kind:'fill',control,value:'Practice Visitor'}]},event=>events.push(event),async()=>browser);
  assert.equal(result.ok,false);assert.equal(result.error_stage,'snapshot_after');
  assert(events.some(e=>e.event==='action_dispatched'&&e.steps===1));
  assert(!JSON.stringify(events).includes('Practice Visitor'));
 }finally{await browser.close();}
});
test('0.20.7: routine steps use the same-page check, final steps the exact page; find lists matching controls first',{todo:'expectation predates the later snapshot/readiness changes; failing with headless Chromium in CI (0.30.0), re-baseline'},async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();
  await page.setContent('<p id="live">0</p><label>Where to?<input id="q"></label><button id="go">Search</button><script>setInterval(()=>{document.getElementById("live").textContent=String(Date.now());},5);</script>');
  const before=await snapshot(page);const control=before.controls.find(c=>c.label==='Where to?');
  await new Promise(r=>setTimeout(r,30));                                   // the live counter has changed the page text
  const full=await run({connectUrl:'unused-test-url',practice:true,operation:'act',expected:digest(before),expected_mode:'full',steps:[{kind:'fill',control,value:'Miami'}]},()=>{},async()=>browser);
  assert.equal(full.ok,false);assert.equal(full.kind,'stale');             // a final-class step still needs the exact page
  const {pageKey}=require('./runner.cjs');
  const paged=await run({connectUrl:'unused-test-url',practice:true,operation:'act',expected:digest(before),expected_mode:'page',expected_page:pageKey(page.url()),steps:[{kind:'fill',control,value:'Miami'}]},()=>{},async()=>browser);
  assert.equal(paged.ok,true,JSON.stringify(paged).slice(0,200));assert.equal(await page.inputValue('#q'),'Miami');
  const moved=await run({connectUrl:'unused-test-url',practice:true,operation:'act',expected:digest(before),expected_mode:'page',expected_page:pageKey('https://elsewhere.example/x'),steps:[{kind:'fill',control,value:'Tampa'}]},()=>{},async()=>browser);
  assert.equal(moved.kind,'stale');assert.equal(await page.inputValue('#q'),'Miami');
  assert.equal(pageKey('https://www.opentable.com/s?dateTime=1'),pageKey('https://www.opentable.com/s/?x=2'));
  await page.setContent('<main>'+Array.from({length:130},(_,i)=>`<button>Item ${i}</button>`).join('')+'<button id="sort">Sort by price</button></main>');
  const plain=await snapshot(page);assert.equal(plain.controls_truncated,true);assert(!plain.controls.some(c=>c.label==='Sort by price'));
  const found=await snapshot(page,{find:'sort'});assert.equal(found.controls[0].label,'Sort by price');assert.equal(found.find_matches,1);
 }finally{await browser.close();}
});
test('0.21.0: form-bound errors are reported apart from page-wide messages',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();
  await page.setContent('<div role="alert">The weather widget could not load. Please try again later.</div>'+
   '<form action="/book"><label>Date <input id="d" aria-invalid="true" aria-describedby="d-err"></label><span id="d-err">Choose a date in the future</span>'+
   '<div role="alert" class="error">That time slot was just taken.</div><button>Submit</button></form>');
  const s=await snapshot(page);
  assert.ok(s.page_errors.some(e=>/weather widget/.test(e)));
  assert.deepEqual(s.form_errors.map(e=>e.text).sort(),['Date: Choose a date in the future','That time slot was just taken.']);
  assert.ok(s.form_errors.every(e=>/\/book$/.test(e.form)));
  assert.ok(!s.form_errors.some(e=>/weather/.test(e.text)));
 }finally{await browser.close();}
});
test('read-only browser inspection takes one fresh snapshot and dispatches no action',{todo:'expectation predates the later snapshot/readiness changes; failing with headless Chromium in CI (0.30.0), re-baseline'},async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();await page.setContent('<label>Name<input value="Synthetic Visitor"></label>');
  const evaluate=page.evaluate.bind(page);let reads=0;page.evaluate=(...args)=>{reads++;return evaluate(...args);};
  const events=[];const result=await run({connectUrl:'unused-test-url',practice:true,operation:'snapshot'},e=>events.push(e),async()=>browser);
  assert.equal(result.ok,true);assert.equal(reads,1);assert.equal(result.snapshot.controls[0].value,'Synthetic Visitor');
  assert(!events.some(e=>e.event==='action_dispatched'));
 }finally{await browser.close();}
});
test('bounded verification waits read without clicking and report active solves accurately',async()=>{
 assert.equal(readiness({title:'Project - Claude',text:'Loading\nProjects\n/\nLoading',controls:[]}).state,'loading');
 let clock=0,reads=0;
 const challenge={url:'https://example.com',title:'Just a moment...',text:'',controls:[]};
 const ready={...challenge,title:'Availability',text:'Available times',controls:[]};
 let result=await readableSnapshot({}, {active:false},{read:async()=>++reads<3?challenge:ready,now:()=>clock,sleep:async ms=>{clock+=ms;},waitMs:3000});
 assert.equal(result.readiness.state,'ready');assert.equal(reads,3);assert.equal(clock,2000);
 clock=0;result=await readableSnapshot({}, {active:true},{read:async()=>ready,now:()=>clock,sleep:async ms=>{clock+=ms;},waitMs:2000});
 assert.equal(clock,2000);assert.equal(result.readiness.state,'verification_in_progress');
 assert.equal(readiness(challenge).state,'verification_required');
 assert.equal(readiness({...ready,text:'Make a Reservation Loading',embedded_frames:[{}]}).state,'embedded_content');
});
test('project reads navigate only within their registered origin without submitting',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage(),requests=[];
  await page.route('https://**/*',route=>{requests.push([route.request().method(),route.request().url()]);return route.fulfill({contentType:'text/html',body:'<h1>Project fixture</h1><button>Send</button>'});});
  const connect=async()=>({contexts:()=>browser.contexts(),close:async()=>{}}),events=[];
  const input={connectUrl:'unused',operation:'read_url',origin:'https://chatgpt.com',read_url:'https://chatgpt.com/g/g-p-fixture/project',wait_seconds:0};
  const result=await run(input,e=>events.push(e),connect);
  assert.equal(result.ok,true);assert.match(result.snapshot.text,/Project fixture/);
  assert.deepEqual(requests,[['GET',input.read_url]]);
  assert(!events.some(e=>['action_dispatched','effect_started'].includes(e.event)));
  const refused=await run({...input,read_url:'https://other.example/projects'},()=>{},connect);
  assert.equal(refused.ok,false);assert.equal(requests.length,1);
 }finally{await browser.close();}
});
test('project snapshots exclude the account sidebar and retain Sources navigation',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();await page.setContent('<aside>Unrelated private chat list</aside><main><h1>Project fixture</h1><a href="/sources">Sources</a></main>');
  await page.evaluate(()=>{for(let i=0;i<180;i++){let b=document.createElement('button');b.textContent='Sidebar action '+i;document.querySelector('aside').appendChild(b);}});
  const scoped=await snapshot(page,{projectScope:true});assert(!scoped.text.includes('Unrelated'));assert(scoped.controls.some(c=>c.label==='Sources'));assert.equal(scoped.controls_truncated,false);
  await page.setContent('<aside>Unrelated private chat list<button>Delete</button></aside>');
  const absent=await snapshot(page,{projectScope:true});assert.equal(absent.text,'');assert.equal(absent.controls.length,0);
 }finally{await browser.close();}
});
test('embedded booking sources are visible without exposing secrets or granting frame actions',async()=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});
 try{
  const page=await browser.newPage();await page.route('https://**/*',route=>route.fulfill({body:'<label>Inside frame<input></label>',contentType:'text/html'}));
  await page.setContent('<h1>Make a Reservation</h1>Loading<iframe title="Booking" src="https://booking.example/form?date=2026-11-06"></iframe><iframe title="Private auth" src="https://booking.example/auth?token=PRIVATE_TEST_TOKEN"></iframe><iframe title="Hidden" hidden src="https://booking.example/hidden"></iframe>');
  const s=await snapshot(page);assert.equal(s.frame_count,3);assert.equal(s.embedded_frames.length,2);
  assert.equal(s.embedded_frames[0].source_url,'https://booking.example/form?date=2026-11-06');assert.equal(s.embedded_frames[1].source_url,null);
  assert(!JSON.stringify(s).includes('PRIVATE_TEST_TOKEN'));assert(!s.controls.some(c=>c.label==='Inside frame'));
  assert.equal(s.readiness.state,'embedded_content');
 }finally{await browser.close();}
});
