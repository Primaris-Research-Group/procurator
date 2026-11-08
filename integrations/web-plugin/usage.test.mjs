import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {UsageMeter,registerUsage,readUsage,usageText,source,missionOf} from './usage.mjs';

test('usage is read from either field style and summed per Eastern day and source',t=>{
 const d=fs.mkdtempSync(path.join(os.tmpdir(),'usage-'));t.after(()=>fs.rmSync(d,{recursive:true,force:true}));
 let now=Date.parse('2026-11-05T15:00:00Z');const m=new UsageMeter(d,()=>now);
 assert.equal(readUsage({}),null);
 assert.ok(m.add({input:1000,output:200,cacheRead:5000},'chat','claude-opus'));
 assert.ok(m.add({input_tokens:300,output_tokens:50,cache_read_input_tokens:0},'missions'));
 now=Date.parse('2026-11-06T04:30:00Z');                 // still Nov 5 in Eastern time
 m.add({input:10,output:10},'chat');
 const s=m.summary(7);
 assert.equal(s.days,1);assert.equal(s.today.calls,3);assert.equal(s.by.chat.calls,2);assert.equal(s.by.missions.input,300);
 const text=usageText(s);
 assert.match(text,/Today: 3 model replies · 6k tokens in \(5k from cache\) · 260 out/);assert.match(text,/Mission work sessions/);assert.ok(!/`/.test(text));
 assert.equal(source({agentId:'phone'}),'phone');assert.equal(source({agentId:'main',contextKey:'procurator-mission:bg:1'}),'missions');
});
test('/meter is owner-only and the hook never throws',async t=>{
 const d=fs.mkdtempSync(path.join(os.tmpdir(),'usage-'));t.after(()=>fs.rmSync(d,{recursive:true,force:true}));
 const hooks=[],commands=[];const m=new UsageMeter(d);
 registerUsage({on:(n,f)=>hooks.push([n,f]),registerCommand:c=>commands.push(c)},m,ctx=>{if(ctx.owner!==true)throw new Error('owner only');});
 hooks[0][1]({usage:{input:5,output:5}},{agentId:'main'});hooks[0][1](null,null);
 assert.match((await commands[0].handler({owner:false})).text,/owner only/);
 assert.match((await commands[0].handler({owner:true,args:''})).text,/Today: 1 model reply/);
 assert.match(usageText(null),/No usage recorded yet/);
});
test('packet work sessions are grouped by packet and charged to its budget; builds are kept apart (Nov 6)',async t=>{
 const d=fs.mkdtempSync(path.join(os.tmpdir(),'usage-'));t.after(()=>fs.rmSync(d,{recursive:true,force:true}));
 const m=new UsageMeter(d,()=>Date.parse('2026-11-06T15:00:00Z'));
 const hooks=[],calls=[];
 registerUsage({on:(n,f)=>hooks.push([n,f]),registerCommand:()=>{}},m,()=>{},{call:async(a,args,p)=>{calls.push([a,args,p]);return {};}});
 hooks[0][1]({usage:{input:1000,output:100,cost:{total:0.02}},contextKey:'procurator-mission:bg:1762440000000'},{agentId:'main',sessionKey:'agent:main:main'});
 hooks[0][1]({usage:{input:10,output:1}},{agentId:'main',sessionKey:'agent:main:main'});
 await new Promise(r=>setTimeout(r,5));
 assert.deepEqual(calls,[['mission_spend',{mission:'bg',cost_usd:0.02},true]]);
 assert.ok(m.addBuild(0.5,'bg'));assert.equal(m.addBuild(0),false);
 const s=m.summary(1);
 assert.equal(s.by_mission.bg.calls,1);assert.equal(s.by_mission.bg.cost.toFixed(2),'0.52');assert.equal(s.by.chat.calls,1);
 assert.deepEqual(s.builds,{count:1,cost:0.5});
 assert.match(usageText(s),/By packet:\n• bg: 1 model reply/);assert.match(usageText(s),/Builds: 1, about \$0.50/);assert.match(usageText(s),/\/panel/);
 assert.equal(m.read().days['2026-11-06'].builds.cost,0.5);
});
test('a packet running in its own session (own model) is still counted as packet work (phase 3)',()=>{
 assert.equal(missionOf({sessionKey:'agent:main:mission-sb'}),'sb');assert.equal(missionOf({sessionKey:'agent:main:main'}),null);
 assert.equal(source({agentId:'main',sessionKey:'agent:main:mission-sb'}),'missions');
});
test('model usage in a conversation working a browser task is charged to that task',async()=>{
 const {noteTask,taskFor,forgetTask}=await import('./taskbind.mjs');
 const fs=await import('node:fs'),os=await import('node:os'),path=await import('node:path');
 const {UsageMeter,registerUsage}=await import('./usage.mjs');
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'u-'));const calls=[];let hook;
 registerUsage({on:(n,f)=>{hook=f;},registerCommand:()=>{}},new UsageMeter(dir),()=>{},{call:async(a,b,p)=>{calls.push([a,b,p]);return {};}});
 noteTask('agent:main:telegram:direct:1','0123456789abcdef');
 assert.equal(taskFor('agent:main:telegram:direct:1'),'0123456789abcdef');
 hook({usage:{input:1000,output:200,cost:{total:0.012}},model:'claude-sonnet-5-5'},{agentId:'main',sessionKey:'agent:main:telegram:direct:1'});
 assert.deepEqual(calls.find(c=>c[0]==='task_cost'),['task_cost',{id:'0123456789abcdef',cost_usd:0.012,tokens:1200,model:'claude-sonnet-5-5'},true]);
 forgetTask('agent:main:telegram:direct:1');calls.length=0;
 hook({usage:{input:10,output:2}},{agentId:'main',sessionKey:'agent:main:telegram:direct:1'});
 assert.equal(calls.filter(c=>c[0]==='task_cost').length,0);
 assert.equal(taskFor('x',Date.now()),null);noteTask('k','not-a-task');assert.equal(taskFor('k'),null);
});
test('0.24.3: /meter includes the Packet Composer spend kept by the broker',async t=>{
 const d=fs.mkdtempSync(path.join(os.tmpdir(),'usage-'));t.after(()=>fs.rmSync(d,{recursive:true,force:true}));
 const hooks=[],commands=[],calls=[];const m=new UsageMeter(d);
 registerUsage({on:(n,f)=>hooks.push([n,f]),registerCommand:c=>commands.push(c)},m,()=>{},{call:async(a,args,p)=>{calls.push([a,args,p]);return a==='composer_daily'?{days:{'2026-11-08':{calls:4,cost:0.2}}}:{};}});
 hooks[0][1]({usage:{input:5,output:5}},{agentId:'main'});
 const text=(await commands[0].handler({args:'30'})).text;
 assert.match(text,/• Packet Composer: 4 model replies · about \$0\.20/);
 assert.deepEqual(calls.find(c=>c[0]==='composer_daily'),['composer_daily',{days:30},true]);
});
