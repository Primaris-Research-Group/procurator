// Activity log for phone calls (Nov 5 2026). After each owner-approved call ends, one entry goes to the
// assistant's activity log (kept by the browser broker): who was called, the task, how long, the
// summary (or where to read the transcript). The entry is updated in place when a summary arrives later.
// Best effort: a log failure never affects calls or reports.
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import {atomic} from './core.mjs';
import {classifyCall} from './callflow.mjs';

export function postActivity(entry,socketPath='/run/procurator-web/tools.sock',timeoutMs=4000){
 return new Promise((resolve,reject)=>{
  const s=net.createConnection(socketPath);let data='';
  const timer=setTimeout(()=>{s.destroy();reject(new Error('activity log timeout'));},timeoutMs);
  s.on('connect',()=>s.write(JSON.stringify({action:'activity_record',args:{source:'voice',entry}})+'\n'));
  s.on('data',d=>{data+=d;if(data.includes('\n')){clearTimeout(timer);s.end();try{const r=JSON.parse(data);r.ok?resolve(r.result):reject(new Error(String(r.error)));}catch(e){reject(e);}}});
  s.on('error',e=>{clearTimeout(timer);reject(e);});
 });
}

const minutes=ms=>{const s=Math.max(0,Math.round(ms/1000));return s<60?s+' s':Math.floor(s/60)+' min '+String(s%60).padStart(2,'0')+' s';};
const ET=t=>new Date(t).toLocaleString('en-US',{timeZone:'America/New_York',weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});

export function callEntry(r,call,summary){
 const plan=r.plan||{};const ended=call&&Number.isFinite(call.endedAt);
 const reached=ended?classifyCall(call,r):null;
 const goodVoicemail=reached?.reached==='voicemail'&&plan.voicemail==='leave_message';
 const status=!r.callId?(r.status==='uncertain'?'uncertain':'failed'):!call?'uncertain':!ended?'in_progress':(reached.reached==='person'||goodVoicemail)?'completed':reached.reached==='unclear'?'uncertain':'failed';
 const lines=['Number: '+plan.to,'Task: '+String(plan.task??plan.purpose??'').slice(0,500)];
 if(plan.successCriteria)lines.push('Goal: '+String(plan.successCriteria).slice(0,250));
 if(plan.menuGoal)lines.push('Phone menu goal: '+String(plan.menuGoal).slice(0,200));
 if(Number.isFinite(r.attemptedAt))lines.push('Placed: '+ET(r.attemptedAt)+' (after your /callapprove)');
 if(ended){
  const start=Number.isFinite(call.answeredAt)?call.answeredAt:(Number.isFinite(call.startedAt)?call.startedAt:r.attemptedAt);
  lines.push('Length: '+minutes(call.endedAt-start)+(call.state?' · ended: '+String(call.state).replace(/[-_]/g,' '):''));
  lines.push('Reached: '+reached.summary+(reached.path.length?' Path: '+reached.path.join(' → ')+'.':'')+(reached.hold_seconds?' Hold: '+minutes(reached.hold_seconds*1000)+'.':'')+' (estimated from the transcript)');
  if(status==='failed')lines.push('Not retried automatically'+(reached.reached==='hold_only'?'; a longer call (up to 25 minutes) may get through.':'.'));
 }
 if(r.callId&&!call)lines.push('The ended-call record was not found; check /callstatus '+r.id+'. Calls are never redialed automatically.');
 if(!r.callId)lines.push(r.status==='uncertain'?'Placement could not be confirmed; it is never redialed automatically.':'The call was not placed.');
 if(summary)lines.push('',String(summary).replace(/\n*Full transcript:[\s\S]*$/,'').slice(0,4000));
 else if(ended)lines.push('','No summary yet.');
 if(r.callId)lines.push('','Full transcript in Telegram: /calltranscript '+r.id);
 const title=(status==='failed'&&ended?'Not reached — ':'')+(r.kind==='assignment'?'Called ':'Test call to ')+plan.to+(plan.task||plan.purpose?' — '+String(plan.task??plan.purpose).replace(/\s+/g,' ').slice(0,110):'');
 return {key:'call-'+r.id,kind:'call',title,status,details:lines.join('\n'),refs:['call reference '+r.id]};
}

export class CallActivity {
 constructor(calls,reports,post=postActivity){this.calls=calls;this.reports=reports;this.post=post;}
 file(){return path.join(this.calls.directory,'activity-logged.json');}
 seen(){try{return JSON.parse(fs.readFileSync(this.file(),'utf8'));}catch{return {};}}
 async sweep({maxAgeMs=7*86400000}={}){
  const now=this.calls.now(),seen=this.seen(),out=[];let changed=false;
  for(const r of this.calls.read().records){
   if(!Number.isFinite(r.attemptedAt)||now-r.attemptedAt>maxAgeMs)continue;
   try{this.calls.verify(r);}catch{continue;}
   let call,summary;
   if(r.callId){
    try{call=await this.reports.record(r);}catch{call=undefined;}
    if(call&&Number.isFinite(call.endedAt)&&this.reports.reached)try{await this.reports.reached(r);}catch{}   // learns a working menu path
    if(!call&&now-r.attemptedAt<15*60000)continue;   // still in progress or not yet persisted
    const saved=r.callId?this.reports.read(r.callId):undefined;
    summary=typeof saved?.text==='string'?saved.text.replace(/^Call summary — [a-f0-9]+\n/,''):typeof call?.metadata?.callReport?.summary==='string'?call.metadata.callReport.summary:undefined;
   }else if(['attempting'].includes(r.status))continue;
   const entry=callEntry(r,call,summary);
   const sig=crypto.createHash('sha256').update(JSON.stringify(entry)).digest('hex').slice(0,24);
   if(seen[r.id]===sig)continue;
   try{await this.post(entry);seen[r.id]=sig;changed=true;out.push({id:r.id,status:entry.status});}catch{out.push({id:r.id,status:'not_logged'});}
  }
  if(changed){const records=this.calls.read().records;for(const k of Object.keys(seen))if(!records.some(r=>r.id===k&&now-(r.attemptedAt??0)<=30*86400000))delete seen[k];atomic(this.file(),seen);}
  return out;
 }
}
