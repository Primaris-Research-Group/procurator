// Activity log (Nov 5 2026): what the assistant did, kept by the procurator-web broker (activity.py).
// System records come from the services themselves (browser tasks and approved actions, emails sent or
// moved to Trash, calendar and Google Tasks changes, phone calls). The assistant adds its own
// after-task reports with activity_report. The owner reads it with /activity or a private page.
// No tool here can create or change a system record, and nothing here performs an action.

import fs from 'node:fs';
import net from 'node:net';

// Read-only calls to the mail broker's model-facing socket (pending approvals, mission inbox watches).
export function mailBroker(action,args={},timeoutMs=60000){
 return new Promise((resolve,reject)=>{
  const s=net.createConnection('/run/procurator-mail/drafts.sock');let data='';s.setTimeout(timeoutMs);
  s.on('connect',()=>s.write(JSON.stringify({action,args})+'\n'));
  s.on('data',c=>{data+=c;if(data.length>200000){s.destroy();reject(new Error('Mail response too large.'));}
   else if(data.includes('\n')){s.end();try{const r=JSON.parse(data.split('\n')[0]);r.ok?resolve(r.result):reject(new Error(r.error));}catch(e){reject(e);}}});
  s.on('timeout',()=>{s.destroy();reject(new Error('Mail check timed out.'));});
  s.on('error',()=>reject(new Error('Mail service unavailable.')));
 });
}

const ET=t=>new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(t));
// Gateway-side "waiting / running / scheduled" items: email and calendar approvals, phone calls, reminders, morning brief.
export async function collectUpcoming({mail=mailBroker,voiceFile='/home/node/.openclaw/procurator-voice/tests.json',now=Date.now()}={}){
 const items=[];
 try{for(const p of (await mail('pending',{}))?.pending||[])items.push({group:'Waiting for you',kind:p.kind,what:p.what,how:`Send the ${p.command.split(' ')[0]} line from its review message (expires in ${p.expires_in_min} min)`});}catch{}
 try{
  const j=JSON.parse(fs.readFileSync(voiceFile,'utf8'));
  for(const r of j.records||[]){
   const what=`Phone call to ${r.plan?.to}${r.plan?.task||r.plan?.purpose?' — '+String(r.plan.task??r.plan.purpose).replace(/\s+/g,' ').slice(0,100):''}`;
   if(r.status==='pending'&&r.expiresAt>now)items.push({group:'Waiting for you',kind:'call',what,how:`Approve with the /callapprove line from its review (expires in ${Math.max(0,Math.round((r.expiresAt-now)/60000))} min)`});
   else if(['attempting','initiated','hangup_requested'].includes(r.status)&&now-(r.attemptedAt??0)<20*60000)items.push({group:'In progress',kind:'call',what,how:'Call in progress: /callstatus '+r.id});
  }
 }catch{}
 try{for(const x of globalThis[Symbol.for('owner.reminders')]?.list()||[])items.push({group:'Scheduled',kind:'calendar',what:'Reminder: '+x.text,when:x.at/1000,how:''});}catch{}
 try{const b=globalThis[Symbol.for('owner.morning-brief')]?.status();if(b?.enabled&&b.next)items.push({group:'Scheduled',kind:'other',what:'Morning brief ('+b.next+')',how:''});}catch{}
 return items;
}

export const KIND_ALIASES={call:'call',calls:'call',phone:'call',browser:'browser_task',browsing:'browser_task',tasks:'browser_task',actions:'browser_action',
 booking:'booking',bookings:'booking',purchase:'purchase',purchases:'purchase',email:'email',emails:'email',mail:'email',trash:'email_trash',
 calendar:'calendar',reminders:'calendar',todo:'task_list',signin:'signin',logins:'signin',mission:'mission',missions:'mission',report:'report',reports:'report'};

export function parseActivityArgs(raw){
 const w=String(raw||'').trim().toLowerCase().replace(/#/g,'').split(/\s+/).filter(Boolean);
 if(!w.length)return {days:3};
 if(w[0]==='page')return {page:true};
 if(['upcoming','next','pending','waiting','planned'].includes(w[0]))return {upcoming:true};
 if(w[0]==='days'){const days=Number(w[1]);const kind=KIND_ALIASES[w[2]];return Number.isInteger(days)&&days>=1&&days<=400?{days,...kind?{kind}:{}}:{error:true};}
 if(/^\d+$/.test(w[0])&&w.length===1)return {id:Number(w[0])};
 const kind=KIND_ALIASES[w[0]];if(kind){const days=Number(w[1]||7);return {days:Number.isInteger(days)&&days>=1&&days<=400?days:7,kind};}
 return {error:true};
}

export const REPORT_GUIDE='After you finish any task for the owner (a call, browser task, booking, email, calendar change, research or mission step) — or stop one unfinished — save one activity_report: what the owner asked, what you did, the result with exact names, dates, amounts and reference numbers, and anything still open. Keep it factual; it is their record.';

export function registerActivity(api,call,ownerContext,{collect=collectUpcoming,setTimer=(f,ms)=>setInterval(f,ms)}={}){
 const pushUpcoming=async()=>{try{await call('activity_upcoming_push',{items:await collect()});}catch{}};
 const text=x=>({content:[{type:'text',text:typeof x==='string'?x:JSON.stringify(x)}]});
 const str=(description,maxLength)=>({type:'string',description,maxLength});
 const kinds=['call','browser_task','browser_action','purchase','booking','email','email_trash','calendar','task_list','signin','mission','report','other'];
 const tools=[
  ['activity_search','Look back at what you and the services did for the owner: phone calls, browser tasks and approved actions, bookings, purchases, emails sent or moved to Trash, calendar and Google Tasks changes, saved logins, and your own after-task reports. Newest first; use activity_get for one entry in full. Check this before telling them what was or was not done earlier.',
   {days:{type:'integer',minimum:1,maximum:400,description:'How far back (default 7).'},kind:{type:'string',enum:kinds},status:{type:'string',enum:['completed','in_progress','failed','uncertain','cancelled','paused','recorded']},
    query:str('Words to find in titles, details or references.',80),mission:str('Mission name.',24),source:{type:'string',enum:['assistant','system'],description:'Only your reports, or only service records.'},limit:{type:'integer',minimum:1,maximum:100}},[]],
  ['activity_get','One activity log entry in full (details, timeline, references).',{id:{type:'integer',minimum:1}},['id']],
  ['activity_upcoming','What is waiting for the owner (approvals, open mission questions), running now (browser tasks, calls) and scheduled (mission work sessions, deadlines in 14 days, reminders, morning brief). Use when the owner asks what is pending or planned. Approval lines themselves are never shown here; the owner uses the ones in their review messages.',{},[]],
  ['activity_report',REPORT_GUIDE+' Reuse the same request_id to update a report (e.g. when a follow-up finishes it). This only records; it never performs or proves an action — cite real references (call reference, browser task ID, mail draft ID).',
   {request_id:str('Stable ID for this report, e.g. cvs-flu-2026-11-05.',80),title:str('One line: what was done.',200),details:str('What the owner asked, what you did, the outcome with exact details, what is still open.',6000),
    status:{type:'string',enum:['completed','in_progress','failed','uncertain','cancelled','paused','recorded']},kind:{type:'string',enum:['report','call','browser_task','booking','purchase','email','calendar','mission','other']},
    refs:{type:'array',maxItems:8,items:str('Reference: call reference, browser task ID, draft ID, confirmation number…',300)},mission:str('Mission name, if part of one.',24)},['request_id','title','details']],
 ];
 for(const [name,description,properties,required] of tools)api.registerTool(ctx=>ctx.agentId==='main'?{name,label:name,description,
  parameters:{type:'object',properties,required,additionalProperties:false},
  async execute(_id,args){try{ctx.assertInvocationCurrent?.();if(name==='activity_upcoming')await pushUpcoming();return text(await call(name,args??{}));}catch(e){return {...text({error:String(e?.message||e)}),isError:true};}}
 }:undefined,{optional:true,name});

 api.registerCommand({name:'activity',description:'What your assistant did and has planned: /activity · /activity upcoming · /activity days 7 calls · /activity 12 · /activity page',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);
  const a=parseActivityArgs(ctx.args);
  if(a.error)return {text:'Try: /activity upcoming (waiting for you, running, scheduled) · /activity (last 3 days) · /activity days 7 · /activity days 30 calls · /activity 12 (one entry in full) · /activity page (private page with everything)'};
  if(a.page||a.upcoming)await pushUpcoming();
  if(a.page){await call('activity_owner',{page:true},true);return {text:'The private link to your activity page is above (read-only, works for 45 minutes).'};}
  return {text:(await call('activity_owner',a,true)).text};
 }catch(e){return {text:String(e?.message||e)};}}});
 // Keep the page's 'waiting / scheduled' section fresh (mail approvals, calls, reminders live in the gateway).
 const KEY=Symbol.for('owner.activity.upcoming');
 if(globalThis[KEY])clearInterval(globalThis[KEY]);
 globalThis[KEY]=setTimer(()=>{pushUpcoming();},300000);globalThis[KEY]?.unref?.();
 return {pushUpcoming};
}
