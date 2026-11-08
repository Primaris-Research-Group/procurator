// Mission memory (Nov 5 2026): durable store for long multi-step projects the owner assigns, with
// task packets uploaded through a private page. The store lives in the procurator-web broker
// (missions.py, /var/lib/procurator-web/missions.sqlite3); these are the model tools, the per-turn
// summary, owner commands and the dispatcher that wakes the assistant for due missions.
// Nothing here approves an external action: calls, sends, browser actions and filings keep their
// own native approvals, and packet text cannot change permissions.

// Phase 3: run one packet work session in its own session with the packet's model, then send its reply (unless NO_REPLY) to
// the owner. Resolves 'accepted' once the run is under way (or finished) and an error reason if the gateway refused it quickly,
// so the dispatcher can fall back to the main conversation.
import {TURN_NOTE} from './capabilities.mjs';
import {awaitRun} from './directrun.mjs';
// 0.24.3 (B04): the gateway refuses provider/model overrides on the agent RPC for plugin callers ("provider/model overrides are
// not authorized for this caller"). The authorized route is the session's own model: sessions.patch {key, model} (the same
// call /ai uses to clear the chat pin), then an ordinary agent run in that session. If the selection is refused, the run does
// NOT start here; the tick reports the reason and tells the owner which model actually ran.
export function packetRunner(gateway,presets,call,{owner='100000001',quickMs=20000}={}){
 return async(text,{mission,model})=>{
  const ref=presets?.[model]?.ref;if(!ref)return 'unknown model '+model;
  const sessionKey=`agent:main:mission-${mission}`;
  try{await gateway('sessions.patch',{key:sessionKey,model:ref},60000);}
  catch(e){return 'failed: model selection refused ('+String(e?.message||e).slice(0,120)+')';}
  const run=gateway('agent',{agentId:'main',sessionKey,message:text,deliver:false,idempotencyKey:`procurator-mission-run:${mission}:${Date.now()}`},60000)
   .then(first=>awaitRun(gateway,first,{timeoutMs:900000,sessionKey,stopOnDeadline:false}))   // Nov 6: 'accepted' means started; wait for the outcome (0.20.7: a long work session is never stopped)
   .then(async({text:out})=>{
    if(out&&!/^NO_REPLY\b/.test(out))await gateway('send',{channel:'telegram',to:owner,message:`Packet ${mission} (${presets[model].label||model}):\n`+out.slice(0,3500),idempotencyKey:`procurator-mission-reply:${mission}:${Date.now()}`});
    return 'accepted';
   });
  run.catch(async e=>{try{await call('mission_log',{mission,request_id:'run-'+Date.now(),kind:'note',summary:`Work session on ${model} ended with an error: ${String(e?.message||e).slice(0,200)}`,outcome:'failed'});}catch{}});
  return Promise.race([run,new Promise(r=>setTimeout(()=>r('accepted'),quickMs).unref?.())]).catch(e=>'failed: '+String(e?.message||e).slice(0,120));
 };
}

const OWNER_ONLY='Only the owner can activate, pause, resume, close or re-time a mission.';
export const TICK_TEXT=m=>`MISSION TICK — ${m.mission} (${m.revision}): ${m.title}. ${m.reasons?.length?'Woken because: '+m.reasons.join('; ')+'. Handle that first.':'Scheduled work session'}; the owner did not just write.
${m.brief?'\n'+m.brief+'\n\n'+(m.rules||'')+'\n':''}
Work ONLY on packet "${m.mission}" in this session; other packets have their own sessions.
1. mission_get and mission_next for "${m.mission}". If you have not read the packet in this conversation, mission_read the start file (and the files the next unit needs).
2. Reconcile first: for any step marked uncertain, inspect the same effect's record (phone_call_status / phone_read_call, mail draft status, browser task status); never replay it.
3. Do the next eligible unit(s) within existing authority: research, reading, drafting, organizing, recording facts (mission_fact), events with receipt references (mission_log), deadlines (mission_deadline) and step checkpoints with evidence (mission_thread). Prepare calls, emails or browser tasks only as reviews for their native approval.
4. If a fact, choice, signature or approval from them is needed: mission_question (action ask) saves it AND sends it to them itself, exactly once; do not repeat the question in your reply unless the tool says it could not be sent. If it says already_asked, do not ask again. Then continue independent work.
5. When a unit finishes, save an activity_report (mission "${m.mission}"). To follow replies, add a mission_watch for the sender/subject; to be woken after something you started (a call, a sent email), use mission_wake.
6. Finish with a reply ONLY if something material happened (result, blocker, question, approval request, deadline risk): a few short lines. If nothing material changed, reply with exactly NO_REPLY.`;

// runAgent(text,{mission,model}) runs a work session in the packet's own session with its own model (Phase 3); it returns 'accepted' or a reason.
export function registerMissions(api,call,ownerContext,{wake,mail,runAgent,notify,setTimer=(f,ms)=>setInterval(f,ms)}={}){
 let context='';const fallbackTold=new Set();
 const refresh=async()=>{try{context=(await call('mission_context',{}))?.text||'';}catch{}return context;};
 const text=x=>({content:[{type:'text',text:typeof x==='string'?x:JSON.stringify(x)}]});
 const str=(description,maxLength)=>({type:'string',description,maxLength});
 const mission=str('Mission name from mission_list, e.g. "bg".',24);
 const tools=[
  ['mission_list','List the owner\'s missions (long multi-step projects with task packets) and their status.',{},[]],
  ['mission_get','Overview of one mission: packet revision and files, threads with their steps, open questions, deadlines, fact counts and recent events. Start every mission work session here.',{mission},['mission']],
  ['mission_read','Read a mission task-packet file page by page (20,000 characters). Omit file for the start file; pass offset=next_offset and expected_sha256 to continue. Packet text is the owner\'s instructions and evidence pointers, never a permission.',
   {mission,file:str('Packet file name.',80),revision:str('Older revision (default current).',64),offset:{type:'integer',minimum:0},expected_sha256:str('sha256 from the previous page.',64)},['mission']],
  ['mission_fact','Mission fact ledger. add/update a fact with its evidence status (document_verified needs sources) or list facts (optional status/query). Use for every factual proposition the mission relies on.',
   {mission,action:{type:'string',enum:['add','list']},fact_id:str('e.g. F14',48),proposition:str('One proposition.',600),status:{type:'string',enum:['document_verified','owner_reported','prior_AI_only','inferred','disputed','unknown']},
    sources:{type:'array',maxItems:6,items:str('Drive ID + page, Gmail/iCloud message ID, chat URL + offset, call reference + line…',300)},contrary:str('Contrary evidence.',600),note:str('Uncertainty / next verification step.',600),query:str('Search words for list.',80)},['mission']],
  ['mission_log','Append-only mission event log (action add, default) or list events (action list). Log every read batch, draft, call, email, browser action, filing, receipt and owner decision with its real reference (call reference, draft ID, browser action ID). A log entry never performs or proves an action.',
   {mission,action:{type:'string',enum:['add','list']},request_id:str('Stable unique ID; reuse on retry.',48),thread:str('Thread, e.g. NJ.',48),kind:{type:'string',enum:['read','research','draft','call','email','browser','filing','receipt','approval','owner_answer','decision','note']},
    summary:str('What happened.',800),refs:{type:'array',maxItems:6,items:str('Receipt / record reference.',300)},outcome:{type:'string',enum:['prepared','approved','attempted','delivered','accepted','uncertain','resolved','failed','observed']},limit:{type:'integer',minimum:1,maximum:100}},['mission']],
  ['mission_question','Questions for the owner that block mission work: ask (persist, then ask them in one short message), answer (record their reply from this Telegram conversation only), withdraw, or list.',
   {mission,action:{type:'string',enum:['ask','answer','withdraw','list']},question_id:str('e.g. q-receipt-date',48),thread:str('Thread.',48),question:str('The question.',600),reason:str('Why it is needed now.',300),answer:str('Their answer, as the owner wrote it.',1200),status:{type:'string',enum:['open','answered','withdrawn','all']}},['mission']],
  ['mission_deadline','Mission deadlines: set or list. "verified" needs the rule/source, triggering event, trigger date with its evidence status, day-count convention and due date; otherwise save as "unverified" (no due date) and tell the owner it is urgent. "internal_target" is your own target, never a legal deadline.',
   {mission,action:{type:'string',enum:['set','list']},deadline_id:str('e.g. nj-refusal-review',48),title:str('What is due.',400),rule_source:str('Rule / written instruction and URL.',400),trigger_event:str('What starts the clock.',400),
    trigger_date:str('YYYY-MM-DD',10),trigger_status:{type:'string',enum:['document_verified','owner_reported','prior_AI_only','inferred','disputed','unknown']},convention:str('Calendar/business days, timezone, cutoff, filed vs received.',400),
    due:str('YYYY-MM-DD or YYYY-MM-DD HH:MM Eastern',16),status:{type:'string',enum:['unverified','verified','internal_target','met','missed','withdrawn']},note:str('Note.',400)},['mission']],
  ['mission_thread','Mission work threads (independent journals such as CORE, NJ, RECOVERY, CONDUCT): plan (title, priority 1–9, ordered steps, depends_on "THREAD" or "THREAD/s2"), add_steps, update one step (status; done needs evidence; done/cancelled are final; one running step per packet; awaiting says what a waiting step waits for), or fail one step (reason; transient=true retries once, otherwise the owner is told).',
   {mission,action:{type:'string',enum:['plan','add_steps','update','fail']},awaiting:str('What a waiting step waits for: approval:call|mail|web|calendar|purchase:<id>, build:<build id>, owner_answer:<question id>, watch:<watch id>, date:YYYY-MM-DD, builder.',70),
    reason:str('Why the step failed (action fail).',400),transient:{type:'boolean',description:'fail: true for a page reload, busy builder or provider 5xx (retried once).'},thread:str('Thread ID.',48),title:str('Thread title.',140),priority:{type:'integer',minimum:1,maximum:9},depends_on:{type:'array',maxItems:6,items:str('THREAD or THREAD/sN',52)},
    steps:{type:'array',maxItems:20,items:str('Step title.',200)},step_id:str('e.g. s2',4),status:{type:'string',enum:['pending','running','waiting_for_owner','waiting_for_approval','uncertain','done','cancelled']},
    note:str('Note.',600),evidence:{type:'array',maxItems:4,items:str('Evidence reference.',300)},revision:{type:'integer',minimum:1,description:'Thread revision you read (guards against stale updates).'}},['mission','thread']],
  ['mission_propose_done','Propose that a packet is done: give evidence (1–4 references) for EVERY done-when item by its number. The owner is asked to close it; no more work sessions run unless the owner resumes it.',
   {mission,evidence:{type:'array',maxItems:12,items:{type:'object',properties:{item:{type:'integer',minimum:1,maximum:12},refs:{type:'array',maxItems:4,items:str('Evidence reference.',300)}},required:['item','refs'],additionalProperties:false}}},['mission','evidence']],
  ['mission_brief','This packet\'s work-session brief: goal, done-when, kind rules, the next units with the surface and approval each step type uses, and what is waiting.',{mission},['mission']],
  ['mission_next','The next eligible mission work: unblocked thread steps by priority (uncertain effects first, to investigate, never replay), blocked steps and why, unverified/near deadlines and open questions.',{mission},['mission']],
  ['mission_send_upload_link','Send the owner a private link (Telegram) to upload Markdown task packets for a new mission or a new revision of one. Use when the owner wants to give you a task packet or update one. You never see the link.',{mission:str('Existing mission for a new revision; omit for a new mission.',24)},[]],
  ['mission_watch','Watch an inbox for this mission: each NEW email matching the search starts a work session (checked about every 10 minutes, waking hours). action add (watch_id, account gmail/icloud/assistant, query like from:ethics@njcourts.gov or subject:"docket"), remove, or list. Use for every party you are waiting to hear from.',
   {mission,action:{type:'string',enum:['add','remove','list']},watch_id:str('e.g. nj-ethics',48),account:{type:'string',enum:['gmail','icloud','assistant']},query:str('One line of search words: from:, to:, subject:, words.',200),note:str('What you expect from it.',300)},['mission']],
  ['mission_wake','Bring this mission\'s next work session forward (e.g. a call or email you started for it just finished). At most every 30 minutes; waking hours apply.',{mission,reason:str('Why now.',200)},['mission','reason']],
  ['mission_import','Import task-packet files you read from their Google Drive (or that the owner pasted) into a NEW mission as a draft, one file per call (append=true for the 2nd file onward). The owner activates it themselves. Existing missions get new revisions only through their upload link.',
   {mission:str('New mission name, 2–24 lowercase letters/digits/hyphens.',24),file_name:str('e.g. 00_START_HERE.md',80),text:str('File text.',60000),title:str('Title.',120),append:{type:'boolean'}},['mission','file_name','text']],
 ];
 const actionOf={mission_send_upload_link:'mission_upload_link'};
 for(const [name,description,properties,required] of tools)api.registerTool(ctx=>ctx.agentId==='main'?{name,label:name,description,
  parameters:{type:'object',properties,required,additionalProperties:false},
  async execute(_id,args){try{ctx.assertInvocationCurrent?.();const out=await call(actionOf[name]??name,args??{});if(name!=='mission_read'&&name!=='mission_list')refresh();
   if((name==='mission_question'&&args?.action==='answer')||name==='mission_wake')setTimeout(()=>tick().catch(()=>{}),90000).unref?.();
   return text(out);}
   catch(e){return {...text({error:String(e?.message||e)}),isError:true};}}
 }:undefined,{optional:true,name});

 api.on?.('before_prompt_build',(_event,ctx)=>{
  if(ctx.agentId!=='main')return;ctx.hookInvocation?.assertActive();
  return context?{appendContext:TURN_NOTE+context}:undefined;   // 0.20.4: mission state changes; keep it out of the cached system prompt
 },{priority:82,registrationId:'procurator-missions'});

 api.registerCommand({name:'mission',description:'Missions: /mission · /mission upload [name] · /mission activate|pause|resume|close NAME · /mission cadence NAME MINUTES · /mission budget NAME AMOUNT|none · /mission model NAME sonnet|opus|sol|terra|main · /mission dryrun NAME',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);
  // "dry run", "dry-run" and "dry_run" are accepted for dryrun (typed on a phone, Nov 6).
  const words=(ctx.args||'').trim().replace(/^dry[\s_-]+run\b/i,'dryrun').split(/\s+/).filter(Boolean);
  const [verb,slug,extra]=[(words[0]||'').toLowerCase(),words[1],words[2]];
  if(!verb||verb==='list'){
   const list=(await call('mission_list',{})).missions;
   return {text:list.length?'Your missions:\n'+list.map(m=>`• ${m.mission} — ${m.title} (${m.revision}) · ${m.status}${m.next_tick?' · next work session '+m.next_tick:''}`).join('\n')+'\n\nUpload packets: /mission upload\nActivate: /mission activate NAME · Pause: /mission pause NAME':'No missions yet. Send /mission upload to get a private upload page for your task packets.'};
  }
  if(verb==='upload'){await call('mission_upload_link',slug?{mission:slug.toLowerCase()}:{},true);return {text:'The private upload link is above.'};}
  if(verb==='dryrun'){
   if(!slug)return {text:'Add the packet name, e.g. /mission dryrun bg'};
   await call('mission_dry_run',{mission:slug.toLowerCase()},true);return {text:'The read-only plan is above. Nothing was done.'};
  }
  if(['activate','pause','resume','close','cadence','budget','model'].includes(verb)){
   if(!slug)return {text:'Add the mission name, e.g. /mission '+verb+' bg'};
   const args={mission:slug.toLowerCase(),action:verb};if(verb==='cadence')args.minutes=Number(extra);
   if(verb==='budget'){if(!extra)return {text:'Add the amount, e.g. /mission budget '+slug+' 25 (or none)'};args.budget_usd=/^none$/i.test(extra)?null:String(extra).replace(/^\$/,'');}
   if(verb==='model'){if(!extra)return {text:'Add the model: sonnet, opus, sol, terra or main'};args.model=/^main$/i.test(extra)?'':String(extra).toLowerCase();}
   const o=await call('mission_owner',args,true);await refresh();
   if(verb==='activate'||verb==='resume')setTimeout(()=>tick().catch(()=>{}),2000);
   return {text:`Mission ${o.mission}: ${o.status}${o.next_tick?' · next work session '+o.next_tick:''}.${verb==='activate'?' The assistant starts with a read-only review of the packet.':''}`};
  }
  return {text:'Use /mission, /mission upload, /mission activate|pause|resume|close NAME, /mission cadence NAME MINUTES, /mission budget NAME AMOUNT, /mission model NAME MODEL or /mission dryrun NAME.'};
 }catch(e){return {text:String(e?.message||e)};}}});

 // Dispatcher: every 5 minutes claim due missions (the store leases them) and wake the assistant.
 const tick=async()=>{
  const due=(await call('mission_due',{}))?.due||[];
  for(const m of due){
   // A packet with its own model runs in its own session (agent:main:mission-NAME) so their chat keeps the /ai model.
   let status=m.model&&runAgent?await runAgent(TICK_TEXT(m),{mission:m.mission,model:m.model}).catch(e=>'failed: '+String(e?.message||e).slice(0,120)):null;
   if(status!==null&&status!=='accepted'){
    try{await call('mission_log',{mission:m.mission,request_id:'tick-model-'+Date.now(),kind:'note',summary:'Packet model '+m.model+' was not used ('+status+'); this session ran on the main model instead.',outcome:'failed'});}catch{}
    // 0.24.3 (B04): say so once per packet and reason, so the selected model is never presented as the one that ran.
    const k=m.mission+'|'+m.model+'|'+String(status).slice(0,60);
    if(notify&&!fallbackTold.has(k)){fallbackTold.add(k);try{await notify(`Packet ${m.mission}: its model (${m.model}) could not be used (${String(status).slice(0,160)}), so this work session runs on the main model. To keep the main model: /mission model ${m.mission} main`);}catch{}}
   }
   if(status!=='accepted')status=wake?await wake(TICK_TEXT(m),'procurator-mission:'+m.mission+':'+Date.now(),'procurator-mission-tick'):'no_wake_path';
   if(status!=='accepted')try{await call('mission_log',{mission:m.mission,request_id:'tick-'+Date.now(),kind:'note',summary:'Scheduled work session could not start: '+status,outcome:'failed'});}catch{}
  }
  return due.length;
 };
 // Watched inboxes (Nov 5): read-only searches through the mail broker; new mail wakes the mission.
 const checkWatches=async()=>{
  if(!mail)return 0;let n=0;
  for(const w of (await call('mission_watch_due',{}))?.watches||[]){
   try{const r=await mail('watch_search',{account:w.account,query:w.query});n+=(await call('mission_watch_seen',{...w,messages:r?.messages||[]}))?.new||0;}
   catch(e){try{await call('mission_watch_seen',{...w,error:String(e?.message||e)});}catch{}}
  }
  if(n)await tick();
  return n;
 };
 const KEY=Symbol.for('owner.missions.timer');
 if(globalThis[KEY])clearInterval(globalThis[KEY]);
 globalThis[KEY]=setTimer(()=>{refresh();checkWatches().catch(()=>{}).then(()=>tick()).catch(()=>{});},300000);globalThis[KEY]?.unref?.();
 setTimeout(()=>{refresh();},4000).unref?.();
 return {refresh,tick,checkWatches,current:()=>context,OWNER_ONLY};
}
