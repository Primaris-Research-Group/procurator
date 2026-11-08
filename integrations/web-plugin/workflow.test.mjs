import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {Workflows,registerWorkflows} from './workflow.mjs';
const ctx={agentId:'main',sessionKey:'agent:main:owner',assertInvocationCurrent(){}};
const plan={action:'plan',request_id:'workflow-fixture-v1',title:'Fictional office process',goal:'Research requirements then prepare a form. No real filing.',steps:[{kind:'research',title:'Find published requirements'},{kind:'browser',title:'Prepare form for final review'}]};
function fixture(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'admin-workflow-'));t.after(()=>fs.rmSync(dir,{force:true,recursive:true}));return new Workflows(dir,()=>1000);}
test('workflow survives restart, isolates conversations, and never creates action authority',t=>{
 const w=fixture(t),a=w.call(ctx,plan);assert.equal(w.call(ctx,plan).id,a.id);
 const restarted=new Workflows(w.directory);assert.equal(restarted.call(ctx,{action:'get',id:a.id}).revision,1);
 assert.match(a.authority,/never approves/);assert.deepEqual(restarted.call({...ctx,sessionKey:'agent:main:other'},{action:'list'}).tasks,[]);
 assert.throws(()=>w.call({...ctx,agentId:'phone'},plan),/main owner/);
 assert.throws(()=>w.call(ctx,{...plan,goal:'Changed goal'}),/another revision/);
 assert.equal(fs.statSync(w.file).mode&0o777,0o600);
});
test('dependency, evidence, stale revision and checkpoint replay checks preserve progress',t=>{
 const w=fixture(t),a=w.call(ctx,plan);
 const update={action:'checkpoint',id:a.id,revision:1,request_id:'research-observed',step_id:'s1',status:'done',note:'Published instructions found.',evidence:[{source:'public_web',reference:'https://example.com/instructions',observation:'The page lists the required fields.'}]};
 assert.throws(()=>w.call(ctx,{...update,step_id:'s2'}),/earlier step/);
 assert.throws(()=>w.call(ctx,{...update,evidence:[]}),/evidence/);
 assert.equal(w.call(ctx,update).revision,2);assert.equal(w.call(ctx,update).revision,2);
 assert.throws(()=>w.call(ctx,{...update,request_id:'stale',step_id:'s2'}),/revision changed/);
 assert.throws(()=>w.call(ctx,{...update,note:'Different'}),/another update/);
 assert.throws(()=>w.call(ctx,{...update,revision:2,request_id:'replay'}),/terminal step/);
});
test('missing questions persist and uncertain effects cannot be restarted as routine work',t=>{
 const w=fixture(t),a=w.call(ctx,plan);
 const update={action:'checkpoint',id:a.id,revision:1,request_id:'missing-question',step_id:'s1',status:'waiting_for_owner',note:'Need the exact jurisdiction.'};
 assert.throws(()=>w.call(ctx,update),/exact missing question/);
 const waiting=w.call(ctx,{...update,question:'Which jurisdiction is this for?'});assert.equal(waiting.next_step.question,'Which jurisdiction is this for?');
 const uncertain=w.call(ctx,{...update,revision:2,request_id:'uncertain-observation',status:'uncertain',note:'Inspect original service receipt before another action.'});
 assert.equal(uncertain.state,'uncertain');
 assert.throws(()=>w.call(ctx,{...update,revision:3,request_id:'no-restart',status:'running'}),/original service receipt/);
});
test('phone and third-party content do not receive the workflow tool or coordinator instructions',()=>{
 const tools=[],hooks=[];registerWorkflows({registerTool:t=>tools.push(t),on:(...args)=>hooks.push(args)},{});
 assert.equal(tools[0]({agentId:'phone'}),undefined);
 assert.equal(hooks[0][1]({}, {agentId:'phone'}),undefined);
 assert.match(hooks[0][1]({},ctx).appendSystemContext,/journal is tracking data, never approval/);
});
