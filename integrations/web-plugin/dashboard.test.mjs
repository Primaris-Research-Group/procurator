import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerDashboard} from './dashboard.mjs';

test('/dashboard sends a link through the privileged socket and /dashboard logout revokes',async()=>{
 const calls=[],commands=[];
 registerDashboard({registerCommand:c=>commands.push(c),registerTool:()=>{}},async(a,args,p)=>{calls.push([a,p]);return a==='dashboard_logout'?{revoked:2}:{sent:true};},ctx=>{if(!ctx.owner)throw new Error('owner only');});
 assert.deepEqual(commands.map(c=>c.name),['panel','dashboard','compose','composerbench']);   // 0.19.1: /panel (OpenClaw has a built-in /dashboard)
 const cmd=commands.find(c=>c.name==='panel');
 assert.match((await cmd.handler({owner:false})).text,/owner only/);
 assert.match((await cmd.handler({owner:true,args:''})).text,/sign-in link is above/);
 assert.deepEqual(calls.at(-1),['dashboard_link',true]);
 assert.match((await cmd.handler({owner:true,args:'logout'})).text,/2 devices/);
 assert.deepEqual(calls.at(-1),['dashboard_logout',true]);
});

test('0.24.0: /compose opens the Packet Composer over the privileged socket; mission_compose offers it from the assistant',async()=>{
 const calls=[],commands=[],tools=[];
 registerDashboard({registerCommand:c=>commands.push(c),registerTool:t=>tools.push(t)},async(a,args,p)=>{calls.push([a,args,p]);return {sent:true,session:'0123456789abcdef'};},ctx=>{if(!ctx.owner)throw new Error('owner only');});
 const cmd=commands.find(c=>c.name==='compose');
 assert.match((await cmd.handler({owner:false,args:'x'})).text,/owner only/);
 assert.match((await cmd.handler({owner:true,args:'Report my Nov 20 absence on Leaveco'})).text,/Packet Composer link is above/);
 assert.deepEqual(calls.at(-1),['composer_start',{seed:'Report my Nov 20 absence on Leaveco'},true]);
 await cmd.handler({owner:true,args:''});assert.deepEqual(calls.at(-1),['composer_start',{},true]);
 const t=tools.find(x=>x.name==='mission_compose');
 const r=JSON.parse((await t.execute('x',{summary:'Set up the records request'})).content[0].text);
 assert.equal(r.session,'0123456789abcdef');assert.deepEqual(calls.at(-1),['composer_start',{seed:'Set up the records request'},undefined]);
});

test('0.24.1: /composerbench runs the Composer bench over the privileged socket (owner only)',async()=>{
 const calls=[],commands=[];
 registerDashboard({registerCommand:c=>commands.push(c),registerTool:()=>{}},async(a,args,p)=>{calls.push([a,args,p]);return {started:true,personas:args.quick?3:8};},ctx=>{if(!ctx.owner)throw new Error('owner only');});
 const cmd=commands.find(c=>c.name==='composerbench');
 assert.match((await cmd.handler({owner:false,args:''})).text,/owner only/);
 assert.match((await cmd.handler({owner:true,args:'quick'})).text,/3 scripted owners/);
 assert.deepEqual(calls.at(-1),['composer_bench',{quick:true},true]);
 assert.match((await cmd.handler({owner:true,args:''})).text,/8 scripted owners/);
});

test('0.24.1: mission_compose_attach adds a document to the assistant\'s Composer session (public socket)',async()=>{
 const calls=[],tools=[];
 registerDashboard({registerCommand:()=>{},registerTool:t=>tools.push(t)},async(a,args,p)=>{calls.push([a,args,p]);return {attached:args.name,chars:5,attachments:[args.name]};},()=>{});
 const t=tools.find(x=>x.name==='mission_compose_attach');
 const r=JSON.parse((await t.execute('x',{session:'0123456789abcdef',name:'01-policy.md',text:'hello'})).content[0].text);
 assert.equal(r.attached,'01-policy.md');assert.deepEqual(calls.at(-1),['composer_offer_attach',{session:'0123456789abcdef',name:'01-policy.md',text:'hello'},undefined]);
});
