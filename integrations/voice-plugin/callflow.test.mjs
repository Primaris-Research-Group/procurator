import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {classifyCall,lineKind,learnedKeypad,MenuMemory,CALL_LOGIC} from './callflow.mjs';
import {formatCallStatus} from './presentation.mjs';
const T=1_800_000_000_000,s=n=>T+n*1000;
const u=(at,text)=>({speaker:'user',text,timestamp:s(at),isFinal:true}),b=(at,text)=>({speaker:'bot',text,timestamp:s(at),isFinal:true});

test('line kinds: menus, hold queues, voicemail, closed offices and people',()=>{
 assert.equal(lineKind('Thank you for calling. For billing, press 1. Para español, oprima dos.'),'menu');
 assert.equal(lineKind('In a few words, tell me why you are calling.'),'menu');
 assert.equal(lineKind('All of our representatives are assisting other callers. Your call is important to us.'),'hold');
 assert.equal(lineKind('Please leave a message after the tone.'),'voicemail');
 assert.equal(lineKind('Our office is currently closed. Our business hours are 8 to 5.'),'closed');
 assert.equal(lineKind('Hi, this is Maria, how can I help you?'),'other');
});

test('IVR, keys, long hold, then a person: path, hold time and the learned key path',()=>{
 const call={state:'completed',answeredAt:s(0),endedAt:s(900),transcript:[u(1,'Thank you for calling Acme Insurance. For claims, press 2. For all other questions, press 0.'),
  u(40,'Please hold. Your call is important to us.'),u(400,'Thank you for holding, all representatives are still busy.'),
  u(760,'Claims, this is Maria speaking, how can I help you?'),b(762,'Hello, I am the owner’s AI assistant calling about claim status.'),u(770,'Sure, what is the claim number?')]};
 const r={keypresses:[{digits:'2',option:'Claims',at:s(12)}]};
 const out=classifyCall(call,r);
 assert.equal(out.reached,'person');assert.equal(out.hold_seconds,720);
 assert.deepEqual(out.path,['phone menu','pressed 2 (Claims)','on hold ~12 min','a person']);
 assert.deepEqual(learnedKeypad(call,r),[{wait_seconds:10,press:'2',why:'Claims'}]);
});

test('menu only, hold only, voicemail, closed, no answer and a plain person call',()=>{
 const base={state:'completed',answeredAt:s(0),endedAt:s(120)};
 assert.equal(classifyCall({...base,transcript:[u(1,'For billing press 1, for sales press 2.'),u(20,'I did not understand. For billing press 1.')]},{keypresses:[]}).reached,'menu_only');
 assert.equal(classifyCall({...base,transcript:[u(1,'Please hold for the next available agent.')]},{}).reached,'hold_only');
 assert.equal(classifyCall({...base,transcript:[u(1,'You have reached Dr. Lee. Please leave a message after the tone.'),b(8,'Hello, this is the owner’s AI assistant.')]},{}).reached,'voicemail');
 assert.equal(classifyCall({...base,transcript:[u(1,'Our office is closed. Business hours are 9 to 5.')]},{}).reached,'closed');
 assert.equal(classifyCall({state:'no-answer',endedAt:s(30),transcript:[]},{}).reached,'no_answer');
 assert.equal(classifyCall({state:'busy',endedAt:s(3),transcript:[]},{}).reached,'busy');
 assert.equal(classifyCall({...base,transcript:[u(1,'Smith Dental.'),b(2,'Hello, I am an AI assistant.'),u(5,'Yes, we have Tuesday at 3.')]},{}).reached,'person');
 // A greeting with no reply after the assistant spoke is not counted as a person.
 assert.equal(classifyCall({...base,transcript:[u(1,'Thank you for calling Smith Dental.'),b(3,'Hello?')]},{}).reached,'unclear');
});

test('menu memory keeps the newest working key path per number',t=>{
 const d=fs.mkdtempSync(path.join(os.tmpdir(),'menus-'));t.after(()=>fs.rmSync(d,{recursive:true,force:true}));
 const m=new MenuMemory(d,()=>T);
 assert.equal(m.get('+17065550123'),null);
 assert.ok(m.learn('+17065550123',[{wait_seconds:8,press:'2',why:'Claims'}],'abc'));
 assert.equal(m.learn('+17065550123',[{wait_seconds:8,press:'2'}],'abc'),false,'same call twice');
 assert.deepEqual(m.get('+17065550123').keypad,[{wait_seconds:8,press:'2',why:'Claims'}]);
 assert.equal(m.learn('bad',[{press:'1'}],'x'),false);
});

test('status text shows who was reached in plain words',()=>{
 const text=formatCallStatus({id:'abc123',status:'call_ended',to:'+17065550123',call:{found:true,call:{state:'completed',answeredAt:T,endedAt:s(300)}},report:{status:'delivered'},
  reached:{reached:'hold_only',summary:'Waited on hold; no person picked up before the call ended.',path:['phone menu','pressed 0 (operator)','on hold ~5 min']}});
 assert.match(text,/Reached: Waited on hold; no person picked up before the call ended \(phone menu → pressed 0 \(operator\) → on hold ~5 min\) — estimated from the transcript/);
 assert.match(text,/Nothing is retried automatically/);
 assert.ok(!/`/.test(text));
 assert.match(CALL_LOGIC,/Decline automated callback offers/);assert.match(CALL_LOGIC,/say only "Sure, thank you." once/);assert.match(CALL_LOGIC,/Give the opening once per person/);
});
