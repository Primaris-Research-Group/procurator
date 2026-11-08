import test from 'node:test';
import assert from 'node:assert/strict';
import {withInventory,registerSupport,manual,IDENTITY,VERSION} from './support.mjs';

test('the inventory records tools (including factories) and commands as they are registered (0.20.0)',()=>{
 const reg={tools:[],commands:[]};
 const api={registerTool:(t,o)=>reg.tools.push([t,o]),registerCommand:c=>reg.commands.push(c),runtime:{x:1},on(){return this.runtime;}};
 const {api:p,inv}=withInventory(api);
 p.registerTool({name:'web_look',description:'Page-vision check.'});
 p.registerTool(ctx=>({name:'web_task_step',description:'Step an active task. ctx '+typeof ctx}),{optional:true,name:'web_task_step'});
 // Most factories in this plugin answer only for their conversation (agentId main); the probe must look like it.
 p.registerTool(ctx=>ctx.agentId==='main'?{name:'mission_list',description:'Missions.'}:null,{optional:true});
 p.registerCommand({name:'weblane',description:'Lanes.'});
 assert.equal(reg.tools.length,3);assert.equal(reg.commands.length,1);
 assert.deepEqual(inv.tools.map(t=>[t.name,t.optional]),[['web_look',false],['web_task_step',true],['mission_list',true]]);
 assert.equal(inv.commands[0].name,'weblane');
 assert.equal(p.runtime.x,1);assert.equal(p.on(),api.runtime);   // everything else passes through
});

test('the manual answers by section and by search, with live tool and command lists',()=>{
 const inv={tools:[{name:'memory_search',description:'Search memory.'}],commands:[{name:'memory',description:'Memory status.'}]};
 assert.match(manual(inv).contents,/Sections: purpose .*scope .*safety .*releases .*tools .*commands/);
 assert.match(manual(inv,{section:'tools'}).text,/memory_search: Search memory\./);
 assert.match(manual(inv,{section:'commands'}).text,/\/memory: Memory status\./);
 assert.match(manual(inv,{section:'safety'}).text,/never pressed twice/);
 assert.match(manual(inv,{section:'nope'}).error,/No section/);
 const h=manual(inv,{query:'CAPTCHA takeover'});assert.ok(h.hits.length>0);assert.ok(h.hits.some(x=>/CAPTCHA/i.test(x.text)));
 assert.match(IDENTITY(VERSION),/procurator_manual/);assert.ok(IDENTITY(VERSION).length<600);
 assert.equal(VERSION,JSON.parse(require_pkg()).version);
});
import fs from 'node:fs';
function require_pkg(){return fs.readFileSync(new URL('./package.json',import.meta.url),'utf8');}

test('the identity line goes to their main agent only and /manual is owner-only',async()=>{
 const hooks=[],tools=[],commands=[];
 registerSupport({on:(n,f,o)=>hooks.push({n,f,o}),registerTool:t=>tools.push(t),registerCommand:c=>commands.push(c)},{tools:[],commands:[{name:'manual',description:'Manual.'}]},ctx=>{if(!ctx.owner)throw new Error('owner only');});
 assert.match(hooks[0].f({}, {agentId:'main'}).appendSystemContext,/You are PROCURATOR/);assert.equal(hooks[0].f({}, {agentId:'voice'}),undefined);
 assert.equal(tools[0].name,'procurator_manual');
 assert.equal((await commands[0].handler({owner:false,args:''})).text,'owner only');
 const rel=(await commands[0].handler({owner:true,args:'releases'})).text;assert.match(rel,/^Release history \(PROCURATOR [^)]+\) · page 1 of \d/);assert.match(rel,/More: \/manual releases 2/);
 let all='';for(let n=1;n<=8;n++)all+=(await commands[0].handler({owner:true,args:'releases '+n})).text;assert.match(all,/0\.20\.0/);   // 0.24.3: paged, nothing cut off (0.28.0: the history now spans more pages)
 assert.match((await commands[0].handler({owner:true,args:'find second press'})).text,/•/);
});

test('0.24.3 (B14): /manual commands is paged and the last commands (health) are reachable',async()=>{
 const {withInventory,registerSupport,pageText}=await import('./support.mjs');
 const cmds=[],inv=withInventory({registerTool(){},registerCommand:c=>cmds.push(c),on(){}});
 for(let i=0;i<140;i++)inv.api.registerCommand({name:'cmd'+i,description:'Does thing number '+i+' with a fairly long description so the list runs past one Telegram message.'});
 inv.api.registerCommand({name:'healthstatus',description:'Health module status.'});
 registerSupport(inv.api,inv.inv??inv,()=>{});
 const manual=cmds.find(c=>c.name==='manual');
 const p1=(await manual.handler({args:'commands'})).text;
 assert.match(p1,/page 1 of \d+/);assert.match(p1,/More: \/manual commands 2/);assert.ok(p1.length<=3900);
 const total=Number(/page 1 of (\d+)/.exec(p1)[1]);
 const last=(await manual.handler({args:'commands '+total})).text;
 assert.match(last,/\/healthstatus: Health module status/);assert.doesNotMatch(last,/More:/);
 assert.deepEqual(pageText('a\nb',3),['a\nb']);assert.deepEqual(pageText('aa\nbb',4),['aa','bb']);
});
