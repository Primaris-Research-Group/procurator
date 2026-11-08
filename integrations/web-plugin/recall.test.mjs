import test from 'node:test';
import assert from 'node:assert/strict';
import {registerRecall,turnFrom,sessionKind,textOf} from './recall.mjs';

function fakeApi(){const hooks={},tools=[],commands=[];return {hooks,tools,commands,on:(n,f,o)=>{(hooks[n]??=[]).push({f,o});},registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)};}
const owner=ctx=>{if(!ctx.owner)throw new Error('owner only');};

test('turns are taken from the final messages of their conversation only (0.20.0)',async()=>{
 assert.equal(sessionKind('agent:main:telegram:direct:100000001'),'chat');assert.equal(sessionKind('agent:main:mission-bg'),'mission');assert.equal(sessionKind('agent:main:webtask-ab'),'webtask');assert.equal(sessionKind('agent:main:cron:nightly'),'other');assert.equal(sessionKind('agent:main:subagent:x'),'other');
 assert.equal(textOf({content:[{type:'text',text:'a'},{type:'tool_use',name:'x'},{type:'text',text:'b'}]}),'a\nb');
 assert.deepEqual(turnFrom({messages:[{role:'user',content:'old'},{role:'assistant',content:'old reply'},{role:'user',content:'What is the W-4 date?'},
  {role:'assistant',content:[{type:'tool_use',name:'web_browser_open'}]},{role:'tool',content:'page'},{role:'assistant',content:[{type:'text',text:'Rev. December 2025.'}]}]}),{owner:'What is the W-4 date?',reply:'Rev. December 2025.'});
 const api=fakeApi(),calls=[];
 const call=async(a,args,p)=>{calls.push([a,args,p]);return a==='recall_context'?{text:'MEMORY (…)\n• Thu: W-4'}:{};};
 registerRecall(api,call,owner,{setTimer:()=>null});await new Promise(r=>setImmediate(r));
 api.hooks.agent_end[0].f({runId:'r1',messages:[{role:'user',content:'hi'},{role:'assistant',content:'hello'}]},{agentId:'main',sessionKey:'agent:main:telegram:direct:100000001'});
 api.hooks.agent_end[0].f({runId:'r2',messages:[{role:'user',content:'MISSION TICK'},{role:'assistant',content:'NO_REPLY'}]},{agentId:'main',sessionKey:'agent:main:mission-bg'});
 api.hooks.agent_end[0].f({runId:'r3',success:false,messages:[{role:'user',content:'x'}]},{agentId:'main',sessionKey:'agent:main:main'});
 await new Promise(r=>setImmediate(r));
 const turns=calls.filter(c=>c[0]==='recall_turn');assert.equal(turns.length,1);
 assert.deepEqual(turns[0],['recall_turn',{session_kind:'chat',owner_text:'hi',reply_text:'hello',run_id:'r1'},true]);
 const built=api.hooks.before_prompt_build[0].f({}, {agentId:'main',sessionKey:'agent:main:main'});assert.match(built.appendContext,/not typed by the owner\.\)\nMEMORY/);assert.equal(built.appendSystemContext,undefined);
 assert.equal(api.hooks.before_prompt_build[0].f({}, {agentId:'main',sessionKey:'agent:main:mission-bg'}),undefined);
 api.hooks.before_reset[0].f({reason:'daily'},{agentId:'main',sessionKey:'agent:main:main'});await new Promise(r=>setImmediate(r));
 assert.deepEqual(calls.find(c=>c[0]==='recall_digest'),['recall_digest',{reason:'reset:daily'},true]);
 assert.deepEqual(api.tools.map(t=>t.name),['memory_search','memory_note']);
});

test('/memory is owner-only and maps to the owner socket',async()=>{
 const api=fakeApi(),calls=[];
 const call=async(a,args,p)=>{calls.push([a,args,p]);if(a==='recall_status')return {memory:'on',turns:4,waiting:2,digests:1,open_items:1,lessons:2,facts:0,last_digest:{span:'Thu Nov 5–Thu Nov 5',model:'claude-haiku-5-5',summary:'W-4 and CVS.'}};
  if(a==='recall_digest')return {digest:2,turns:2,model:'claude-haiku-5-5',notes_added:1,summary:'Flu shot.'};if(a==='recall_forget')return {forgotten:3};return {text:''};};
 registerRecall(api,call,owner,{setTimer:()=>null});const cmd=api.commands[0];
 assert.equal((await cmd.handler({owner:false,args:''})).text,'owner only');
 assert.match((await cmd.handler({owner:true,args:''})).text,/Memory: on · 4 messages kept .* 1 open items · 2 lessons/);
 assert.match((await cmd.handler({owner:true,args:'now'})).text,/Summarized 2 messages/);
 assert.match((await cmd.handler({owner:true,args:'forget Acme Telecom invoice'})).text,/Forgotten: 3/);
 assert.deepEqual(calls.find(c=>c[0]==='recall_forget'),['recall_forget',{what:'Acme Telecom invoice'},true]);
 await cmd.handler({owner:true,args:'off'});assert.deepEqual(calls.find(c=>c[0]==='recall_set'),['recall_set',{on:false},true]);
});
