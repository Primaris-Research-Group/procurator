import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {formatCallStatus} from './presentation.mjs';
import {voiceSnapshot,voiceProcessing,assertVoiceCurrent} from './voice-mode.mjs';

export const PURPOSE = 'One two-way voice test with the owner. Introduce yourself as their AI assistant and ask whether the owner can hear you. No administrative tasks, private disclosures, purchases, or commitments.';
export const OPENING = "Hello the owner. I'm your AI administrative assistant. This is our phone connection test. Can you hear me clearly?";
export function ownerContext(ctx) {
  if(ctx.channel!=='telegram'||ctx.isAuthorizedSender!==true||String(ctx.senderId).replace(/^telegram:/,'')!=='100000001'||ctx.senderIsOwner===false)
    throw new Error('Only the authenticated Telegram owner can control phone tests.');
  ctx.assertOwnerCurrent?.();
}
export const digest = v => crypto.createHash('sha256').update(JSON.stringify(v)).digest('hex').slice(0,16);
export function atomic(file,value) {
  const tmp=file+'.'+crypto.randomBytes(8).toString('hex')+'.tmp';
  let fd;
  try {fd=fs.openSync(tmp,'wx',0o600);fs.writeFileSync(fd,JSON.stringify(value));fs.fsyncSync(fd);fs.closeSync(fd);fd=undefined;fs.renameSync(tmp,file);const dirfd=fs.openSync(path.dirname(file),'r');try{fs.fsyncSync(dirfd);}finally{fs.closeSync(dirfd);}}
  finally {if(fd!==undefined)fs.closeSync(fd);fs.rmSync(tmp,{force:true});}
}
export class Tests {
  constructor(directory, gateway, now=()=>Date.now()) {
    this.directory=directory;this.gateway=gateway;this.now=now;
    fs.mkdirSync(directory,{recursive:true,mode:0o700});
    this.file=path.join(directory,'tests.json');
  }
  read(){return fs.existsSync(this.file)?JSON.parse(fs.readFileSync(this.file,'utf8')):{records:[]};}
  save(journal){atomic(this.file,journal);}
  settings(){
    const s=JSON.parse(fs.readFileSync(path.join(this.directory,'settings.json'),'utf8'));
    if(!/^\+1[2-9]\d{2}[2-9]\d{6}$/.test(s.testNumber)||s.maxDurationSeconds!==60)
      throw new Error('Phone test settings are not ready.');
    return s;
  }
  verify(r){if(!r||r.hash!==digest(r.plan))throw new Error('Phone test record is invalid. Request a fresh review.');}
  prepare(){
    const settings=this.settings(),j=this.read();
    const existing=j.records.find(r=>['attempting','uncertain','initiated','hangup_requested'].includes(r.status));
    if(existing)throw new Error('A previous call may still exist. Use /callstatus before requesting another test.');
    const pending=j.records.find(r=>r.status==='pending'&&r.expiresAt>this.now());
    if(pending){this.verify(pending);return pending;}
    for(const r of j.records)if(r.status==='pending')r.status='expired';
    if(j.records.length>=100)throw new Error('Phone test journal limit reached; ask Codex to inspect it.');
    const plan={to:settings.testNumber,purpose:PURPOSE,opening:OPENING,maxDurationSeconds:60,recording:false,...voiceSnapshot(settings)};
    const r={id:crypto.randomBytes(8).toString('hex'),plan,hash:digest(plan),status:'pending',expiresAt:this.now()+600000};
    j.records.push(r);this.save(j);return r;
  }
  review(r){this.verify(r);return `PHONE TEST REVIEW — ${r.id}\nDestination: ${r.plan.to}\nPurpose: ${r.plan.purpose}\nOpening: ${r.plan.opening}\nMaximum: 60 seconds. Audio recording: off. ${voiceProcessing(r.plan)}\nMetered Twilio and AI-provider charges apply; this is not a price cap.\nApprove within 10 minutes by sending this ENTIRE plain-text line:\n/callapprove ${r.id} ${r.hash}\nCancel: /callcancel ${r.id}`;}
  find(j,id){if(!/^[a-f0-9]{16}$/.test(id??''))throw new Error('Use a test ID from its review.');const r=j.records.find(x=>x.id===id);this.verify(r);return r;}
  async approve(id,hash,assertOwner=()=>{}){
    assertOwner();const j=this.read(),r=this.find(j,id);
    if(hash!==r.hash)throw new Error('Approval does not match this exact phone test review.');
    if(r.status!=='pending')return {id,status:r.status};
    if(r.expiresAt<=this.now()){r.status='expired';this.save(j);throw new Error('Phone test review expired. Send /calltest for a fresh review.');}
    if(r.plan.to!==this.settings().testNumber)throw new Error('The test destination changed. Request a fresh review.');
    assertVoiceCurrent(r.plan,this.settings());
    assertOwner();r.status='attempting';r.attemptedAt=this.now();this.save(j);
    try {
      const result=await this.gateway('voicecall.initiate',{to:r.plan.to,message:r.plan.opening,mode:'conversation',sessionKey:`agent:phone:voice:test:${r.id}`,brief:{task:r.plan.purpose,maxDurationSeconds:50},...this.requesterSessionKey?{requesterSessionKey:this.requesterSessionKey}:{}});
      if(typeof result?.callId!=='string'||!result.callId||result.initiated!==true)throw new Error('No call receipt.');
      const latest=this.read(),saved=this.find(latest,id);saved.status='initiated';saved.callId=result.callId;this.save(latest);
      return {id,status:saved.status};
    }catch{
      const latest=this.read(),saved=this.find(latest,id);saved.status='uncertain';this.save(latest);
      return {id,status:'uncertain'};
    }
  }
  cancel(id){const j=this.read(),r=this.find(j,id);if(r.status!=='pending')throw new Error('This test is no longer pending. Use /callend to end an initiated call.');r.status='cancelled';this.save(j);return r;}
  async status(id){
    const j=this.read();const r=id?this.find(j,id):j.records.at(-1);
    if(!r)return {status:'no test prepared'};this.verify(r);
    const details={to:r.plan.to};
    if(!r.callId)return {id:r.id,status:r.status,...details};
    const result=await this.gateway('voicecall.status',{callId:r.callId});
    const call=result?.call;
    if(result?.found===true&&call?.callId===r.callId&&Number.isFinite(call.endedAt)){
      const latest=this.read(),saved=this.find(latest,r.id);saved.status='call_ended';saved.endedAt=call.endedAt;saved.carrierState=call.state;this.save(latest);
      return {id:r.id,status:saved.status,call:result,...details,...this.reports?{report:await this.reports.status(saved)}:{}};
    }
    return {id:r.id,status:r.status,call:result,...details,...this.reports?{report:await this.reports.status(r)}:{}};
  }
  async end(id){const j=this.read(),r=this.find(j,id);if(!r.callId)throw new Error('No call receipt is available. An uncertain placement must be inspected before another call.');await this.gateway('voicecall.end',{callId:r.callId});const latest=this.read(),saved=this.find(latest,id);saved.status='hangup_requested';this.save(latest);return saved;}
}

export function registerVoice(api,tests){
  api.registerTool?.((ctx)=>ctx.agentId==='phone'?{
    name:'phone_line_info',label:'Phone line information',description:'Read only the active session’s approved call brief or incoming message-taking limits. No account access or actions.',
    parameters:{type:'object',properties:{},additionalProperties:false},
    async execute(){return {content:[{type:'text',text:tests.phoneContext?.(ctx)??'This is a one-minute phone connection test with the owner’s AI assistant. Administrative actions and private account access are unavailable. Use Telegram for normal assistant tasks.'}]};}
  }:undefined,{optional:true});
  for(const name of ['calltest','callapprove','callcancel','callstatus','callend']){
    api.registerCommand({name,description:'Owner-only private phone connection test.',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){
      try {
        ownerContext(ctx);const args=ctx.args?.trim()??'';
        if(name==='calltest'){if(args)throw new Error('Send /calltest without arguments.');return {text:tests.review(tests.prepare())};}
        if(name==='callapprove'){
          const m=/^([a-f0-9]{16}) ([a-f0-9]{16})$/.exec(args);if(!m)throw new Error('Use the ENTIRE /callapprove line from the phone review, without backticks.');
          const r=await tests.approve(m[1],m[2],()=>ownerContext(ctx));
          if(r.listen_in)return {text:`Listen-in call ${r.id}: the assistant is calling your cell now. Answer without speaking, tap Add Call, dial the number, tap Merge Calls, then Mute. The steps are in the message above.`};
          return {text:r.status==='initiated'?`Phone call ${r.id}: dialing approved destination. Placement is confirmed; conversation outcome is not. Send /callstatus ${r.id} to inspect it. A private summary will be attempted after the call; /callstatus shows its delivery status.`:`Phone call ${r.id}: ${r.status}. Do not retry an uncertain call; ask Codex to inspect it.`};
        }
        if(name==='callcancel'){const r=tests.cancel(args);return {text:`Phone test ${r.id}: cancelled.`};}
        if(name==='callend'){const r=await tests.end(args);return {text:`Phone test ${r.id}: hangup requested.`};}
        const result=await tests.status(args);return {text:formatCallStatus(result)};
      }catch(error){return {text:error instanceof Error&&/^(Only the authenticated|Use |Send |A previous|Phone test|This test|The test|Approval|No call|Input|An uncertain)/.test(error.message)?error.message:'Phone test service did not confirm the operation. Ask Codex to inspect it; do not retry an uncertain call.'};}
    }});
  }
}
