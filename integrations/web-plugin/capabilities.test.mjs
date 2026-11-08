import {test} from 'node:test';
import assert from 'node:assert/strict';
process.env.PROCURATOR_EDITION='x';   // 0.30.0: the card tests describe PROCURATOR-X (the full card); the standard edition drops the builds line
const {CARD,statusLines,registerCapabilities,configLines,permissionsText}=await import('./capabilities.mjs');
const regCaps=registerCapabilities;

test('the card names every tool family the Nov 4 summary forgot, and what is not available',()=>{
 for(const want of ['drive_search','docs_get','workspace_create_document','calendar_create_event','tasks_add','mail_request_review','project_sources','news_headlines','market_quotes','web_site_logins','phone_read_call','owner_profile','/spendcap'])assert.ok(CARD.includes(want),want);
 assert.match(CARD,/NOT available[^]*locating people/);assert.match(CARD,/places_search/);assert.match(CARD,/\/healthread/);assert.match(CARD,/bank or card access beyond read-only balances/);assert.match(CARD,/web_look/);assert.match(CARD,/flight_search/);
 assert.ok(CARD.length<7300,CARD.length);   // 0.30.0: neutral owner wording ("the owner" for "he") and the welcome line; 0.29.0: +Apple account line; 0.28.0: +library and home line; 0.27.0: +public posts and events line (0.25.0: +lookups, texts, MCP and health safeguards)
 assert.ok(CARD.includes('build_prepare')&&CARD.includes('/buildapprove'));
});
test('live status says what is ready and the exact one-time step for what is not',()=>{
 const s=statusLines({gmail_send_authorized:false,gmail_modify_authorized:false,google_calendar_tasks_authorized:true,icloud_connected:true,icloud_send_enabled:false},
  {saved_site_logins:[{site:'leaveco',status:'awaiting_owner_sign_in'},{site:'hotels',status:'owner_connected'}],purchase_cap_usd:500},{sources:[{service:'chatgpt',status:'owner_connected'},{service:'claude',status:'owner_connected'}]});
 assert.match(s,/Gmail sending: NOT yet — the owner runs once .*connect-google\.py --start --force-consent --all/);
 assert.match(s,/Google Calendar and Tasks: ready/);assert.match(s,/iCloud mail: connected, sending not set up/);
 assert.match(s,/leaveco \(awaiting owner sign in\), hotels \(signed in\)/);assert.match(s,/cap: \$500\.00/);assert.match(s,/chatgpt connected, claude connected/);
 assert.match(statusLines(null,null,null),/Gmail sending: unknown/);
 // 0.19.1 (QA T2-10): the home PC browser and uploads appear in the live status.
 assert.match(statusLines(null,{home_browser:{registered:true,online:true,signed_in:['leaveco']}},null),/Home PC browser: online; signed in at the PC: leaveco\. Uploads \(web_upload_stage\)/);
 assert.match(statusLines(null,{home_browser:{registered:false}},null),/Home PC browser: not set up/);
});
test('hook injects card + live status for the main agent only; tool refreshes',async()=>{
 const hooks=[],tools=[];
 const api={on:(name,fn,opts)=>hooks.push({name,fn,opts}),registerTool:(f,o)=>tools.push(f)};
 const web=async a=>a==='status'?{saved_site_logins:[],purchase_cap_usd:300}:{sources:[]};
 const mail=async()=>({gmail_send_authorized:true,gmail_modify_authorized:true,google_calendar_tasks_authorized:true,icloud_connected:true,icloud_send_enabled:true});
 const c=registerCapabilities(api,web,mail);
 assert.equal(hooks[0].name,'before_prompt_build');assert.equal(hooks[0].fn({}, {agentId:'phone'}),undefined);
 await c.refresh();
 const built=hooks[0].fn({}, {agentId:'main'}),out=built.appendSystemContext;assert.match(out,/ASSISTANT CAPABILITIES/);
 // 0.20.4: the system prompt part is fixed (cacheable); the live status goes with the turn's message, marked as not typed by them.
 assert.doesNotMatch(out,/LIVE STATUS|Gmail sending/);assert.match(built.appendContext,/^\(Context added by PROCURATOR's system for this turn, not typed by the owner\.\)\nLIVE STATUS/);
 assert.match(built.appendContext,/Gmail sending: ready/);assert.match(built.appendContext,/cap: \$300\.00/);
 await c.refresh();assert.equal(hooks[0].fn({}, {agentId:'main'}).appendSystemContext,out);
 const tool=tools[0]({agentId:'main'});assert.equal(tool.name,'assistant_capabilities');assert.equal(tools[0]({agentId:'phone'}),undefined);
 assert.match((await tool.execute()).content[0].text,/Calendar and Tasks: ready/);
 clearInterval(globalThis[Symbol.for('owner.capabilities.refresh')]);
});

test('config check reports which Google connectors and optional tools are really on',()=>{
 const off=configLines({mcp:{servers:{google:{command:'node',args:['/opt/procurator/bin/google-mcp.cjs']}}},tools:{alsoAllow:['project_sources','news_headlines']}});
 assert.match(off,/read connector \(Gmail\/Drive\/Docs\): configured/);assert.match(off,/Docs creation .*NOT configured/);
 assert.match(off,/not switched on yet .*calendar_events/);assert.doesNotMatch(off,/project_sources/);
 const on=configLines({mcp:{servers:{a:{args:['google-mcp.cjs']},b:{args:['/opt/x/integrations/google-output/index.cjs']}}}});
 assert.match(on,/Docs creation \(assistant Drive folder\): configured/);assert.equal(configLines(null),'');
});
test('/permissions shows each connection in plain words with live status and is owner-only',async()=>{
 const t=permissionsText({gmail_send_authorized:true,gmail_modify_authorized:false,google_calendar_tasks_authorized:true,icloud_send_enabled:false,assistant_address:'owner+assistant@example.com (send ready)'},
  {purchase_cap_usd:300,purchase_cards:['4242'],saved_site_logins:[{site:'hotels',status:'owner_connected'},{site:'leaveco',status:'awaiting_owner_sign_in'}]});
 for(const want of ['Asks you: every email sent (/mailsend)','mark read/unread, move to/from Junk (not switched on yet)','owner+assistant@example.com','$300.00 per purchase (/spendcap) · only cards ending 4242','Signed-in sites (hotels)','logs into wellsfargo.com','/callapprove'])assert.ok(t.includes(want),want);
 assert.ok(!/`/.test(t));
 const commands=[];regCaps({registerCommand:c=>commands.push(c),registerTool(){},on(){}},async()=>({}),async()=>({}),ctx=>{if(!ctx.owner)throw new Error('owner only');});
 const cmd=commands.find(c=>c.name==='permissions');
 assert.match((await cmd.handler({owner:false})).text,/owner only/);
 assert.match((await cmd.handler({owner:true})).text,/What your assistant can do/);
});

test('standard edition: the card has no code-builds line and is shorter',async()=>{
 // The module is evaluated once per process, so a fresh evaluation needs a cache-busting import.
 process.env.PROCURATOR_EDITION='standard';
 const {CARD:STD}=await import('./capabilities.mjs?edition=standard');
 process.env.PROCURATOR_EDITION='x';
 assert.ok(!STD.includes('build_prepare')&&!STD.includes('/buildapprove'));
 assert.ok(STD.length<CARD.length);
});
