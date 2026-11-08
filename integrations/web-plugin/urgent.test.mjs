import {test} from 'node:test';
import assert from 'node:assert/strict';
import {urgentMatch,lastOwnerText,registerUrgentCare,NOTICE} from './urgent.mjs';

test('first-person emergencies match',()=>{
 for(const t of ['I have crushing chest pain right now','my chest hurts and my left arm is numb','I can\'t breathe','My wife passed out in the kitchen',
  'I think I took too many pills','he\'s having a seizure','I want to die','I am going to kill myself','my throat is closing after eating shrimp','My dad\'s face is drooping'])
  assert.equal(urgentMatch(t),true,t);
});

test('clinical work, logistics and other people\'s text do not match',()=>{
 for(const t of ['Calculate the HEART score for a 58 year old patient with chest pain','Find evidence on troponin in chest pain workup',
  'Book my follow-up with cardiology next week','PHQ-9 suicide risk assessment for my patients','Summarize the article on stroke symptoms in young adults',
  '','Schedule my mammogram'])
  assert.equal(urgentMatch(t),false,t);
});

test('reads the owner text from prompt or the last user message',()=>{
 assert.equal(lastOwnerText({prompt:'hi'}),'hi');
 assert.equal(lastOwnerText({messages:[{role:'user',content:'a'},{role:'assistant',content:'b'},{role:'user',content:[{type:'text',text:'c'}]}]}),'c');
});

test('sends the fixed notice once per message and tells the assistant to stop health tasks',async()=>{
 const sent=[];let hook;const api={on:(n,f)=>{if(n==='before_prompt_build')hook=f;}};
 registerUrgentCare(api,{gateway:async(m,p)=>{sent.push(p);}});
 const ctx={agentId:'main',sessionKey:'agent:main:main'};
 const r=await hook({prompt:'I have chest pain and feel faint'},ctx);
 assert.equal(sent.length,1);assert.equal(sent[0].message,NOTICE);assert.match(r.appendContext,/do not assess symptoms/);
 await hook({prompt:'I have chest pain and feel faint'},ctx);assert.equal(sent.length,1);
 assert.equal(await hook({prompt:'book a dentist'},ctx),undefined);
 assert.equal(await hook({prompt:'I have chest pain'},{agentId:'main',sessionKey:'agent:main:mission-abc'}),undefined);
 assert.equal(await hook({prompt:'I have chest pain'},{agentId:'phone',sessionKey:'agent:phone:voice:1'}),undefined);
});
