import test from 'node:test';
import assert from 'node:assert/strict';
import {chatRunner,chunks,replyText,awaitRun} from './directrun.mjs';
import {wakeMain,SPEED,continuationText} from './tasks.mjs';

const fakeGateway=(behaviour)=>{const calls=[];const g=async(method,params)=>{calls.push([method,params]);if(method==='agent')return behaviour(params);return {ok:true};};g.calls=calls;return g;};
const ok=text=>({status:'ok',result:{payloads:[{text}]}});

test('a direct run in their conversation sends the reply and reports completed (0.19.2)',async()=>{
 const g=fakeGateway(()=>ok('Opened irs.gov; the W-4 is Rev. December 2025.'));
 const settled=[];
 const s=await chatRunner(g,{sleep:()=>new Promise(()=>{})})('Continue task',{sessionKey:'agent:main:telegram:direct:100000001',key:'task:a:b',onSettled:x=>settled.push(x)});
 assert.equal(s,'completed');
 const [m,p]=g.calls[0];assert.equal(m,'agent');assert.equal(p.sessionKey,'agent:main:telegram:direct:100000001');assert.equal(p.deliver,false);assert.match(p.idempotencyKey,/^procurator-run:task:a:b:[0-9a-z]+$/);
 assert.equal(g.calls[1][0],'send');assert.equal(g.calls[1][1].message,'Opened irs.gov; the W-4 is Rev. December 2025.');assert.equal(g.calls[1][1].to,'100000001');assert.match(g.calls[1][1].idempotencyKey,/^procurator-reply:task:a:b:[0-9a-z]+:0$/);
 await new Promise(r=>setImmediate(r));assert.deepEqual(settled,['completed']);
});

test('a long run reports started, NO_REPLY sends nothing, and a refused run reports failed',async()=>{
 let release;const slow=fakeGateway(()=>new Promise(r=>{release=()=>r(ok('NO_REPLY'));}));
 const settled=[];
 const s=await chatRunner(slow,{sleep:()=>Promise.resolve()})('x',{sessionKey:'agent:main:telegram:direct:1',key:'k',onSettled:x=>settled.push(x)});
 assert.equal(s,'started');release();await new Promise(r=>setTimeout(r,5));
 assert.deepEqual(settled,['completed']);assert.equal(slow.calls.filter(c=>c[0]==='send').length,0);
 const bad=fakeGateway(()=>{throw new Error('unknown session');});
 assert.match(await chatRunner(bad,{sleep:()=>new Promise(()=>{})})('x',{sessionKey:'agent:main:telegram:direct:1',key:'k'}),/^failed: unknown session/);
 assert.match(await chatRunner(bad)('x',{sessionKey:'nope',key:'k'}),/^failed: no conversation/);
});

test('long replies are split for Telegram',()=>{
 const parts=chunks('a'.repeat(3000)+'\n'+'b'.repeat(3000));assert.equal(parts.length,2);assert.ok(parts.every(p=>p.length<=3800));
 assert.equal(replyText({result:{payloads:[{text:'x'},{text:'hidden',isReasoning:true}]}}),'x');
});

test('an approved task starts a direct run and says so; a failed start says nothing was done',async()=>{
 const saved=[];const save=s=>{saved.push(s);return Promise.resolve();};
 const captured={context:{agentId:'main',sessionKey:'agent:main:telegram:direct:100000001',sessionId:'s'},legacy:true};
 SPEED.chat=async(text,{sessionKey,key,onSettled})=>{assert.equal(sessionKey,captured.context.sessionKey);assert.equal(key,'task:t1:activate:t1');setTimeout(()=>onSettled('completed'),1);return 'started';};
 const r=await wakeMain({},captured,'t1','activate:t1','Continue',save);
 assert.deepEqual(r,{queued:true,status:'run_started'});await new Promise(r=>setTimeout(r,5));
 assert.deepEqual(saved,['run_started','completed']);
 assert.match(continuationText('t1',r),/approved and started/);
 const notes=[];SPEED.notify=async m=>notes.push(m);
 SPEED.chat=async()=>'failed: gateway refused';
 const f=await wakeMain({},captured,'t2','activate:t2','Continue',save);
 assert.deepEqual(f,{queued:false,status:'run_failed',reason:'gateway refused'});
 assert.match(continuationText('t2',f),/could not be started \(gateway refused\)\. Nothing was done/);
 SPEED.chat=null;SPEED.notify=null;
});

// Nov 6 (Leaveco, live): the gateway answers `agent` with {runId, status:'accepted'} and the outcome comes from agent.wait.
const liveGateway=(waits,history)=>{const calls=[];const g=async(method,params)=>{calls.push([method,params]);
 if(method==='agent')return {runId:'run-1',status:'accepted',acceptedAt:1};
 if(method==='agent.wait')return waits.shift();
 if(method==='chat.history')return history??{messages:[]};
 return {ok:true};};g.calls=calls;return g;};

test('an accepted run is waited for with agent.wait and its terminal reply is sent (not reported as failed)',async()=>{
 const g=liveGateway([{status:'accepted'},{status:'ok',terminalReply:{text:'Signed in to Leaveco; the absence form is open.'}}]);
 const settled=[];
 const s=await chatRunner(g,{sleep:()=>new Promise(()=>{})})('Continue task',{sessionKey:'agent:main:main',key:'task:a:b',onSettled:x=>settled.push(x)});
 assert.equal(s,'completed');assert.deepEqual(settled.length?settled:['completed'],['completed']);
 assert.deepEqual(g.calls.filter(c=>c[0]==='agent.wait').map(c=>c[1].runId),['run-1','run-1']);
 assert.equal(g.calls.find(c=>c[0]==='send')[1].message,'Signed in to Leaveco; the absence form is open.');
});

test('awaitRun: a failed run throws with its reason, a string reply works, and chat.history is the last resort',async()=>{
 await assert.rejects(awaitRun(liveGateway([{status:'error',error:'model overloaded'}]),{runId:'r',status:'accepted'}),/run error: model overloaded/);
 assert.equal((await awaitRun(liveGateway([{status:'ok',terminalReply:'Done.'}]),{runId:'r',status:'accepted'})).text,'Done.');
 const h=liveGateway([{status:'ok'}],{messages:[{role:'user',content:'go'},{role:'assistant',content:[{type:'text',text:'All set.'}]}]});
 assert.equal((await awaitRun(h,{runId:'r',status:'accepted'},{sessionKey:'agent:main:main'})).text,'All set.');
 const mine=liveGateway([{status:'ok'}],{messages:[{role:'assistant',content:'old'},{role:'user',content:'go'}]});
 assert.equal((await awaitRun(mine,{runId:'r',status:'accepted'},{sessionKey:'agent:main:main'})).text,'');   // newest is not a reply
 let t=0;const still=liveGateway(Array.from({length:50},()=>({status:'accepted'})));
 await assert.rejects(awaitRun(still,{runId:'r',status:'accepted'},{timeoutMs:3000,now:()=>(t+=1000)}),/still running/);
 // 0.20.7: agent.wait's own 'timeout' (its window ended, the run goes on) is waited through, not reported as a failure…
 const slow=liveGateway([{status:'timeout'},{status:'timeout'},{status:'ok',terminalReply:'Found it after 12 minutes.'}]);
 assert.equal((await awaitRun(slow,{runId:'r',status:'accepted'},{timeoutMs:900000,sessionKey:'agent:main:webbench-x'})).text,'Found it after 12 minutes.');
 assert.equal(slow.calls.filter(c=>c[0]==='agent.wait').length,3);assert.equal(slow.calls.filter(c=>c[0]==='chat.abort').length,0);
 // …and past OUR deadline the run is stopped (chat.abort on its conversation) before the failure is reported.
 let t2=0;const over=liveGateway(Array.from({length:50},()=>({status:'timeout'})));
 await assert.rejects(awaitRun(over,{runId:'r',status:'accepted'},{timeoutMs:3000,now:()=>(t2+=1000),sessionKey:'agent:main:webbench-x'}),/still running after 0 minutes \(stopped\)/);
 assert.deepEqual(over.calls.filter(c=>c[0]==='chat.abort').map(c=>c[1]),[{sessionKey:'agent:main:webbench-x',runId:'r'}]);
 let t3=0;const main=liveGateway(Array.from({length:50},()=>({status:'timeout'})));
 await assert.rejects(awaitRun(main,{runId:'r',status:'accepted'},{timeoutMs:3000,now:()=>(t3+=1000),sessionKey:'agent:main:main',stopOnDeadline:false}),/still running/);
 assert.equal(main.calls.filter(c=>c[0]==='chat.abort').length,0);   // their own conversation is never stopped
 const lost=liveGateway([{status:'timeout',unknownRun:true}]);
 await assert.rejects(awaitRun(lost,{runId:'r',status:'accepted'},{sessionKey:'agent:main:main'}),/lost/);
 assert.equal((await awaitRun(async()=>{throw new Error('no wait');},{status:'ok',result:{payloads:[{text:'Inline.'}]}})).text,'Inline.');   // a final result needs no wait
});
