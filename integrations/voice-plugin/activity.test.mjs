import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {Assignments,OWNER_SESSION} from './assignments.mjs';
import {Reports} from './reports.mjs';
import {CallActivity,callEntry} from './activity.mjs';
const cid='20096062-a0e4-4796-8fda-f2dd43163200';
function fixture(t){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'voice-activity-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 fs.writeFileSync(path.join(dir,'settings.json'),JSON.stringify({testNumber:'+17865550123',maxDurationSeconds:60}));
 const now={t:2_000_000};
 const calls=new Assignments(dir,async()=>({}),()=>now.t);
 const r=calls.prepareAssignment({request_id:'activity-call-01',to:'+17065550123',task:'Ask whether my grievance was received.',opening:'Hello, I am an AI assistant.',success_criteria:'Receipt date and reference number.',menu_goal:'Intake'});
 const j=calls.read();Object.assign(j.records[0],{status:'call_ended',callId:cid,attemptedAt:now.t-60000});calls.save(j);
 const record={callId:cid,agentId:'phone',direction:'outbound',to:r.plan.to,sessionKey:`agent:phone:voice:assignment:${r.id}`,state:'completed',answeredAt:now.t-55000,endedAt:now.t-10000,metadata:{callReport:{status:'pending'}},
  transcript:[{speaker:'bot',text:'Hello'},{speaker:'user',text:'Yes, received on October 28, reference 4471.'}]};
 const reports=new Reports(calls,async()=>record);const posted=[];
 const log=new CallActivity(calls,reports,async e=>{posted.push(e);return {id:1};});
 return {calls,r,record,reports,log,posted,now};
}
test('an ended call is logged once, then updated when its summary arrives',async t=>{
 const f=fixture(t);
 await f.log.sweep();assert.equal(f.posted.length,1);
 const e=f.posted[0];assert.equal(e.key,'call-'+f.r.id);assert.equal(e.kind,'call');assert.equal(e.status,'completed');
 assert.match(e.title,/^Called \+17065550123 — Ask whether/);assert.match(e.details,/Length: 45 s/);assert.match(e.details,/No summary yet/);assert.match(e.details,/\/calltranscript /);
 await f.log.sweep();assert.equal(f.posted.length,1,'unchanged entries are not re-sent');
 f.record.metadata.callReport={status:'delivered',summary:'Outcome: achieved. Received Oct 28, reference 4471.'};
 await f.log.sweep();assert.equal(f.posted.length,2);assert.match(f.posted[1].details,/reference 4471/);assert.equal(f.posted[1].key,f.posted[0].key);
});
test('a log failure is retried on the next sweep and never throws',async t=>{
 const f=fixture(t);let fail=true;f.log.post=async e=>{if(fail)throw new Error('socket down');f.posted.push(e);};
 assert.deepEqual((await f.log.sweep()).map(x=>x.status),['not_logged']);fail=false;
 await f.log.sweep();assert.equal(f.posted.length,1);
});
test('unanswered calls and unconfirmed placements are labelled honestly',()=>{
 const r={id:'abcdef0123456789',kind:'assignment',plan:{to:'+17065550123',task:'X'},attemptedAt:1,callId:cid};
 assert.equal(callEntry(r,{state:'no-answer',endedAt:5000,transcript:[]}).status,'failed');
 assert.equal(callEntry({...r,callId:undefined,status:'uncertain'}).status,'uncertain');
 assert.match(callEntry(r,undefined).details,/never redialed/);
});
