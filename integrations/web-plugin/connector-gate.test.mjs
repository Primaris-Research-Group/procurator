import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gateTool,withConnectorGate,outboundText,loadPolicy} from './connector-gate.mjs';

const policy={mode:'enforce',tools:{
 lookup:{risk:'read'},
 mail_create_draft:{risk:'draft',outbound:true,destination:'to'},
 calendar_create_event:{risk:'own_write',outbound:true,destination:'guests'},
 web_task_step:{risk:'external',outbound:true,destination:'id'}}};
const tool=(name)=>({name,description:'',parameters:{},async execute(_id,a){return {content:[{type:'text',text:'ran '+name+' '+JSON.stringify(a)}]};}});

test('undeclared tools are registered but refuse to run',async()=>{
 const t=gateTool(tool('mystery_tool'),policy,{check:async()=>{throw new Error('not called');}});
 const r=await t.execute('1',{});
 assert.equal(r.isError,true);assert.match(r.content[0].text,/not declared by any connector manifest/);
});

test('read tools run without a health check',async()=>{
 let calls=0;const t=gateTool(tool('lookup'),policy,{check:async()=>{calls++;return {tainted:true,allowed:false};}});
 assert.match((await t.execute('1',{q:'x'})).content[0].text,/^ran lookup/);assert.equal(calls,0);
});

test('an outbound draft carrying health details to an unlisted recipient is refused; nothing runs',async()=>{
 let seen;const t=gateTool(tool('mail_create_draft'),policy,{check:async a=>{seen=a;return {tainted:true,allowed:false,reason:'carries health details'};}});
 const r=await t.execute('1',{request_id:'req-1',to:['friend@example.com'],subject:'Dr. Rivera',body:'Cardiology on Tuesday'});
 assert.equal(r.isError,true);assert.match(r.content[0].text,/health details/);
 assert.deepEqual(seen.destination,['friend@example.com']);assert.match(seen.text,/Dr\. Rivera/);assert.doesNotMatch(seen.text,/req-1/);
});

test('allowed or untainted outbound tools run',async()=>{
 const ok=gateTool(tool('mail_create_draft'),policy,{check:async()=>({tainted:true,allowed:true})});
 assert.match((await ok.execute('1',{to:['clinic@example.org'],body:'x'})).content[0].text,/^ran/);
 const clean=gateTool(tool('mail_create_draft'),policy,{check:async()=>({tainted:false,allowed:true})});
 assert.match((await clean.execute('1',{to:['a@b.co'],body:'hi'})).content[0].text,/^ran/);
});

test('the check failing refuses the tool (fails closed)',async()=>{
 const t=gateTool(tool('mail_create_draft'),policy,{check:async()=>{throw new Error('socket down');}});
 const r=await t.execute('1',{to:['a@b.co'],body:'x'});
 assert.equal(r.isError,true);assert.match(r.content[0].text,/could not run.*Nothing was sent/);
});

test('own-account changes with no other recipient skip the check; with guests they are checked',async()=>{
 let calls=0;const t=gateTool(tool('calendar_create_event'),policy,{check:async()=>{calls++;return {tainted:true,allowed:false,reason:'no'};}});
 assert.match((await t.execute('1',{title:'Cardiology'})).content[0].text,/^ran/);
 assert.match((await t.execute('1',{title:'Cardiology',guests:[]})).content[0].text,/^ran/);
 assert.equal(calls,0);
 assert.equal((await t.execute('1',{title:'Cardiology',guests:['x@y.co']})).isError,true);assert.equal(calls,1);
});

test('browser steps send the session id as a session destination',async()=>{
 let seen;const t=gateTool(tool('web_task_step'),policy,{check:async a=>{seen=a;return {tainted:false};}});
 await t.execute('1',{id:'abcdef0123456789',steps:[{kind:'fill',ref:'e1',value:'Lisinopril'}]});
 assert.equal(seen.destination_kind,'session');assert.equal(seen.destination,'abcdef0123456789');assert.match(seen.text,/Lisinopril/);assert.doesNotMatch(seen.text,/e1/);
});

test('audit mode lets everything run',async()=>{
 const t=gateTool(tool('mystery'),{...policy,mode:'audit'});
 assert.match((await t.execute('1',{})).content[0].text,/^ran/);
});

test('withConnectorGate wraps tools and tool factories',async()=>{
 const reg=[];const api={registerTool:(t,o)=>reg.push([t,o]),registerCommand(){}};
 const g=withConnectorGate(api,{policy,check:async()=>({tainted:false})});
 g.registerTool(tool('mystery'));g.registerTool(ctx=>ctx.agentId==='main'?tool('lookup'):undefined,{optional:true});
 assert.equal((await reg[0][0].execute('1',{})).isError,true);
 assert.equal(reg[1][0]({agentId:'other'}),undefined);
 assert.match((await reg[1][0]({agentId:'main'}).execute('1',{})).content[0].text,/^ran lookup/);
 assert.equal(reg[1][1].optional,true);
});

test('outboundText skips bookkeeping fields',()=>{
 assert.equal(outboundText({request_id:'r',id:'s',to:['a@b.co'],body:'hello',steps:[{ref:'e3',value:'v'}]}),'a@b.co\nhello\nv');
});

test('the shipped policy declares every tool this plugin registers and copies are identical',()=>{
 const p=loadPolicy();assert.ok(p&&p.tools.web_task_prepare&&p.tools.mail_create_draft&&p.tools.phone_prepare_call);
 assert.equal(p.mode,'enforce');
 for(const dir of ['../mail-plugin/','../voice-plugin/']){
  assert.equal(fs.readFileSync(new URL(dir+'connector-gate.mjs',import.meta.url),'utf8'),fs.readFileSync(new URL('./connector-gate.mjs',import.meta.url),'utf8'));
  assert.deepEqual(JSON.parse(fs.readFileSync(new URL(dir+'connector-policy.json',import.meta.url),'utf8')),p);
 }
});

test('editions: an x-only tool refuses on a standard box and runs on PROCURATOR-X',async()=>{
 const pol={mode:'enforce',tools:{build_prepare:{risk:'draft',edition:'x'}}};
 const prev=process.env.PROCURATOR_EDITION;
 try{
  process.env.PROCURATOR_EDITION='standard';
  const r=await gateTool(tool('build_prepare'),pol,{check:async()=>{throw new Error('not called');}}).execute('1',{});
  assert.equal(r.isError,true);assert.match(r.content[0].text,/PROCURATOR-X/);
  process.env.PROCURATOR_EDITION='x';
  assert.match((await gateTool(tool('build_prepare'),pol,{check:async()=>{throw new Error('not called');}}).execute('1',{})).content[0].text,/^ran build_prepare/);
 }finally{if(prev===undefined)delete process.env.PROCURATOR_EDITION;else process.env.PROCURATOR_EDITION=prev;}
});
