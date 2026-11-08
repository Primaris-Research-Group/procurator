import test from 'node:test';
import assert from 'node:assert/strict';
import {registerProjectSources} from './project-sources.mjs';
import {ownerContext} from './register.mjs';
test('project source tool is main-only and cannot connect or acknowledge an account',async()=>{
 const tools=[],commands=[],calls=[];registerProjectSources({registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c),on(){}},async(...args)=>{calls.push(args);return {};},ownerContext);
 assert.equal(tools[0]({agentId:'phone'}),undefined);
 const tool=tools[0]({agentId:'main'});assert.deepEqual(tool.parameters.properties.action.enum,['status','read','files','read_file','chats','read_chat']);
 await tool.execute('read',{action:'status'});assert.equal(calls[0][0],'project_status');assert.equal(calls[0][2],undefined);
 const owner={channel:'telegram',senderId:'100000001',isAuthorizedSender:true,args:'chatgpt'};
 await commands[0].handler({...owner,senderId:'attacker'});assert.equal(calls.length,1);
 await commands[0].handler(owner);assert.equal(calls.at(-1)[0],'project_connect');assert.equal(calls.at(-1)[2],true);
});
