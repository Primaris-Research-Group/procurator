import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerMail,ownerContext} from './register.mjs';
const owner={channel:'telegram',senderId:'100000001',isAuthorizedSender:true};
test('owner native context checks reject other users and non-Telegram channels',()=>{
  ownerContext(owner);
  for(const ctx of [{...owner,senderId:'123'},{...owner,channel:'webchat'},{...owner,isAuthorizedSender:false},{...owner,senderIsOwner:false}]) assert.throws(()=>ownerContext(ctx));
});
test('model tools have no approval or send action; only trusted native command can send',async()=>{
  const tools=[],commands=[],calls=[];
  registerMail({registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)},async(...args)=>{calls.push(args);return {id:'a'.repeat(16),status:'sent',receipt:'fake'};});
  assert.equal(tools.length,30);   // 0.25.0: + contacts_search and four sms_* tools
  for(const t of tools) await t.execute('fake',{});
  assert.ok(calls.every(([a,,p])=>a!=='send'&&a!=='trash'&&a!=='sms_send'&&a!=='assistant_address_set'&&!p));
  const send=commands.find(c=>c.name==='mailsend');
  const args='a'.repeat(16)+' '+'b'.repeat(16);
  await send.handler({...owner,senderId:'attacker',args});
  assert.equal(calls.length,30);
  await send.handler({...owner,args});
  assert.deepEqual(calls.at(-1),['send',{id:'a'.repeat(16),hash:'b'.repeat(16)},true]);
  const sms=commands.find(c=>c.name==='smssend');
  await sms.handler({...owner,senderId:'attacker',args});assert.notEqual(calls.at(-1)[0],'sms_send');
  await sms.handler({...owner,args});assert.deepEqual(calls.at(-1),['sms_send',{id:'a'.repeat(16),hash:'b'.repeat(16)},true]);
});
test('only the owner native command can move mail to Trash, with the exact review id and code',async()=>{
  const tools=[],commands=[],calls=[];
  registerMail({registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)},async(...args)=>{calls.push(args);return {id:'c'.repeat(16),status:'done',account:'icloud',result:{changed:['1','2']}};});
  const trash=commands.find(c=>c.name==='mailtrash');const args='c'.repeat(16)+' '+'d'.repeat(16);
  assert.match((await trash.handler({...owner,senderId:'attacker',args})).text,/Only the authenticated/);
  assert.match((await trash.handler({...owner,args:'c'.repeat(16)})).text,/complete \/mailtrash/);assert.equal(calls.length,0);
  assert.match((await trash.handler({...owner,args})).text,/Moved 2 message\(s\) to Trash in iCloud/);
  assert.deepEqual(calls.at(-1),['trash',{id:'c'.repeat(16),hash:'d'.repeat(16)},true]);
  const request=tools.find(t=>t.name==='mail_request_trash');assert.ok(!('action' in request.parameters.properties));
  const manage=tools.find(t=>t.name==='mail_manage');assert.deepEqual(manage.parameters.properties.action.enum,['mark_read','mark_unread','junk','not_junk']);
});

test('calendar approval is a native owner command; model tools cannot approve',async()=>{
  const tools=[],commands={},calls=[];
  registerMail({registerTool:t=>tools.push(t),registerCommand:c=>commands[c.name]=c},async(...args)=>{calls.push(args);return {id:'b'.repeat(16),op:'create',status:'done',result:{summary:'Lunch',start:'2026-11-14T12:00:00-05:00'}};});
  assert.ok(tools.some(t=>t.name==='calendar_create_event'));assert.ok(tools.some(t=>t.name==="calendar_add_reminder"));assert.ok(!tools.some(t=>/approve/.test(t.name)));
  const r=await commands.calapprove.handler({...owner,args:'b'.repeat(16)+' '+'c'.repeat(16)});
  assert.match(r.text,/Done: Lunch/);assert.deepEqual(calls[0],['cal_approve',{id:'b'.repeat(16),hash:'c'.repeat(16)},true]);
  const denied=await commands.calapprove.handler({...owner,senderId:'1',args:'b'.repeat(16)+' '+'c'.repeat(16)});
  assert.doesNotMatch(denied.text,/Done/);assert.equal(calls.length,1);
});
test('/assistantemail is owner-only and uses the approval socket',async()=>{
  const commands=[],calls=[];
  registerMail({registerTool(){},registerCommand:c=>commands.push(c)},async(...args)=>{calls.push(args);return {address:'owner+assistant@example.com',send:true};});
  const cmd=commands.find(c=>c.name==='assistantemail');
  assert.match((await cmd.handler({...owner,senderId:'attacker',args:'x@y.z'})).text,/Only the authenticated/);
  assert.equal(calls.length,0);
  const r=await cmd.handler({...owner,args:'owner+assistant@example.com'});
  assert.deepEqual(calls[0],['assistant_address_set',{address:'owner+assistant@example.com',send_as:false},true]);
  assert.match(r.text,/can also send/);assert.ok(!/`/.test(r.text));
});
test('0.24.3 (B17): a replayed /mailsend says already sent and sends nothing new',async()=>{
  const commands=[];
  registerMail({registerTool(){},registerCommand:c=>commands.push(c)},async()=>({id:'a'.repeat(16),status:'sent',receipt:'r1',already:true}));
  const r=await commands.find(c=>c.name==='mailsend').handler({...owner,args:'a'.repeat(16)+' '+'b'.repeat(16)});
  assert.match(r.text,/^Already sent earlier; nothing was sent again\. Draft a{16}\. Send receipt: r1/);
});
