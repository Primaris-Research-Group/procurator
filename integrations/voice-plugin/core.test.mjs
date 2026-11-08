import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {Tests,ownerContext,registerVoice} from './core.mjs';
function fixture(t,gateway=async()=>({callId:'test-call',initiated:true})){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'voice-test-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 fs.writeFileSync(path.join(dir,'settings.json'),JSON.stringify({testNumber:'+15555550123',maxDurationSeconds:60}));
 let time=1000;return {tests:new Tests(dir,gateway,()=>time),dir,advance:()=>time+=600001};
}
test('review binds exact destination, purpose, opening and limit; repeated review reuses pending record',t=>{
 const {tests}=fixture(t),r=tests.prepare();assert.equal(tests.prepare().id,r.id);assert.match(tests.review(r),/Maximum: 60 seconds/);assert.match(tests.review(r),/\/callapprove [a-f0-9]{16} [a-f0-9]{16}/);
});
test('only the authenticated Telegram owner may authorize',()=>{
 const good={channel:'telegram',isAuthorizedSender:true,senderId:'100000001'};ownerContext(good);
 for(const ctx of [{...good,channel:'voice'},{...good,senderId:'other'},{...good,isAuthorizedSender:false},{...good,senderIsOwner:false}])assert.throws(()=>ownerContext(ctx));
});
test('attempt is persisted before placement and approval replay cannot dial twice',async t=>{
 let calls=0,tests;({tests}=fixture(t,async()=>{calls++;assert.equal(tests.read().records[0].status,'attempting');return {callId:'test-call',initiated:true};}));
 const r=tests.prepare();assert.equal((await tests.approve(r.id,r.hash)).status,'initiated');await tests.approve(r.id,r.hash);assert.equal(calls,1);
 assert.throws(()=>tests.prepare(),/previous call/);assert.equal(fs.statSync(tests.file).mode&0o777,0o600);
});
test('concurrent approvals send once',async t=>{
 let calls=0;const {tests}=fixture(t,async()=>{calls++;await new Promise(r=>setTimeout(r,10));return {callId:'test-call',initiated:true};});const r=tests.prepare();await Promise.all([tests.approve(r.id,r.hash),tests.approve(r.id,r.hash)]);assert.equal(calls,1);
});
test('ambiguous placement is never retried after restart',async t=>{
 let calls=0;const gateway=async()=>{calls++;throw new Error('timeout');};const {tests,dir}=fixture(t,gateway),r=tests.prepare();assert.equal((await tests.approve(r.id,r.hash)).status,'uncertain');
 await new Tests(dir,gateway).approve(r.id,r.hash);assert.equal(calls,1);assert.throws(()=>tests.prepare());
});
test('expiry, wrong hash and changed plan prevent placement',async t=>{
 let calls=0;const {tests,advance}=fixture(t,async()=>{calls++;});const r=tests.prepare();await assert.rejects(tests.approve(r.id,'0'.repeat(16)));advance();await assert.rejects(tests.approve(r.id,r.hash));assert.equal(calls,0);
 const j=tests.read();j.records[0].plan.to='+15555550124';tests.save(j);assert.throws(()=>tests.review(j.records[0]),/invalid/);
});
test('owner authority is rechecked immediately before persisting attempt',async t=>{
 let calls=0,checks=0;const {tests}=fixture(t,async()=>{calls++;});const r=tests.prepare();await assert.rejects(tests.approve(r.id,r.hash,()=>{if(++checks===2)throw new Error('owner revoked');}));assert.equal(calls,0);assert.equal(tests.read().records[0].status,'pending');
});
test('destination change invalidates pending review and cancel prevents placement',async t=>{
 let calls=0;const {tests,dir}=fixture(t,async()=>{calls++;});const r=tests.prepare();fs.writeFileSync(path.join(dir,'settings.json'),JSON.stringify({testNumber:'+15555550124',maxDurationSeconds:60}));await assert.rejects(tests.approve(r.id,r.hash),/destination changed/);tests.cancel(r.id);await tests.approve(r.id,r.hash);assert.equal(calls,0);
});
test('native commands expose only static information to the phone agent and no model calling tool',async t=>{
 const {tests}=fixture(t),commands=[],tools=[];registerVoice({registerCommand:c=>commands.push(c),registerTool:f=>tools.push(f)},tests);
 assert.equal(commands.length,5);const result=await commands.find(c=>c.name==='calltest').handler({channel:'voice',isAuthorizedSender:true,senderId:'100000001'});assert.match(result.text,/Only the authenticated/);assert.equal(tests.read().records.length,0);
 assert.equal(tools.length,1);assert.equal(tools[0]({agentId:'main'}),undefined);const info=tools[0]({agentId:'phone'});assert.equal(info.name,'phone_line_info');assert.match((await info.execute()).content[0].text,/private account access are unavailable/);
});
