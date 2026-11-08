import {TURN_NOTE} from './capabilities.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerMissions,TICK_TEXT,packetRunner} from './missions.mjs';

function fixture(responses={}){
 const calls=[],tools=[],commands=[],hooks=[],woke=[];
 const call=async(action,args,privileged=false)=>{calls.push([action,args,privileged]);const r=responses[action];if(r instanceof Error)throw r;return typeof r==='function'?r(args):r??{};};
 const api={registerTool:(f,o)=>tools.push([f,o]),registerCommand:c=>commands.push(c),on:(n,f,o)=>hooks.push([n,f,o])};
 const owner=[];const reg=registerMissions(api,call,ctx=>owner.push(ctx),{wake:async(text,key)=>{woke.push([text,key]);return 'accepted';},setTimer:()=>null});
 return {calls,tools,commands,hooks,woke,owner,reg};
}

test('mission tools are main-agent only and map to the broker',async()=>{
 const f=fixture({mission_get:{mission:'bg'},mission_upload_link:{sent:true}});
 const names=f.tools.map(([,o])=>o.name);
 for(const n of ['mission_list','mission_get','mission_read','mission_fact','mission_log','mission_question','mission_deadline','mission_thread','mission_next','mission_send_upload_link','mission_import'])assert.ok(names.includes(n),n);
 const get=f.tools.find(([,o])=>o.name==='mission_get')[0];
 assert.equal(get({agentId:'phone'}),undefined);
 await get({agentId:'main'}).execute('1',{mission:'bg'});assert.deepEqual(f.calls.find(c=>c[0]==='mission_get'),['mission_get',{mission:'bg'},false]);
 const link=f.tools.find(([,o])=>o.name==='mission_send_upload_link')[0]({agentId:'main'});
 await link.execute('2',{});assert.ok(f.calls.some(c=>c[0]==='mission_upload_link'&&c[2]===false),'the model can only ask for the link to be sent');
});

test('errors come back as tool errors, not crashes',async()=>{
 const f=fixture({mission_fact:new Error('A document_verified fact needs at least one source reference')});
 const r=await f.tools.find(([,o])=>o.name==='mission_fact')[0]({agentId:'main'}).execute('1',{mission:'bg'});
 assert.equal(r.isError,true);assert.match(r.content[0].text,/needs at least one source/);
});

test('/mission commands use the privileged socket only for owner controls',async()=>{
 const f=fixture({mission_list:{missions:[{mission:'bg',title:'Legal Matter',revision:'BG-2026-11-05-r1',status:'draft'}]},mission_owner:{mission:'bg',status:'active',next_tick:'Thu Nov 05 11:30 AM ET'},mission_upload_link:{sent:true}});
 const cmd=f.commands.find(c=>c.name==='mission');
 assert.match((await cmd.handler({args:''})).text,/bg — Legal Matter \(BG-2026-11-05-r1\) · draft/);
 assert.match((await cmd.handler({args:'activate bg'})).text,/Mission bg: active/);
 assert.deepEqual(f.calls.find(c=>c[0]==='mission_owner'),['mission_owner',{mission:'bg',action:'activate'},true]);
 await cmd.handler({args:'upload'});assert.ok(f.calls.some(c=>c[0]==='mission_upload_link'&&c[2]===true));
 assert.match((await cmd.handler({args:'pause'})).text,/Add the mission name/);
 assert.equal(f.owner.length,4,'every command checks the owner');
});

test('due missions wake the assistant with a tick that never replays and stays quiet when nothing changed',async()=>{
 const f=fixture({mission_due:{due:[{mission:'bg',title:'Legal Matter',revision:'BG-2026-11-05-r1'}]},mission_context:{text:'MISSIONS …'}});
 assert.equal(await f.reg.tick(),1);
 const [text,key]=f.woke[0];assert.match(key,/^procurator-mission:bg:/);
 for(const want of [/mission_next/,/never replay it/,/native approval/,/exactly NO_REPLY/,/BG-2026-11-05-r1/])assert.match(text,want);
 await f.reg.refresh();
 const hook=f.hooks.find(([n])=>n==='before_prompt_build')[1];
 assert.equal(hook({}, {agentId:'main'}).appendContext,TURN_NOTE+'MISSIONS …');assert.equal(hook({}, {agentId:'main'}).appendSystemContext,undefined);assert.equal(hook({}, {agentId:'phone'}),undefined);
 assert.match(TICK_TEXT({mission:'x',revision:'r',title:'t'}),/mission_question/);
});

test('watched inboxes are checked read-only through mail and new mail runs a tick',async()=>{
 const calls=[],mailCalls=[],woke=[];
 const responses={mission_watch_due:{watches:[{mission:'bg',watch_id:'ethics',account:'gmail',query:'from:ethics@example.gov'}]},
  mission_watch_seen:{new:1},mission_due:{due:[{mission:'bg',title:'BG',revision:'r1',reasons:['1 new email(s) matching watch ethics']}]}};
 const call=async(a,args,p=false)=>{calls.push([a,args,p]);return responses[a]??{};};
 const api={registerTool(){},registerCommand(){},on(){}};
 const reg=registerMissions(api,call,()=>{},{wake:async(t,k)=>{woke.push(t);return 'accepted';},mail:async(a,args)=>{mailCalls.push([a,args]);return {messages:[{id:'m1'}]};},setTimer:()=>null});
 assert.equal(await reg.checkWatches(),1);
 assert.deepEqual(mailCalls,[['watch_search',{account:'gmail',query:'from:ethics@example.gov'}]]);
 assert.deepEqual(calls.find(c=>c[0]==='mission_watch_seen')[1].messages,[{id:'m1'}]);
 assert.match(woke[0],/Woken because: 1 new email/);
 assert.ok(calls.every(c=>c[2]===false),'no privileged calls');
});
test('a failed mail check is recorded, not thrown',async()=>{
 const calls=[];const call=async(a,args)=>{calls.push([a,args]);return a==='mission_watch_due'?{watches:[{mission:'bg',watch_id:'w',account:'icloud',query:'x'}]}:{};};
 const reg=registerMissions({registerTool(){},registerCommand(){},on(){}},call,()=>{},{mail:async()=>{throw new Error('iCloud unavailable');},setTimer:()=>null});
 assert.equal(await reg.checkWatches(),0);
 assert.equal(calls.find(c=>c[0]==='mission_watch_seen')[1].error,'iCloud unavailable');
});

test('phase 3: the tick carries the packet brief; a packet with its own model runs in its own session, else the main chat',async()=>{
 const calls=[],woke=[],ran=[],told=[];
 const call=async(a,args)=>{calls.push([a,args]);return a==='mission_due'?{due:[{mission:'bg',title:'Bar',revision:'r1',reasons:[],brief:'PACKET bg (legal packet',rules:'HOW TO RUN A STEP',model:null},{mission:'sb',title:'SB',revision:'r1',reasons:[],brief:'PACKET sb',model:'opus'},{mission:'zz',title:'ZZ',revision:'r1',reasons:[],model:'sol'}]}:{};};
 const reg=registerMissions({registerTool:()=>{},registerCommand:()=>{},on:()=>{}},call,()=>{},{wake:async(text,key)=>{woke.push([text,key]);return 'accepted';},setTimer:()=>null,
  runAgent:async(text,o)=>{ran.push([text,o]);return o.mission==='zz'?'failed: model selection refused (not allowed)':'accepted';},notify:async m=>told.push(m)});
 assert.equal(await reg.tick(),3);
 assert.equal(told.length,1);assert.match(told[0],/Packet zz: its model \(sol\) could not be used \(failed: model selection refused/);assert.match(told[0],/runs on the main model/);
 assert.match(woke[0][0],/PACKET bg \(legal packet/);assert.match(woke[0][0],/HOW TO RUN A STEP/);assert.match(woke[0][0],/Work ONLY on packet "bg"/);
 assert.deepEqual(ran.map(r=>r[1]),[{mission:'sb',model:'opus'},{mission:'zz',model:'sol'}]);
 assert.deepEqual(woke.slice(0,2).map(w=>w[1].split(':')[1]),['bg','zz'],'zz fell back to the main conversation');
 assert.ok(calls.some(c=>c[0]==='mission_log'&&/sol was not used .*ran on the main model/.test(c[1].summary)));
 await reg.tick();assert.equal(told.length,1,'told once per packet and reason');
});

test('phase 3 / 0.24.3: packetRunner selects the model on the packet session (sessions.patch), never by agent overrides',async()=>{
 const g=[];let reply='Step MAIN/s1 done: confirmation 88213.';
 const gateway=async(method,params)=>{g.push([method,params]);return method==='agent'?{status:'ok',result:{payloads:[{text:reply}]}}:{};};
 const run=packetRunner(gateway,{opus:{ref:'anthropic/claude-opus-5-5',label:'Claude Opus 5.5'}},async()=>({}),{quickMs:50});
 assert.equal(await run('TICK',{mission:'sb',model:'opus'}),'accepted');
 await new Promise(r=>setTimeout(r,10));
 assert.deepEqual(g[0],['sessions.patch',{key:'agent:main:mission-sb',model:'anthropic/claude-opus-5-5'}]);
 const agent=g.find(x=>x[0]==='agent')[1];
 assert.equal(agent.sessionKey,'agent:main:mission-sb');assert.equal(agent.provider,undefined);assert.equal(agent.model,undefined);assert.equal(agent.deliver,false);
 assert.match(g.find(x=>x[0]==='send')[1].message,/^Packet sb \(Claude Opus 5.5\):\nStep MAIN\/s1 done/);
 const refused=packetRunner(async m=>{if(m==='sessions.patch')throw new Error('model not allowed');return {};},{opus:{ref:'anthropic/claude-opus-5-5'}},async()=>({}),{quickMs:50});
 assert.equal(await refused('TICK',{mission:'sb',model:'opus'}),'failed: model selection refused (model not allowed)');
 g.length=0;reply='NO_REPLY';await run('TICK',{mission:'sb',model:'opus'});await new Promise(r=>setTimeout(r,10));
 assert.ok(!g.some(x=>x[0]==='send'),'NO_REPLY sends nothing');
 assert.match(await run('TICK',{mission:'sb',model:'gpt-9'}),/unknown model/);
 const bad=packetRunner(async m=>{if(m==='agent')throw new Error('unknown method agent');return {};},{opus:{ref:'anthropic/claude-opus-5-5'}},async()=>({}),{quickMs:50});
 assert.match(await bad('TICK',{mission:'sb',model:'opus'}),/failed: unknown method/);
});

test('phase 3: /mission budget, model and dryrun go through the privileged socket',async()=>{
 const f=fixture({mission_owner:a=>({mission:a.mission,status:'active'}),mission_dry_run:{sent:true}});
 const cmd=f.commands.find(c=>c.name==='mission');
 const owner=()=>f.calls.filter(c=>c[0]==='mission_owner').at(-1);
 await cmd.handler({args:'budget bg $25'});assert.deepEqual(owner(),['mission_owner',{mission:'bg',action:'budget',budget_usd:'25'},true]);
 await cmd.handler({args:'budget bg none'});assert.equal(owner()[1].budget_usd,null);
 await cmd.handler({args:'model bg main'});assert.equal(owner()[1].model,'');
 await cmd.handler({args:'model bg Opus'});assert.equal(owner()[1].model,'opus');
 assert.match((await cmd.handler({args:'dryrun bg'})).text,/read-only plan/);assert.deepEqual(f.calls.at(-1),['mission_dry_run',{mission:'bg'},true]);
 assert.match((await cmd.handler({args:'budget bg'})).text,/Add the amount/);
 for(const a of ['dry run bg','Dry-Run bg','dry_run bg','DRYRUN bg']){f.calls.length=0;assert.match((await cmd.handler({args:a})).text,/read-only plan/,a);assert.deepEqual(f.calls[0],['mission_dry_run',{mission:'bg'},true]);}
 const names=f.tools.map(([,o])=>o.name);for(const n of ['mission_propose_done','mission_brief'])assert.ok(names.includes(n),n);
});

test('every mission tool is declared in the plugin manifest (the gateway refuses undeclared tools)',async()=>{
 const {readFileSync}=await import('node:fs');
 const declared=JSON.parse(readFileSync(new URL('./openclaw.plugin.json',import.meta.url))).contracts.tools;
 const f=fixture({});
 for(const [,o] of f.tools)assert.ok(declared.includes(o.name),o.name+' missing from openclaw.plugin.json contracts.tools');
});

test('phase 3 (Nov 6): an accepted packet run is waited for, not logged as failed',async()=>{
 const calls=[],logs=[];
 const gateway=async(m,p)=>{calls.push([m,p]);if(m==='agent')return {runId:'r9',status:'accepted'};if(m==='agent.wait')return {status:'ok',terminalReply:{text:'Filed the NJ request.'}};return {};};
 const run=packetRunner(gateway,{opus:{ref:'anthropic/claude-opus-5-5',label:'Claude Opus 5.5'}},async(a,x)=>{logs.push([a,x]);return {};},{quickMs:5});
 await run('TICK',{mission:'bg',model:'opus'});await new Promise(r=>setTimeout(r,20));
 assert.equal(calls.find(c=>c[0]==='send')[1].message,'Packet bg (Claude Opus 5.5):\nFiled the NJ request.');assert.equal(logs.length,0);
});

