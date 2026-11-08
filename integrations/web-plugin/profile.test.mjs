import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {OwnerProfile,registerProfile} from './profile.mjs';
import {ownerContext} from './register.mjs';
function fixture(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'owner-profile-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return new OwnerProfile(dir);}
test('confirmed facts persist with provenance and reads can minimize disclosed fields',t=>{
 const p=fixture(t);p.set('first_name','Synthetic','Owner fixture');p.set('city','Example City','Owner fixture');
 const read=new OwnerProfile(p.directory).view(['first_name','email']);assert.equal(read.facts.first_name.value,'Synthetic');assert.equal(read.facts.first_name.source,'Owner fixture');assert.deepEqual(read.missing_fields,['email']);assert.equal(read.facts.city,undefined);assert.match(read.authority,/not approval/);assert.equal(fs.statSync(p.file).mode&0o777,0o600);
 for(const [field,value] of [['password','secret'],['date_of_birth','2026-02-30'],['email','not email'],['phone','555'],['city','line\nbreak']])assert.throws(()=>p.set(field,value,'Fixture'));
});
test('only authenticated owner commands can change facts; phone/model tools cannot',async t=>{
 const p=fixture(t),tools=[],commands=[];registerProfile({registerTool:x=>tools.push(x),registerCommand:x=>commands.push(x),on(){}},p,ownerContext);
 assert.equal(tools[0]({agentId:'phone'}),undefined);const tool=tools[0]({agentId:'main'});assert(!tool.parameters.properties.action);
 const cmd=commands.find(x=>x.name==='profileset'),owner={channel:'telegram',senderId:'100000001',isAuthorizedSender:true,args:'city Example City'};
 await cmd.handler({...owner,senderId:'other'});assert.equal(p.view().revision,0);
 await cmd.handler(owner);assert.equal(p.view().facts.city.value,'Example City');
 assert.equal(JSON.parse((await tool.execute('read',{fields:['city']})).content[0].text).facts.city.value,'Example City');
});
