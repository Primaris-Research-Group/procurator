import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerSitePolicy,SITE_RULES_LINE} from './sites.mjs';
test('site rules: read-only tool for the assistant, owner-only changes',async()=>{
 const calls=[],tools=[],commands=[];
 const call=async(a,args,p=false)=>{calls.push([a,args,p]);if(a==='webpolicy')return args.domain?{domain:'amazon.com',mode:'read_only',meaning:'read only',reason:'Amazon blocks agents.'}:{defaults:{'amazon.com':'read_only'},owner_rules:{},fixed_never:['chase.com']};return {domain:'amazon.com',meaning:'normal rules'};};
 registerSitePolicy({registerTool:(f,o)=>tools.push([f,o]),registerCommand:c=>commands.push(c)},call,ctx=>{if(!ctx.owner)throw new Error('owner only');});
 assert.equal(tools[0][0]({agentId:'phone'}),undefined);
 await tools[0][0]({agentId:'main'}).execute('1',{domain:'amazon.com'});assert.deepEqual(calls[0],['webpolicy',{domain:'amazon.com'},false]);
 await tools[1][0]({agentId:'main'}).execute('2',{session_id:'abc',tool:'search_catalog',arguments_json:'{"query":"mug"}'});assert.deepEqual(calls[1],['page_tool',{id:'abc',tool:'search_catalog',arguments:{query:'mug'}},false]);
 const cmd=commands.find(c=>c.name==='sitepolicy');
 assert.match((await cmd.handler({owner:false,args:'amazon.com browser'})).text,/owner only/);
 assert.match((await cmd.handler({owner:true,args:''})).text,/Never browsed \(fixed\)/);
 assert.match((await cmd.handler({owner:true,args:'amazon.com'})).text,/amazon.com: read only/);
 await cmd.handler({owner:true,args:'amazon.com browser'});assert.deepEqual(calls.at(-1),['webpolicy_set',{domain:'amazon.com',mode:'browser'},true]);
 assert.ok(!/`/.test(SITE_RULES_LINE));
});
import {registerSigninWake,SIGNIN_WAKE_TEXT,TAKEOVER_WAKE_TEXT,HOME_READY_WAKE_TEXT} from './sites.mjs';
test('a finished owner sign-in wakes the assistant once, for that browser only',async()=>{
 const woken=[];let queue=[{site:'leaveco',browser:'0123456789abcdef',at:1},{site:'bad name',browser:'x'}];
 const call=async a=>{assert.equal(a,'site_signin_events');const out={events:queue};queue=[];return out;};
 const check=registerSigninWake(call,async(t,k,r)=>{woken.push([t,k,r]);return 'accepted';},{setTimer:()=>null});
 assert.deepEqual(await check(),['accepted']);assert.deepEqual(await check(),[]);
 assert.equal(woken.length,1);assert.match(woken[0][0],/browser 0123456789abcdef/);assert.match(woken[0][0],/do not open a new browser/);
 assert.equal(woken[0][1],'procurator-signin:0123456789abcdef:1');
 assert.match(SIGNIN_WAKE_TEXT({site:'leaveco',browser:'b'}),/not a message the owner typed/);
 assert.deepEqual(await registerSigninWake(async()=>{throw new Error('down');},async()=>'x',{setTimer:()=>null})(),[]);
});
test('/siteauto: owner-only standing permission per saved login',async()=>{
 const calls=[],commands=[];
 const call=async(a,args,p=false)=>{calls.push([a,args,p]);return args.site===undefined?{task_autostart:['leaveco'],saved_logins:['leaveco','hotels']}:{task_autostart:args.on?['leaveco']:[]};};
 registerSitePolicy({registerTool:()=>{},registerCommand:c=>commands.push(c)},call,ctx=>{if(!ctx.owner)throw new Error('owner only');});
 const cmd=commands.find(c=>c.name==='siteauto');
 assert.match((await cmd.handler({owner:false,args:'leaveco on'})).text,/owner only/);assert.equal(calls.length,0);
 assert.match((await cmd.handler({owner:true,args:''})).text,/leaveco[\s\S]*Saved logins: leaveco, hotels/);
 assert.match((await cmd.handler({owner:true,args:'leaveco maybe'})).text,/on or/);
 assert.match((await cmd.handler({owner:true,args:'Leaveco ON'})).text,/final submit/);assert.deepEqual(calls.at(-1),['autostart_logins',{site:'leaveco',on:true},true]);
 assert.match((await cmd.handler({owner:true,args:'leaveco off'})).text,/need \/webtask again/);assert.deepEqual(calls.at(-1),['autostart_logins',{site:'leaveco',on:false},true]);
 assert.ok(!commands.some(c=>/`/.test(c.description)));
});

test('Nov 6: "I am done" on a private takeover page wakes the assistant in the same browser',async()=>{
 const woken=[];
 const check=registerSigninWake(async()=>({events:[{site:'leaveco.example',browser:'6103ac0bd0604037',at:5,kind:'takeover'}]}),async(t,k,r)=>{woken.push([t,k,r]);return 'started';},{setTimer:()=>null});
 assert.deepEqual(await check(),['started']);
 assert.match(woken[0][0],/^TAKEOVER FINISHED/);assert.match(woken[0][0],/browser 6103ac0bd0604037/);assert.match(woken[0][0],/Never ask for passwords/);
 assert.equal(woken[0][1],'procurator-takeover:6103ac0bd0604037:5');assert.equal(woken[0][2],'procurator-takeover-finished');
 assert.equal(TAKEOVER_WAKE_TEXT({site:'x.com',browser:'b'}).includes('web_send_takeover_link'),true);
});


test('0.20.3: /homeready after a sign-in the assistant asked for wakes it to continue on the PC',async()=>{
 const woken=[];
 const check=registerSigninWake(async()=>({events:[{site:'hotels',browser:'',at:7,kind:'home_ready'},{site:'bad site',browser:'',kind:'home_ready'}]}),async(t,k,r)=>{woken.push([t,k,r]);return 'started';},{setTimer:()=>null});
 assert.deepEqual(await check(),['started']);
 assert.match(woken[0][0],/^HOME SIGN-IN FINISHED/);assert.match(woken[0][0],/site_login=hotels browser=home/);assert.match(woken[0][0],/never ask for passwords/);
 assert.deepEqual(woken[0].slice(1),['procurator-homeready:hotels:7','procurator-home-signin-finished']);
 assert.equal(HOME_READY_WAKE_TEXT({site:'x'}).includes('not a message the owner typed'),true);
});
