// 0.24.3 (B01): the gateway closes any tool-schema object that lists no properties ("constraints?: {}"), so a nested
// open object silently refuses every field the model sends. This walks every tool this plugin registers and fails on
// one, then checks a real health grant against the closed (runtime-normalized) form of its schema.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {TOOLS as HEALTH,CONSTRAINT_KEYS} from './health.mjs';

const dir=path.dirname(fileURLToPath(import.meta.url));
const noop=new Proxy(function(){},{get:(t,k)=>k==='then'?undefined:noop,apply:()=>noop});
async function allTools(){
 const tools=[];
 for(const f of fs.readdirSync(dir).filter(f=>f.endsWith('.mjs')&&!f.endsWith('.test.mjs')&&f!=='index.mjs')){
  const m=await import(path.join(dir,f));
  for(const [k,fn] of Object.entries(m)){
   if(!/^register/.test(k)||typeof fn!=='function')continue;
   const api=new Proxy({registerTool:t=>{const x=typeof t==='function'?t({agentId:'main',assertInvocationCurrent(){}}):t;if(x)tools.push({file:f,...x});}},{get:(o,p)=>p in o?o[p]:noop});
   try{fn(api,async()=>({}),noop,noop,noop);}catch{/* registrars needing real services are covered by their own tests */}
  }
 }
 return tools;
}
function openObjects(s,where,out=[],top=true){
 if(!s||typeof s!=='object')return out;
 const isObj=s.type==='object'||(Array.isArray(s.type)&&s.type.includes('object'));
 if(isObj&&!top&&!(s.properties&&Object.keys(s.properties).length))out.push(where);
 for(const [k,v] of Object.entries(s.properties||{}))openObjects(v,where+'.'+k,out,false);
 if(s.items)openObjects(s.items,where+'[]',out,false);
 for(const k of ['anyOf','oneOf','allOf'])(s[k]||[]).forEach((x,i)=>openObjects(x,where+'|'+i,out,false));
 return out;
}
// What the gateway does to a schema: every object becomes closed; an object without properties accepts nothing.
function closed(s){
 if(!s||typeof s!=='object')return s;
 const o={...s};
 if(o.type==='object'){o.properties=Object.fromEntries(Object.entries(o.properties||{}).map(([k,v])=>[k,closed(v)]));o.additionalProperties=false;}
 if(o.items)o.items=closed(o.items);
 return o;
}
function validate(s,v,where='$'){
 const errs=[];const t=Array.isArray(s.type)?s.type:[s.type];
 const kind=Array.isArray(v)?'array':v===null?'null':Number.isInteger(v)?'integer':typeof v;
 if(s.type&&!t.some(x=>x===kind||(x==='number'&&kind==='integer')))return [`${where}: ${kind} is not ${t}`];
 if(s.enum&&!s.enum.includes(v))errs.push(`${where}: ${v} not in enum`);
 if(kind==='object'){for(const r of s.required||[])if(!(r in v))errs.push(`${where}.${r}: required`);
  for(const [k,x] of Object.entries(v)){if(s.properties?.[k])errs.push(...validate(s.properties[k],x,where+'.'+k));else if(s.additionalProperties===false)errs.push(`${where}.${k}: not allowed`);}}
 if(kind==='array'&&s.items)v.forEach((x,i)=>errs.push(...validate(s.items,x,`${where}[${i}]`)));
 return errs;
}

test('no tool schema has a nested object without listed properties',async()=>{
 const tools=await allTools();
 assert.ok(tools.length>=100,'found '+tools.length+' tools');
 const bad=tools.flatMap(t=>openObjects(t.parameters,t.file+':'+t.name));
 assert.deepEqual(bad,[]);
});

test('a bounded weekday-morning booking grant survives the closed runtime schema',()=>{
 const propose=HEALTH.find(t=>t[0]==='health_grant_propose')[3];
 const grant={connection:'2ad0366c91b834ac',title:'Follow-up with Dr. Wei Chen',actions:['appointment.search','appointment.book','appointment.reschedule'],
  constraints:{clinician:'pr-chen',provider:'Riverside',specialty:'Internal Medicine',visit_type:'follow-up',locations:['Midtown'],weekdays:['Monday','Tuesday','Wednesday','Thursday','Friday'],
   time_of_day:'morning',time_windows:[{from:'2026-11-09',to:'2026-12-31'}],spend_cap_usd:0,cancellation_policy:'no_fee_only',reschedule_order:'book_then_cancel'},
  disclosures:[],valid_days:1};
 assert.deepEqual(validate(closed(propose),grant),[]);
 assert.deepEqual(validate(closed(propose),{...grant,constraints:{clinicain:'pr-chen'}}),['$.constraints.clinicain: not allowed']);
 assert.deepEqual(Object.keys(propose.properties.constraints.properties).sort(),[...CONSTRAINT_KEYS].sort());
 const refill={connection:'x',title:'Refill',actions:['rx.status','rx.refill'],constraints:{prescriptions:['rx-1001'],pharmacy:'st-12',fulfillment:['pickup'],spend_cap_usd:15}};
 assert.deepEqual(validate(closed(propose),refill),[]);
});

test('effect payloads and nutrition items survive the closed runtime schema',()=>{
 const begin=HEALTH.find(t=>t[0]==='health_effect_begin')[3];
 const book={request_id:'chen-book-2026-11-16',action:'appointment.book',connection:'c',grant:'g',payload:{slot_id:'slot-1',date:'2026-11-16',time:'09:00',clinician:'Dr. Wei Chen',
  location:'Midtown',weekday:'Monday',time_of_day:'morning',visit_type:'follow-up',cost_usd:0}};
 assert.deepEqual(validate(closed(begin),book),[]);
 const log=HEALTH.find(t=>t[0]==='health_nutrition_log')[3];
 assert.deepEqual(validate(closed(log),{entry_key:'meal-1',items:[{fdc_id:1,quantity:1,unit:'cup'},{name:'bar',quantity:1,unit:'serving',nutrients:{energy_kcal:200,protein_g:10},basis:'label'}]}),[]);
});

test('python broker and plugin agree on the grant limits',()=>{
 const py=fs.readFileSync(path.join(dir,'../web/health.py'),'utf8').match(/CONSTRAINT_KEYS = \(([^)]*)\)/)[1];
 assert.deepEqual([...py.matchAll(/'([a-z_]+)'/g)].map(m=>m[1]).sort(),[...CONSTRAINT_KEYS].sort());
});
