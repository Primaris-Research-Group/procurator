import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerCoach,parseCoach,COACH_TOOLS} from './coach.mjs';

const owner={channel:'telegram',isAuthorizedSender:true,senderId:'100000001',senderIsOwner:true,assertOwnerCurrent(){}};
const ownerContext=ctx=>{if(ctx.senderId!=='100000001')throw new Error('Only the authenticated Telegram owner.');};

test('/coach parsing',()=>{
 assert.deepEqual(parseCoach('on'),{set:'on'});assert.deepEqual(parseCoach(''),{set:'status'});
 assert.deepEqual(parseCoach('pause 3'),{set:'pause',days:3});assert.equal(parseCoach('pause 90'),null);
 assert.deepEqual(parseCoach('meals 19:00'),{set:'reminder',kind:'meals',time:'19:00'});assert.deepEqual(parseCoach('sleep off'),{set:'reminder',kind:'sleep',time:'off'});
 assert.equal(parseCoach('meals soon'),null);assert.equal(parseCoach('delete everything'),null);
});

test('tools are on the model socket with closed schemas; settings and the timer use the owner socket',async()=>{
 const tools=[],commands=[],calls=[],sent=[],wakes=[];let tick;
 const call=async(a,args,p)=>{calls.push([a,args,p]);if(a==='health_coach_due')return {send:[{kind:'meals',text:'Coach: time to log today\'s meals'}]};if(a==='health_coach_settings')return {enabled:true,questionnaire_done:false};return {};};
 registerCoach({registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)},call,ownerContext,{gateway:async(m,p)=>sent.push(p),wake:async(t)=>wakes.push(t),setTimer:(f)=>{tick=f;return {unref(){}};}});
 assert.deepEqual(tools.map(t=>t.name),COACH_TOOLS.map(t=>t[0]));
 for(const t of tools){assert.equal(t.parameters.additionalProperties,false);await t.execute('1',{});}
 assert.ok(calls.every(([a,,p])=>p===undefined&&!['health_coach_settings','health_coach_due'].includes(a)));
 const ans=tools.find(t=>t.name==='health_coach_answers').parameters.properties;
 assert.equal(ans.reminders.additionalProperties,false);assert.ok(ans.reminders.properties.meals);
 const coach=commands.find(c=>c.name==='coach');
 assert.match((await coach.handler({...owner,senderId:'intruder',args:'on'})).text,/Only/);
 assert.match((await coach.handler({...owner,args:'on'})).text,/quick questions/);
 assert.deepEqual(calls.at(-1),['health_coach_settings',{set:'on'},true]);assert.match(wakes[0],/health_coach_questionnaire/);
 await tick();await new Promise(r=>setImmediate(r));
 assert.deepEqual(calls.at(-1),['health_coach_due',{},true]);
 assert.equal(sent[0].message,'Coach: time to log today\'s meals');assert.match(sent[0].idempotencyKey,/^procurator-coach:meals:\d{4}-\d{2}-\d{2}$/);
});
