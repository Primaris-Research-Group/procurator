import test from 'node:test';
import assert from 'node:assert/strict';
import {registerWeb} from './register.mjs';
const context={channel:'telegram',isAuthorizedSender:true,senderId:'100000001'};
test('browser tools never expose execution or takeover',async()=>{
 const tools=[],commands=[];registerWeb({registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)},async()=>({}));
 assert.equal(tools.length,20);assert(!tools.filter(t=>t.name!=='web_send_takeover_link').some(t=>/approve|execute|takeover|evaluate|connect/.test(t.name)));assert(tools.some(t=>t.name==='web_send_signin_link'));assert.equal(commands.length,18);assert(commands.some(c=>c.name==='webtrace')&&commands.some(c=>c.name==='webguard'));
});
test('only actual Telegram owner can invoke exact browser approval',async()=>{
 let calls=[];const commands=[];registerWeb({registerTool(){},registerCommand:c=>commands.push(c)},async(...args)=>{calls.push(args);return {id:'a'.repeat(16),status:'completed'}});
 const cmd=commands.find(c=>c.name==='webapprove');const args='a'.repeat(16)+' '+'b'.repeat(16);
 for(const bad of [{channel:'other'},{senderId:'123'},{isAuthorizedSender:false},{senderIsOwner:false}])await cmd.handler({...context,...bad,args});
 assert.equal(calls.length,0);await cmd.handler({...context,args:''});assert.equal(calls.length,0);
 await cmd.handler({...context,args});assert.deepEqual(calls[0],['get',{id:'a'.repeat(16)}]);assert.deepEqual(calls[1],['execute',{id:'a'.repeat(16),hash:'b'.repeat(16)},true]);
});
test('saved site logins: only the owner connects, confirms or deletes; the model can only list',async()=>{
 const calls=[],tools=[],commands=[];
 registerWeb({registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)},async(...args)=>{calls.push(args);return {site:'leaveco',origin:'https://www.leaveco.example',live_view_url:'https://live.example/x',sites:[]};});
 const connect=commands.find(c=>c.name==='siteconnect');
 await connect.handler({...context,senderId:'999',args:'leaveco https://www.leaveco.example/'});assert.equal(calls.length,0);
 assert.match((await connect.handler({...context,args:'LEAVECO!! nope'})).text,/PRIVATE sign-in link/);assert.equal(calls.length,0);
 const r=await connect.handler({...context,args:'leaveco https://www.leaveco.example/'});
 assert.deepEqual(calls.at(-1),['site_connect',{site:'leaveco',url:'https://www.leaveco.example/'},true]);assert.match(r.text,/sign in yourself/);assert.match(r.text,/\/siteready leaveco/);
 await commands.find(c=>c.name==='siteready').handler({...context,args:'leaveco'});assert.deepEqual(calls.at(-1),['site_ready',{site:'leaveco'},true]);
 const list=tools.find(t=>t.name==='web_site_logins');await list.execute('x',{});assert.deepEqual(calls.at(-1),['site_list',{}]);
 assert.ok(tools.find(t=>t.name==='web_browser_open').parameters.properties.site_login);
});

test('siteconnect accepts friendly input: any case, no https, or just an address',async()=>{
 const {parseSiteConnect}=await import('./register.mjs');
 assert.deepEqual(parseSiteConnect('LEAVECO www.leaveco.example'),{site:'leaveco',url:'https://www.leaveco.example'});
 assert.deepEqual(parseSiteConnect('leaveco https://services.leaveco.example/login?x=1'),{site:'leaveco',url:'https://services.leaveco.example/login?x=1'});
 assert.deepEqual(parseSiteConnect('https://portal.example.co'),{site:'example',url:'https://portal.example.co'});
 assert.deepEqual(parseSiteConnect('www.leaveco.example/claims'),{site:'leaveco',url:'https://www.leaveco.example/claims'});
 assert.deepEqual(parseSiteConnect('LEAVECO'),{site:'leaveco',url:undefined});
 for(const bad of ['', 'u', 'leaveco http://x.com', 'a b c', 'leaveco javascript:alert(1)'])assert.equal(parseSiteConnect(bad),null,bad);
});

test('siteconnect help has no tappable web address; siteready explains a still-signed-out browser',async()=>{
 const commands={},calls=[];
 registerWeb({registerTool(){},registerCommand:c=>commands[c.name]=c,on(){}},async(action,args)=>{calls.push([action,args]);
  if(action==='site_connect')return {site:'leaveco',origin:'https://www.leaveco.example',live_view_url:'https://www.browserbase.com/devtools-fullscreen/abc'};
  if(action==='site_ready')return {site:'leaveco',status:'still_signed_out',note:'The private browser still shows a sign-in page.'};});
 const help=(await commands.siteconnect.handler({...context,args:''})).text;
 assert.doesNotMatch(help,/https?:|www\.|\.com/);
 const r=(await commands.siteconnect.handler({...context,args:'leaveco'})).text;
 assert.deepEqual(calls[0],['site_connect',{site:'leaveco'}]);
 assert.match(r,/browserbase\.com\/devtools-fullscreen\/abc/);assert.match(r,/\/siteready leaveco/);
 const ready=(await commands.siteready.handler({...context,args:'leaveco'})).text;
 assert.match(ready,/still shows a sign-in page/);assert.doesNotMatch(ready,/is ready/);
});

test('/spendcap shows and sets the per-purchase cap for the owner only',async()=>{
 const commands={},calls=[];
 registerWeb({registerTool(){},registerCommand:c=>commands[c.name]=c,on(){}},async(action,args,privileged)=>{calls.push([action,args,privileged]);return {purchase_cap_usd:args.amount??500};});
 assert.match((await commands.spendcap.handler({...context,args:''})).text,/Per-purchase cap: \$500\.00/);
 assert.match((await commands.spendcap.handler({...context,args:'$1,200'})).text,/now \$1200\.00/);
 assert.deepEqual(calls[1],['purchase_cap',{amount:1200},true]);
 assert.match((await commands.spendcap.handler({...context,args:'lots'})).text,/Use \/spendcap AMOUNT/);
 assert.doesNotMatch((await commands.spendcap.handler({...context,senderId:'999',args:'5000'})).text,/now/);
 assert.equal(calls.length,2);
});

test('siteconnect leads with the phone sign-in page when available',async()=>{
 const commands={};
 registerWeb({registerTool(){},registerCommand:c=>commands[c.name]=c,on(){}},async()=>({site:'leaveco',origin:'https://www.leaveco.example',live_view_url:'https://www.browserbase.com/devtools-fullscreen/abc',signin_url:'https://assistant.example.com/assistant/signin/'+'T'.repeat(43)}));
 const r=(await commands.siteconnect.handler({...context,args:'leaveco'})).text;
 assert.ok(r.indexOf('assistant.example.com/assistant/signin/')<r.indexOf('browserbase.com'));
 assert.match(r,/works on your phone/);assert.match(r,/I'm signed in/);
});

test('web_site_signup is registered, declared and calls the site_signup broker action (Nov 6 Publix)',async()=>{
 const {readFileSync}=await import('node:fs');
 const tools=[],calls=[];
 registerWeb({registerTool:t=>tools.push(t),registerCommand:()=>{},on:()=>{}},async(a,args)=>{calls.push([a,args]);return {status:'signing_up'};});
 const t=tools.find(x=>x.name==='web_site_signup');
 assert.ok(t);assert.match(t.description,/never click the final Create account button/);
 await t.execute('1',{site:'publix'});assert.deepEqual(calls.at(-1),['site_signup',{site:'publix'}]);
 assert.match(tools.find(x=>x.name==='web_send_signin_link').description,/NO account on the site yet, use web_site_signup/);
 const declared=JSON.parse(readFileSync(new URL('./openclaw.plugin.json',import.meta.url))).contracts.tools;
 for(const x of tools.filter(t=>t.name))assert.ok(declared.includes(x.name),x.name+' must be declared in openclaw.plugin.json');
});
test('/webstats week shows the three numbers per site',async()=>{
 const {weekText}=await import('./register.mjs');
 const w={sites:[{site:'cvs.com',started:3,completed:2,completion_without_takeover:0.333,approvals_per_completed:1,cost_per_completed_usd:0.45}],total:{started:3,completed:2,completion_without_takeover:0.333,approvals_per_completed:1,cost_per_completed_usd:0.45},notes:'n'};
 const t=weekText(w);assert.match(t,/cvs\.com: 3 started, 2 done → 33% · 1 · \$0\.45/);assert.match(t,/All: 3 started/);
 assert.equal(weekText({sites:[],total:{},notes:''}),'No browser tasks started in the last 7 days.');
 // 0.19.1 (QA T2-01): lanes and playbooks are always listed.
 assert.match(t,/Browsers: cloud 0, home PC 0 task\(s\)\./);assert.match(t,/Playbooks: 0 yet/);
 const t2=weekText({...w,sites:[{...w.sites[0],lanes:{cloud:1,home:2}}],total:{...w.total,playbooks:[{site:'leaveco.example',steps:5,runs:3,hit_rate:null,demoted:false}]}});
 assert.match(t2,/Browsers: cloud 1, home PC 2 task\(s\) \(cvs\.com 2\)/);assert.match(t2,/Playbooks: 1 — leaveco\.example \(5 steps, 3 runs\)/);
});

test('Nov 6: /webtakeover answers with the private phone page, never the raw live view',async()=>{
 const calls=[],commands=[];
 registerWeb({registerTool(){},registerCommand:c=>commands.push(c)},async(a,x,p)=>{calls.push([a,x,p]);
  if(a==='site_takeover_page')return {id:'6103ac0bd0604037',site:'leaveco.example',page_url:'https://assistant.example.com/assistant/signin/'+'t'.repeat(43),minutes:44};
  return {id:'6103ac0bd0604037',live_view_url:'https://www.browserbase.com/devtools-fullscreen/inspector.html?x'};});
 const out=await commands.find(c=>c.name==='webtakeover').handler({...context,args:'6103ac0bd0604037'});
 assert.match(out.text,/assistant\.example\.com\/assistant\/signin\//);assert.doesNotMatch(out.text,/browserbase\.com/);assert.match(out.text,/password manager/);
 assert.deepEqual(calls.map(c=>[c[0],c[2]]),[['takeover',true],['site_takeover_page',true]]);
});
