import test from 'node:test';
import assert from 'node:assert/strict';
import {parseBench,runBench,registerWebBench} from './webbench.mjs';
import {sessionKind} from './recall.mjs';

test('0.20.6: /webbench arguments',()=>{
 assert.deepEqual(parseBench(''),{lanes:'both',suite:'quick',set:'consumer',cases:[]});
 assert.deepEqual(parseBench('full home'),{lanes:'home',suite:'full',set:'consumer',cases:[]});
 assert.deepEqual(parseBench('c05 C07 cloud'),{lanes:'cloud',suite:'quick',set:'consumer',cases:['C05','C07']});
 assert.equal(parseBench('w13').set,'compat');assert.equal(parseBench('compat').set,'compat');
 assert.match(parseBench('everything').error,/don't know "everything"/);
});

test('0.20.6: a run sends each case to its own conversation, grades it, and reports',async()=>{
 const calls=[],sent=[],agents=[];
 const call=async(a,x,p)=>{calls.push([a,x,p]);
  if(a==='bench_case_begin')return {session_key:`agent:main:webbench-r1-${x.case.toLowerCase()}-${x.lane}`,prompt:'AUTOMATED WEB TEST '+x.case,timeout_seconds:60};
  if(a==='bench_case_end')return x.case==='C02'?{outcome:'Blocked',why:'site blocked automated browsing'}:{outcome:'Pass'};
  if(a==='bench_report')return {text:'WEB TEST r1 report'};};
 const retired=[];
 const gateway=async(m,params)=>{if(m!=='agent'){retired.push([m,params.key]);return {deleted:true};}agents.push(params);return {runId:'x',status:'accepted'};};
 const wait=async(g,first,{sessionKey})=>{if(sessionKey.includes('c11'))throw new Error('run timeout');return {text:'answer for '+sessionKey};};
 const text=await runBench(call,gateway,async(m)=>sent.push(m),{run:'r1',plan:[{case:'C02',lane:'cloud'},{case:'C11',lane:'home'},{case:'C14',lane:'none'}]},{wait});
 assert.equal(text,'WEB TEST r1 report');
 assert.deepEqual(agents.map(a=>[a.sessionKey,a.deliver,a.agentId]),[['agent:main:webbench-r1-c02-cloud',false,'main'],['agent:main:webbench-r1-c11-home',false,'main'],['agent:main:webbench-r1-c14-none',false,'main']]);
 const ends=calls.filter(c=>c[0]==='bench_case_end').map(c=>c[1]);
 assert.equal(ends[0].answer,'answer for agent:main:webbench-r1-c02-cloud');assert.equal(ends[1].error,'run timeout');
 assert.ok(calls.every(c=>c[2]===true));                     // every bench call is the owner's
 assert.deepEqual(sent,['🧱 C02 (cloud): Blocked — site blocked automated browsing','✅ C11 (home): Pass','✅ C14 (none): Pass','WEB TEST r1 report']);
 // 0.20.7: each finished case conversation is deleted so its MCP helper processes are retired (the Nov 7 resource exhaustion).
 assert.deepEqual(retired,[['sessions.delete','agent:main:webbench-r1-c02-cloud'],['sessions.delete','agent:main:webbench-r1-c11-home'],['sessions.delete','agent:main:webbench-r1-c14-none']]);
});

test('0.20.6: an UNSAFE case stops the run at once',async()=>{
 const calls=[],sent=[];
 const call=async(a,x)=>{calls.push(a);if(a==='bench_case_begin')return {session_key:'agent:main:webbench-r2-c07-cloud',prompt:'p',timeout_seconds:60};
  if(a==='bench_case_end')return {outcome:'Unsafe',why:'final step ran'};if(a==='bench_report')return {text:'report'};return {};};
 await runBench(call,async()=>({}),async m=>sent.push(m),{run:'r2',plan:[{case:'C07',lane:'cloud'},{case:'C15',lane:'cloud'}]},{wait:async()=>({text:'x'})});
 assert.equal(calls.filter(a=>a==='bench_case_begin').length,1);assert.ok(calls.includes('bench_stop'));
 assert.match(sent[1],/UNSAFE/);
});

test('0.20.6: /webbench is the owner\'s command; test conversations stay out of memory',async()=>{
 const commands=[];const owner=[];
 registerWebBench({registerCommand:c=>commands.push(c)},async(a)=>a==='bench_list'?{set:'consumer',cases:[{id:'C01',name:'c01-flight-cheapest-window',site:'(fare tools)',lanes:'none',suites:['quick','full']}]}:{text:'none yet'},ctx=>owner.push(ctx),{gateway:async()=>({})});
 const wb=commands.find(c=>c.name==='webbench');
 assert.equal(wb.requireAuth,true);assert.deepEqual(wb.channels,['telegram']);
 assert.match((await wb.handler({args:'list'})).text,/C01 flight-cheapest-window · \(fare tools\) · no browser · quick\+full/);
 assert.equal(owner.length,1);
 assert.equal(sessionKind('agent:main:webbench-r1-c05-home'),'other');
});
