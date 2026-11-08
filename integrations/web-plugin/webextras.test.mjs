import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {stageFile,speedRunner,registerWebExtras,WORKER_TEXT} from './webextras.mjs';
import {continueTask,SPEED} from './tasks.mjs';

test('files are staged only from their Telegram media or the workspace, up to 20 MB',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'media-'))+'/',inbox=fs.mkdtempSync(path.join(os.tmpdir(),'inbox-'));
 const f=path.join(root,'note.pdf');fs.writeFileSync(f,'%PDF');
 const st=stageFile(f,{roots:[root],inbox});assert.match(st.inbox_name,/^upload-[a-f0-9]{32}\.bin$/);assert.equal(st.original,'note.pdf');
 assert.equal(fs.readFileSync(path.join(inbox,st.inbox_name),'utf8'),'%PDF');
 assert.throws(()=>stageFile('/etc/passwd',{roots:[root],inbox}),/Only files the owner sent/);
 assert.throws(()=>stageFile('relative.pdf',{roots:[root],inbox}),/full path/);
 const outside=path.join(fs.mkdtempSync(path.join(os.tmpdir(),'outside-')),'secret.pdf');fs.writeFileSync(outside,'x');
 const link=path.join(root,'link.pdf');fs.symlinkSync(outside,link);
 assert.throws(()=>stageFile(link,{roots:[root],inbox}),/Only files the owner sent/);
});
test('the worker runs on the cheap model in the task session, then wakes their conversation with its report',async()=>{
 const calls=[],woke=[];let resolveRun;
 const gateway=(method,params)=>{calls.push([method,params]);if(method!=='agent')return Promise.resolve({deleted:true});return new Promise(r=>{resolveRun=r;});};
 const call=async(a,b,p)=>{calls.push([a,b,p]);return {};};
 const runner=speedRunner(gateway,call);
 const saves=[];const r=await runner({id:'0123456789abcdef',lane:{cheap_model:'haiku'},event_id:'activate:x',save:async s=>saves.push(s),wake:async t=>woke.push(t)});
 assert.deepEqual(r,{queued:true,status:'accepted',lane:'worker:claude-haiku-5-5'});
 await new Promise(r=>setImmediate(r));
 const agent=calls.find(c=>c[0]==='agent')[1];
 assert.equal(agent.sessionKey,'agent:main:webtask-0123456789abcdef');assert.equal(agent.provider,undefined);assert.equal(agent.model,undefined);assert.deepEqual(calls.find(x=>x[0]==='sessions.patch')?.[1],{key:agent.sessionKey,model:'anthropic/claude-haiku-5-5'});assert.equal(agent.deliver,false);
 assert.deepEqual(calls.find(c=>c[0]==='task_delegate')[1],{id:'0123456789abcdef',worker_key:'agent:main:webtask-0123456789abcdef',model:'anthropic/claude-haiku-5-5'});
 resolveRun({status:'ok',result:{payloads:[{text:'Filled 6 fields; stopped at Submit (action_id abc).'}]}});
 await new Promise(r=>setTimeout(r,10));
 assert.match(woke[0],/Worker report \(unverified\):\nFilled 6 fields/);assert.match(woke[0],/You are the main model: validate/);
 assert.deepEqual(saves,['accepted','completed']);
 assert.deepEqual(calls.find(c=>c[0]==='sessions.delete')?.[1],{key:'agent:main:webtask-0123456789abcdef'});   // 0.20.7: worker session retired
});
test('a failed worker escalates the task back to their conversation',async()=>{
 const calls=[],woke=[];
 const runner=speedRunner(()=>Promise.reject(new Error('model not found')),async(a,b)=>{calls.push([a,b]);return {};});
 await runner({id:'0123456789abcdef',lane:{cheap_model:'terra'},event_id:'e',save:async()=>{},wake:async t=>woke.push(t)});
 await new Promise(r=>setTimeout(r,10));
 assert.equal(calls.find(c=>c[0]==='task_escalate')[1].id,'0123456789abcdef');assert.match(woke[0],/finish this task here/);
});
test('continuations use the worker only for routine work; final approvals stay in their conversation',async()=>{
 const used=[];SPEED.runner=async x=>{used.push(x.event_id);return {queued:true,status:'accepted'};};
 const call=async a=>a==='task_resume_claim'?{claimed:true}:a==='task_lane'?{worker:true,cheap_model:'haiku'}:{};
 const captured={context:{agentId:'main',sessionKey:'agent:main:telegram:direct:1',sessionId:'s'},legacy:true};
 const api={runtime:{agent:{session:{getSessionEntryAsync:async()=>({sessionId:'s'})}},system:{enqueueSystemEvent:()=>true,requestHeartbeat:()=>{}}}};
 assert.equal((await continueTask(api,call,captured,'0123456789abcdef','activate:0123456789abcdef')).status,'accepted');
 const r=await continueTask(api,call,captured,'0123456789abcdef','action:final:abc');
 assert.equal(r.status,'wake_requested');assert.deepEqual(used,['activate:0123456789abcdef']);
 SPEED.runner=null;
});
test('every tool registered here is declared in the plugin contracts',()=>{
 const tools=[],commands=[];registerWebExtras({registerTool:t=>tools.push(t.name),registerCommand:c=>commands.push(c.name)},async()=>({}),()=>{});
 const declared=JSON.parse(fs.readFileSync(new URL('./openclaw.plugin.json',import.meta.url))).contracts.tools;
 for(const t of tools)assert(declared.includes(t),t);
 assert.deepEqual(commands.sort(),['homeprofile','homeready','homesignin','webfacts','weblane','weblook','webspeed','webview']);
 assert.match(WORKER_TEXT('x','m'),/STOP and end your turn/);
});

test('two-speed worker (Nov 6): an accepted worker run is waited for, then the main assistant checks it; no escalation',async()=>{
 const calls=[],woken=[];
 const gateway=async(m,p)=>{calls.push([m,p]);if(m==='agent')return {runId:'w1',status:'accepted'};if(m==='agent.wait')return {status:'ok',terminalReply:'Reached the absence form; dates filled.'};return {};};
 const call=async(a)=>{calls.push(['broker',a]);return {};};
 const r=await speedRunner(gateway,call)({id:'d7c94bb0c3c6492e',lane:{cheap_model:'haiku'},event_id:'e1',save:async()=>{},wake:async t=>{woken.push(t);}});
 assert.equal(r.status,'accepted');await new Promise(r=>setTimeout(r,20));
 assert.equal(calls.filter(c=>c[0]==='broker'&&c[1]==='task_escalate').length,0);
 assert.equal(woken.length,1);assert.match(woken[0],/Reached the absence form; dates filled\./);
});


test('Nov 6: /homesignin NAME URL adds a website to the PC; a bare name still works',async()=>{
 const commands=[],calls=[];
 registerWebExtras({registerTool(){},registerCommand:c=>commands.push(c)},async(a,x,p)=>{calls.push([a,x,p]);return {profile:x.site,machine:'home',headed:true,created:!!x.url,lane_set:x.url?'georgiapower.com':null};},()=>{});
 const cmd=commands.find(c=>c.name==='homesignin');
 const out=await cmd.handler({args:'GeorgiaPower https://www.georgiapower.com/myaccount',channel:'telegram'});
 assert.deepEqual(calls[0],['home_signin',{site:'GeorgiaPower',url:'https://www.georgiapower.com/myaccount'},true]);
 assert.match(out.text,/\/homeready georgiapower/);assert.match(out.text,/georgiapower\.com is now set to your PC/);
 await cmd.handler({args:'leaveco',channel:'telegram'});assert.deepEqual(calls[1],['home_signin',{site:'leaveco'},true]);
 assert.match((await cmd.handler({args:'georgia power http://x.com',channel:'telegram'})).text,/^Use \/homesignin/);
 assert.equal(calls.length,2);
 await cmd.handler({args:'hotels.com',channel:'telegram'});assert.deepEqual(calls[2],['home_signin',{site:'hotels.com'},true]);   // the broker maps it to "hotels"
});

test('0.20.3: /homeprofile shows and switches the PC browser profile; /homeready names the site',async()=>{
 const commands=[],calls=[];
 registerWebExtras({registerTool(){},registerCommand:c=>commands.push(c)},async(a,x,p)=>{calls.push([a,x,p]);
  if(a==='home_profile')return {mode:x.mode||'shared',profile:'procurator',signed_in_sites:['hotels']};
  return {site:'hotels',profile:'procurator',ready:true,woke_assistant:true};},()=>{});
 const hp=commands.find(c=>c.name==='homeprofile');
 assert.match((await hp.handler({args:'',channel:'telegram'})).text,/one shared Chrome profile[\s\S]*Signed in \(shared profile\): hotels/);
 assert.match((await hp.handler({args:'separate',channel:'telegram'})).text,/one separate Chrome profile per site/);
 assert.match((await hp.handler({args:'incognito',channel:'telegram'})).text,/^Use \/homeprofile/);
 assert.deepEqual(calls.map(c=>[c[0],c[1],c[2]]),[['home_profile',{},true],['home_profile',{mode:'separate'},true]]);
 const out=await commands.find(c=>c.name==='homeready').handler({args:'hotels',channel:'telegram'});
 assert.match(out.text,/^hotels is marked signed in/);assert.match(out.text,/continuing your request/);
});
