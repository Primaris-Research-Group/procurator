// Listen-in calls (Nov 5): after the owner approves a listen_in assignment, the assistant does not dial. The owner calls
// the assistant's line from their cell, adds the destination themselves and merges (three-way), then listens muted.
// This watcher runs only while such an approval is waiting: it finds their incoming call (caller ID = their saved
// cell number, started after the approval), binds it to the approved brief and steers the live voice.
import {bridgeGuidance} from './assignments.mjs';

export class BridgeWatch{
 constructor(calls,readRecord,{now=()=>Date.now(),setTimer=(f,ms)=>setInterval(f,ms),clearTimer=t=>clearInterval(t),every=2000}={}){
  Object.assign(this,{calls,readRecord,now,setTimer,clearTimer,every});this.timer=null;this.busy=false;
 }
 waiting(){try{return this.calls.read().records.filter(r=>r.kind==='assignment'&&r.status==='bridge_waiting');}catch{return [];}}
 arm(){if(!this.timer&&this.waiting().length){this.timer=this.setTimer(()=>{this.check().catch(()=>{});},this.every);this.timer?.unref?.();}}
 stop(){if(this.timer){this.clearTimer(this.timer);this.timer=null;}}
 async check(){
  if(this.busy)return null;this.busy=true;
  try{
   const j=this.calls.read();let changed=false;
   for(const r of j.records)if(r.kind==='assignment'&&r.status==='bridge_waiting'&&this.now()>r.bridgeUntil){r.status='bridge_expired';changed=true;}
   if(changed)this.calls.save(j);
   const waiting=j.records.filter(r=>r.kind==='assignment'&&r.status==='bridge_waiting');
   if(!waiting.length){this.stop();return null;}
   const owner=this.calls.settings().testNumber;
   const bound=new Set(j.records.map(r=>r.callId).filter(Boolean));
   const status=await this.calls.gateway('voicecall.status',{});
   const active=(Array.isArray(status?.calls)?status.calls:[]).filter(c=>c?.direction==='inbound'&&!Number.isFinite(c.endedAt)&&typeof c.callId==='string'&&!bound.has(c.callId));
   for(const c of active){
    const r=waiting.find(w=>Number.isFinite(c.startedAt)&&c.startedAt>=w.approvedAt-5000);
    if(!r)continue;
    const record=await this.readRecord(c.callId);
    if(!record||record.from!==owner||typeof record.sessionKey!=='string')continue;
    const latest=this.calls.read(),saved=this.calls.find(latest,r.id);
    if(saved.status!=='bridge_waiting')continue;
    Object.assign(saved,{status:'initiated',callId:c.callId,attemptedAt:this.now(),bridgeSessionKey:record.sessionKey.toLowerCase()});
    this.calls.save(latest);
    for(const message of bridgeGuidance(saved)){try{await this.calls.gateway('voicecall.steer',{callId:c.callId,mode:'guidance',message});}catch{}}
    if(!this.waiting().length)this.stop();
    return {bound:saved.id,callId:c.callId};
   }
   return null;
  }finally{this.busy=false;}
 }
}
