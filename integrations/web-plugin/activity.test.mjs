import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {registerActivity,parseActivityArgs,collectUpcoming} from './activity.mjs';

function fixture(responses={}){
 const calls=[],tools=[],commands=[];
 const call=async(action,args,privileged=false)=>{calls.push([action,args,privileged]);const r=responses[action];if(r instanceof Error)throw r;return typeof r==='function'?r(args):r??{};};
 registerActivity({registerTool:(f,o)=>tools.push([f,o]),registerCommand:c=>commands.push(c)},call,()=>{},{collect:async()=>[{group:'Scheduled',kind:'other',what:'x'}],setTimer:()=>null});
 return {calls,tools,commands};
}

test('activity tools are main-agent only, unprivileged, and cannot write system records',async()=>{
 const f=fixture({activity_report:{saved:true}});
 const names=f.tools.map(([,o])=>o.name);
 assert.deepEqual(names,['activity_search','activity_get','activity_upcoming','activity_report']);
 assert.ok(!names.includes('activity_record'));
 const rep=f.tools.find(([,o])=>o.name==='activity_report')[0];
 assert.equal(rep({agentId:'phone'}),undefined);
 await rep({agentId:'main'}).execute('1',{request_id:'cvs-flu-01',title:'Booked',details:'x'});
 assert.deepEqual(f.calls[0],['activity_report',{request_id:'cvs-flu-01',title:'Booked',details:'x'},false]);
});

test('/activity parses plain words and uses the owner socket',async()=>{
 assert.deepEqual(parseActivityArgs(''),{days:3});
 assert.deepEqual(parseActivityArgs('days 7 calls'),{days:7,kind:'call'});
 assert.deepEqual(parseActivityArgs('#12'),{id:12});
 assert.deepEqual(parseActivityArgs('emails'),{days:7,kind:'email'});
 assert.deepEqual(parseActivityArgs('page'),{page:true});
 assert.ok(parseActivityArgs('days 999').error);
 const f=fixture({activity_owner:a=>a.page?{sent:true}:{text:'Activity, last 3 days'}});
 const cmd=f.commands[0];
 assert.equal((await cmd.handler({args:''})).text,'Activity, last 3 days');
 assert.deepEqual(f.calls.at(-1),['activity_owner',{days:3},true]);
 assert.match((await cmd.handler({args:'page'})).text,/link .* is above/);
 assert.match((await cmd.handler({args:'what'})).text,/Try: \/activity/);
 assert.ok(!/`/.test((await cmd.handler({args:'what'})).text));
});

test('/activity upcoming pushes gateway items first, then asks the broker',async()=>{
 const f=fixture({activity_owner:{text:'Waiting for you:'}});
 assert.deepEqual(parseActivityArgs('pending'),{upcoming:true});
 await f.commands[0].handler({args:'upcoming'});
 assert.deepEqual(f.calls.map(c=>c[0]),['activity_upcoming_push','activity_owner']);
 assert.deepEqual(f.calls[1],['activity_owner',{upcoming:true},true]);
});
test('gateway items: mail approvals, calls waiting or running, reminders',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'upcoming-'));const file=path.join(dir,'tests.json');const now=1_800_000_000_000;
 fs.writeFileSync(file,JSON.stringify({records:[{id:'a1',status:'pending',expiresAt:now+300000,plan:{to:'+17065550123',task:'Ask about the grievance'}},
  {id:'a2',status:'initiated',attemptedAt:now-60000,plan:{to:'+17065550124',task:'Book a table'}},{id:'a3',status:'call_ended',attemptedAt:now-60000,plan:{to:'+1',task:'x'}}]}));
 const items=await collectUpcoming({mail:async()=>({pending:[{kind:'email',what:'Email to a@b.c — "Hi"',command:'/mailsend 0123456789abcdef',expires_in_min:12}]}),voiceFile:file,now});
 assert.deepEqual(items.map(x=>[x.group,x.kind]),[['Waiting for you','email'],['Waiting for you','call'],['In progress','call']]);
 assert.match(items[0].how,/Send the \/mailsend line from its review message \(expires in 12 min\)/);
 assert.ok(!JSON.stringify(items).includes('0123456789abcdef'),'no draft IDs or codes beyond the command name');
 fs.rmSync(dir,{recursive:true,force:true});
});
