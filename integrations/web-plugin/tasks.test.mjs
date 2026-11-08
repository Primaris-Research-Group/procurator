import test from 'node:test';
import assert from 'node:assert/strict';
import {registerWeb} from './register.mjs';
const id='a'.repeat(16),hash='b'.repeat(16);
const context={channel:'telegram',isAuthorizedSender:true,senderId:'100000001',agentId:'main',sessionKey:'agent:main:telegram:direct:100000001',sessionId:'original',accountId:'default'};
function fixture(admission={ok:true}){
 const calls=[],commands=[],seen=new Set();let settle;
 const target={},settled=new Promise(r=>{settle=r});
 const api={registerTool(){},registerCommand:c=>commands.push(c),runtime:{system:{
  async captureSessionEventTarget(a,k){calls.push(['capture',a,k]);return target;},
  enqueueSessionEvent(text,options){calls.push(['enqueue',text,options]);return {accepted:Promise.resolve(admission),settled};}
 }}};
 const call=async(action,args,privileged)=>{
  calls.push([action,args,privileged]);
  if(action==='get')return {id,task_id:id};
  if(action==='task_resume_claim'){if(seen.has(args.event_id))return {claimed:false,status:'completed'};seen.add(args.event_id);return {claimed:true};}
  return {id,status:action==='execute'?'completed':'active'};
 };
 registerWeb(api,call);return {calls,commands,api,target,settle};
}
test('native activation captures destination before effects, admits once and tracks settlement separately',async()=>{
 const f=fixture(),cmd=f.commands.find(x=>x.name==='webtask');
 const reply=await cmd.handler({...context,args:id+' '+hash});assert.match(reply.text,/accepted/);
 assert.equal(f.calls[0][0],'capture');
 assert.equal(f.calls[1][0],'task_activate');assert.equal(f.calls[1][2],true);
 const en=f.calls.find(x=>x[0]==='enqueue');assert.equal(en[2].expectedTarget,f.target);assert.equal(en[2].sessionKey,context.sessionKey);assert.equal(en[2].deliveryContext.to,'100000001');
 assert(!f.calls.some(x=>x[0]==='task_complete'));
 assert.deepEqual(f.calls.filter(x=>x[0]==='task_resume_receipt').map(x=>x[1].status),['accepted']);
 await cmd.handler({...context,args:id+' '+hash});assert.equal(f.calls.filter(x=>x[0]==='enqueue').length,1);
 f.settle({status:'completed'});await new Promise(r=>setImmediate(r));
 assert.deepEqual(f.calls.filter(x=>x[0]==='task_resume_receipt').map(x=>x[1].status),['accepted','completed']);
});
test('unconfirmed admission is not reported as started',async()=>{
 const f=fixture({ok:false}),reply=await f.commands.find(x=>x.name==='webtask').handler({...context,args:id+' '+hash});
 assert.match(reply.text,/admission_unconfirmed/);assert(!f.calls.some(x=>x[0]==='task_complete'));
 f.settle({status:'failed'});
});
test('wrong owner or missing conversation cannot activate a task',async()=>{
 const f=fixture(),cmd=f.commands.find(x=>x.name==='webtask');
 await cmd.handler({...context,senderId:'123',args:id+' '+hash});
 await cmd.handler({...context,sessionId:undefined,args:id+' '+hash});assert.equal(f.calls.length,0);
});
test('task-linked final approval automatically resumes inspection in captured conversation',async()=>{
 const f=fixture(),cmd=f.commands.find(x=>x.name==='webapprove');
 const reply=await cmd.handler({...context,args:id+' '+hash});assert.match(reply.text,/inspecting/);
 assert.deepEqual(f.calls.slice(0,3).map(x=>x[0]),['get','capture','execute']);
 assert.equal(f.calls[2][1].context.sessionId,'original');
 assert.match(f.calls.find(x=>x[0]==='enqueue')[1],/actual confirmation/);
 f.settle({status:'completed'});await new Promise(r=>setImmediate(r));
});
test('pinned legacy SDK requests a wake without claiming a model turn completed',async()=>{
 const f=fixture(),system=f.api.runtime.system;delete system.captureSessionEventTarget;delete system.enqueueSessionEvent;
 f.api.runtime.agent={session:{getSessionEntryAsync:async()=>({sessionId:context.sessionId})}};
 system.enqueueSystemEvent=(text,opts)=>{f.calls.push(['passive',text,opts]);return true};
 system.requestHeartbeat=opts=>f.calls.push(['wake',opts]);
 const reply=await f.commands.find(x=>x.name==='webtask').handler({...context,args:id+' '+hash});
 assert.match(reply.text,/wake_requested/);assert.equal(f.calls.filter(x=>x[0]==='wake').length,1);
 assert.deepEqual(f.calls.filter(x=>x[0]==='task_resume_receipt').map(x=>x[1].status),['wake_requested']);
 assert(!f.calls.some(x=>x[0]==='task_complete'));
});
test('task tool context comes from the runtime factory, never model arguments',async()=>{
 const registrations=[],calls=[];registerWeb({registerTool:(t,opts)=>registrations.push([t,opts]),registerCommand(){}},async(a,args)=>{calls.push([a,args]);return {}});
 const factory=registrations.find(([t,o])=>o.name==='web_task_step')[0];
 const tool=factory(context);await tool.execute('test',{id,request_id:'routine-1',steps:[],runtime_context:{sessionId:'forged'}});
 assert.equal(calls[0][1].runtime_context.sessionId,'original');
 const review=registrations.find(([t,o])=>o.name==='web_task_review')[0](context);await review.execute('test',{id,runtime_context:{sessionId:'forged'}});
 assert.equal(calls[1][1].runtime_context.sessionId,'original');  // /siteauto binds the task to this conversation
});
