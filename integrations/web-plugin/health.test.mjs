import test from 'node:test';
import assert from 'node:assert/strict';
import {registerHealth,usedHealthTools,TOOLS} from './health.mjs';
import {registerRecall} from './recall.mjs';

function fakeApi(){const hooks={},tools=[],commands=[];return {hooks,tools,commands,on:(n,f,o)=>{(hooks[n]??=[]).push({f,o});},registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)};}
const owner=ctx=>{if(!ctx.owner)throw new Error('Only the authenticated Telegram owner can approve browser actions.');};
const text=r=>JSON.parse(r.content[0].text);

test('0.21.0: the model gets the health tools; grants and policy are owner commands over the privileged socket',async()=>{
 const api=fakeApi(),calls=[],sent=[],woken=[];
 const call=async(a,args,p)=>{calls.push([a,args,p]);
  if(a==='health_grant_approve')return {id:'g1',revision:1,title:'Derm',actions:['appointment.book'],valid_to:1800000000};
  if(a==='health_receipt')return {receipt:'rc1',token:'TOKEN-SECRET',expires_days:7};
  if(a==='health_portal_authorize')return {authorize_url:'https://portal.example/authorize?x=1',expires_minutes:10};
  if(a==='health_status')return {grants:[],connections:[],open_effects:[],policy:{recall:'off',notify_detail:'minimal'}};
  if(a==='health_policy')return {recall:'off',notify_detail:'minimal',retention_days:365,caregivers:'off',cloud_models:'default'};
  return {ok:true};};
 const gateway=async(m,params)=>{sent.push([m,params]);return {};};
 registerHealth(api,call,owner,{gateway,wake:async(t,k)=>woken.push([t,k])});
 assert.deepEqual(api.tools.map(t=>t.name),TOOLS.map(t=>t[0]));
 assert.deepEqual(api.commands.map(c=>c.name),['healthgrant','healthauth','healthrevoke','healthconnect','healthpolicy','healthadult','healthread','healthconfirm','healthgoal','healthpair','healthdevices','healthstatus']);
 // /healthauth goes over the privileged socket and returns the sign-in link to them only.
 calls.length=0;
 const auth=await api.commands.find(c=>c.name==='healthauth').handler({args:'0123456789abcdef',owner:true});
 assert.deepEqual(calls[0],['health_portal_authorize',{connection:'0123456789abcdef'},true]);
 // Tools call the public socket (never privileged) and the receipt token never reaches the model.
 await api.tools.find(t=>t.name==='health_effect_begin').execute('x',{request_id:'r-00000001',action:'appointment.book',connection:'c',grant:'g',payload:{a:1}});
 assert.deepEqual(calls.at(-1),['health_effect_begin',{request_id:'r-00000001',action:'appointment.book',connection:'c',grant:'g',payload:{a:1}},undefined]);
 const r=text(await api.tools.find(t=>t.name==='health_receipt').execute('x',{effect:'e1',body:'Derm visit Tue 2:30'}));
 assert.equal(r.receipt,'rc1');assert.equal(JSON.stringify(r).includes('TOKEN-SECRET'),false);
 assert.match(sent[0][1].message,/dashboard\/health\/receipt\/TOKEN-SECRET/);assert.equal(sent[0][1].to,'100000001');
 // /healthgrant needs the owner; approval goes over the privileged socket and wakes the assistant.
 const grant=api.commands.find(c=>c.name==='healthgrant');
 assert.match((await grant.handler({args:'p1 abcdefabcdefabcd',owner:false})).text,/Only the authenticated/);
 const ok=await grant.handler({args:'p1 abcdefabcdefabcd',owner:true});
 assert.match(ok.text,/Health grant g1 approved/);assert.deepEqual(calls.at(-1),['health_grant_approve',{proposal:'p1',hash:'abcdefabcdefabcd'},true]);
 assert.match(woken[0][0],/HEALTH GRANT APPROVED — g1/);
 assert.match((await api.commands.find(c=>c.name==='healthpolicy').handler({args:'recall on',owner:true})).text,/Health policy/);
 assert.deepEqual(calls.at(-1),['health_policy',{set:{recall:'on'}},true]);
 assert.match((await api.commands.find(c=>c.name==='healthconnect').handler({args:'clinic_web "Midtown Dermatology" https://book.example.org production',owner:true})).text,/set-health-token/);
 assert.deepEqual(calls.at(-1)[1],{provider:'clinic_web',institution:'Midtown Dermatology',endpoint:'https://book.example.org',environment:'production'});
});

test('0.21.0: a turn that used a health tool is kept out of memory unless the policy says recall on',async()=>{
 assert.equal(usedHealthTools({messages:[{role:'assistant',content:[{type:'tool_use',name:'health_effect_begin',input:{}}]}]}),true);
 assert.equal(usedHealthTools({messages:[{role:'assistant',content:[{type:'tool_use',name:'web_browser_open'}]},{role:'assistant',content:'x'}]}),false);
 assert.equal(usedHealthTools({messages:[{role:'assistant',toolCalls:[{function:{name:'health_status'}}]}]}),true);
 const api=fakeApi(),calls=[];let policy='off';
 const call=async(a,args,p)=>{calls.push([a,args,p]);if(a==='recall_context')return {text:'MEMORY'};if(a==='health_policy')return {recall:policy};return {};};
 registerRecall(api,call,()=>{},{setTimer:()=>null,exclude:usedHealthTools});await new Promise(r=>setImmediate(r));
 const ctx={agentId:'main',sessionKey:'agent:main:telegram:direct:100000001'};
 api.hooks.agent_end[0].f({runId:'h1',messages:[{role:'user',content:'book my derm visit'},{role:'assistant',content:[{type:'tool_use',name:'health_effect_begin'}]},{role:'assistant',content:'Booked; receipt sent.'}]},ctx);
 api.hooks.agent_end[0].f({runId:'n1',messages:[{role:'user',content:'weather?'},{role:'assistant',content:'72 F'}]},ctx);
 await new Promise(r=>setImmediate(r));
 assert.deepEqual(calls.filter(c=>c[0]==='recall_turn').map(c=>c[1].run_id),['n1']);
});

test('0.24.3 (B02): a health call refused at validation, a namespaced name or a tool_call result still keeps the turn out of memory',async()=>{
 const refused='Invalid arguments for tool "openclaw:procurator-web:health_grant_propose": constraints: must not have additional properties: "visit_type".';
 // The live gateway shape: no assistant tool-call part; the refusal arrives as a custom message with a toolResult part.
 assert.equal(usedHealthTools({messages:[{role:'user',content:'Propose a grant for weekday mornings'},{role:'custom',content:[{type:'toolResult',toolName:'tool_call',isError:true,content:[{type:'text',text:refused}]}]},{role:'assistant',content:'The grant could not be proposed.'}]}),true);
 assert.equal(usedHealthTools({messages:[{role:'user',content:'x'},{role:'tool',content:refused}]}),true);
 assert.equal(usedHealthTools({messages:[{role:'user',content:'x'},{role:'assistant',content:[{type:'toolCall',name:'openclaw:procurator-web:health_status',arguments:{}}]}]}),true);
 assert.equal(usedHealthTools({messages:[{role:'user',content:'x'},{role:'toolResult',toolName:'health_grant_propose',content:'{"error":"x"}'}]}),true);
 // Only the current turn counts; another tool's result that merely mentions a health tool does not.
 assert.equal(usedHealthTools({messages:[{role:'user',content:'a'},{role:'assistant',content:[{type:'tool_use',name:'health_status'}]},{role:'user',content:'weather?'},{role:'assistant',content:'72 F'}]}),false);
 assert.equal(usedHealthTools({messages:[{role:'user',content:'what can you do?'},{role:'custom',content:[{type:'toolResult',toolName:'assistant_capabilities',content:[{type:'text',text:'Health: use health_status then health_grant_propose.'}]}]}]}),false);
 // Through the hook: the refused turn is not recorded.
 const api=fakeApi(),calls=[];
 const call=async(a,args,p)=>{calls.push([a,args,p]);if(a==='recall_context')return {text:''};if(a==='health_policy')return {recall:'off'};return {};};
 registerRecall(api,call,()=>{},{setTimer:()=>null,exclude:usedHealthTools});await new Promise(r=>setImmediate(r));
 const ctx={agentId:'main',sessionKey:'agent:main:main'};
 api.hooks.agent_end[0].f({runId:'r120',messages:[{role:'user',content:'For the synthetic Riverside connection, propose a grant'},{role:'custom',content:[{type:'toolResult',toolName:'tool_call',isError:true,content:[{type:'text',text:refused}]}]},{role:'assistant',content:'It was refused.'}]},ctx);
 await new Promise(r=>setImmediate(r));
 assert.deepEqual(calls.filter(c=>c[0]==='recall_turn'),[]);
});

test('0.23.0: confirm, goal, pairing and device commands are owner-only and privileged; the new tools are public',async()=>{
 const api=fakeApi(),calls=[],woken=[];
 const call=async(a,args,p)=>{calls.push([a,args,p]);
  if(a==='health_effect_owner_confirm')return {id:args.effect,action:'appointment.book',state:'confirmed',external_id:args.external_id};
  if(a==='health_nutrition_goal')return args.confirm?{goal:'g1',kcal:2400,basis:'own'}:{goal:null};
  if(a==='health_sync_pair_code')return {code:'ABCD-EFGH',server:'https://assistant.example.com/assistant/dashboard',types:args.types||['body_mass'],expires_minutes:10};
  if(a==='health_sync_devices')return {devices:[{id:'d1',name:'iPhone',status:'active',types:['body_mass'],last_sync_hours_ago:1.5,stale:false}]};
  if(a==='health_sync_revoke')return {device:args.device,revoked:true,deleted_records:3};
  return {ok:true};};
 registerHealth(api,call,owner,{wake:async(t,k)=>woken.push([t,k])});
 const cmd=n=>api.commands.find(c=>c.name===n);
 assert.match((await cmd('healthconfirm').handler({args:'0123456789abcdef RIV-1',owner:false})).text,/Only the authenticated/);
 assert.match((await cmd('healthconfirm').handler({args:'0123456789abcdef RIV-1',owner:true})).text,/confirmed by you \(RIV-1\)/);
 assert.deepEqual(calls.at(-1),['health_effect_owner_confirm',{effect:'0123456789abcdef',external_id:'RIV-1'},true]);
 assert.match(woken[0][0],/health_calendar_plan/);
 assert.match((await cmd('healthgoal').handler({args:'aaaabbbbccccdddd 0123456789',owner:true})).text,/2400 kcal/);
 assert.deepEqual(calls.at(-1),['health_nutrition_goal',{confirm:true,proposal:'aaaabbbbccccdddd',hash:'0123456789'},true]);
 assert.match((await cmd('healthpair').handler({args:'body_mass step_count',owner:true})).text,/Code: ABCD-EFGH/);
 assert.deepEqual(calls.at(-1),['health_sync_pair_code',{types:['body_mass','step_count']},true]);
 assert.match((await cmd('healthdevices').handler({args:'',owner:true})).text,/d1 iPhone — active/);
 assert.match((await cmd('healthdevices').handler({args:'revoke d1',owner:true})).text,/3 record/);
 assert.deepEqual(calls.at(-1),['health_sync_revoke',{device:'d1',keep_data:false},true]);   // 0.25.0 (H4): disconnecting deletes by default
 assert.match((await cmd('healthdevices').handler({args:'revoke d1 keep',owner:true})).text,/kept/);
 assert.deepEqual(calls.at(-1),['health_sync_revoke',{device:'d1',keep_data:true},true]);
 for(const n of ['health_portal_cancel','health_calendar_plan','health_nutrition_log','health_pharmacy_refill','health_sync_activity']){
  await api.tools.find(t=>t.name===n).execute('x',{effect:'e1'});assert.equal(calls.at(-1)[2],undefined);
 }
 // The goal tool cannot confirm: its schema has no confirm field.
 assert.equal('confirm' in api.tools.find(t=>t.name==='health_nutrition_goal').parameters.properties,false);
});
