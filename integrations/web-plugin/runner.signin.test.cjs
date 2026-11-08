const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {run}=require('./runner.cjs');
// Nov 4: the owner taps a field in the live view on their phone; the server types what the owner entered on the sign-in page.
test('owner_keys types into the focused field, supports Tab and clear, and returns no text',async t=>{
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();
 await page.setContent('<form><label>User ID <input id="u"></label><label>Password <input id="p" type="password"></label></form>');
 const keys=input=>run({connectUrl:'unused',operation:'owner_keys',...input},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));
 await page.focus('#u');
 let r=await keys({text:'owner.p'});assert.deepEqual(r,{ok:true});
 r=await keys({key:'Tab'});assert.deepEqual(r,{ok:true});
 await keys({text:'s3cr3t!'});
 assert.equal(await page.inputValue('#u'),'owner.p');assert.equal(await page.inputValue('#p'),'s3cr3t!');
 await keys({clear:true});assert.equal(await page.inputValue('#p'),'');
 r=await keys({key:'F12'});assert.equal(r.ok,false);
});

const signIn=(browser,input)=>run({connectUrl:'unused',...input},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));
async function fresh(t,html){const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();await page.setContent(html);return {browser,page};}

test('owner_login fills user ID and password on one page and submits; nothing is returned',async t=>{
 const {browser,page}=await fresh(t,`<a href="#">Help</a><input placeholder="Search"><form id="f"><label>User ID <input id="u" name="userId"></label><label>Password <input id="p" type="password"></label><button>Log In</button></form>
  <script>document.getElementById('f').addEventListener('submit',e=>{e.preventDefault();window.sent=[document.getElementById('u').value,document.getElementById('p').value];});</script>`);
 const r=await signIn(browser,{operation:'owner_login',username:'owner.p',password:'s3cr3t!'});
 assert.deepEqual(r,{ok:true,submitted:true,steps:'one_page'});
 assert.deepEqual(await page.evaluate(()=>window.sent),['owner.p','s3cr3t!']);
 assert.equal(await page.evaluate(()=>document.querySelectorAll('[data-procurator-login]').length),0);
 assert.equal(await page.inputValue('input[placeholder=Search]'),'');
});
test('owner_login handles two-step sign-in (user ID, Next, then password)',async t=>{
 const {browser,page}=await fresh(t,`<form id="a"><input id="e" type="email" autocomplete="username"><button>Next</button></form><div id="b"></div>
  <script>document.getElementById('a').addEventListener('submit',e=>{e.preventDefault();window.user=document.getElementById('e').value;
   setTimeout(()=>{document.getElementById('a').remove();document.getElementById('b').innerHTML='<form id=c><input id=p type=password><button>Sign in</button></form>';
    document.getElementById('c').addEventListener('submit',ev=>{ev.preventDefault();window.pw=document.getElementById('p').value;});},700);});</script>`);
 const r=await signIn(browser,{operation:'owner_login',username:'a@b.com',password:'pw-123'});
 assert.deepEqual(r,{ok:true,submitted:true,steps:'two_step'});
 assert.equal(await page.evaluate(()=>window.user),'a@b.com');assert.equal(await page.evaluate(()=>window.pw),'pw-123');
});
test('owner_code finds the verification box and submits; no form reports a reason',async t=>{
 const {browser,page}=await fresh(t,`<form id="f"><label>Enter the 6-digit verification code <input id="c" inputmode="numeric"></label><label><input type="checkbox"> Remember this device</label><button>Verify</button></form>
  <script>document.getElementById('f').addEventListener('submit',e=>{e.preventDefault();window.code=document.getElementById('c').value;});</script>`);
 assert.deepEqual(await signIn(browser,{operation:'owner_code',code:'482913'}),{ok:true,submitted:true});
 assert.equal(await page.evaluate(()=>window.code),'482913');
 await page.setContent('<p>Welcome back</p>');
 assert.deepEqual(await signIn(browser,{operation:'owner_login',username:'x',password:'y'}),{ok:false,reason:'no_login_form'});
 assert.deepEqual(await signIn(browser,{operation:'owner_code',code:'1'}),{ok:false,reason:'no_code_box'});
});

// Nov 5: phones get a picture of the private browser; taps and scrolls are relayed (no embedded live view).
test('owner picture, tap and scroll work on the page the owner sees and return no page text',async t=>{
 const {browser,page}=await fresh(t,`<div style="height:3000px"><button id="b" style="position:absolute;left:0;top:0;width:200px;height:100px" onclick="window.hit=(window.hit||0)+1">Log in</button></div>`);
 await page.setViewportSize({width:800,height:600});
 const shot=await signIn(browser,{operation:'owner_shot'});
 assert.equal(shot.ok,true);assert.ok(Buffer.from(shot.image_b64,'base64').subarray(0,2).equals(Buffer.from([0xff,0xd8])));
 assert.deepEqual(Object.keys(shot).sort(),['host','image_b64','ok']);
 assert.deepEqual(await signIn(browser,{operation:'owner_tap',x:0.1,y:0.08}),{ok:true});assert.equal(await page.evaluate(()=>window.hit),1);
 assert.equal((await signIn(browser,{operation:'owner_tap',x:5,y:0})).ok,false);
 assert.deepEqual(await signIn(browser,{operation:'owner_scroll',direction:'down'}),{ok:true});
 assert.ok(await page.evaluate(()=>window.scrollY)>100);
});
