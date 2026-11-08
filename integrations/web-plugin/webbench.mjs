import {ownerId} from './owner-config.mjs';
// /webbench (web 0.20.6, Nov 7): the consumer web battery. The owner starts a run from Telegram; each case goes to the
// assistant in its own background conversation (agent:main:webbench-RUN-CASE-LANE), one at a time, on the cloud browser
// and/or their PC browser. The broker (web/bench.py) fixes the browser per case, lets READ-ONLY tasks start under their run,
// withholds every other approval, captures the pages and grades each case; a report comes to Telegram at the end.
import {awaitRun,retireSession} from './directrun.mjs';

const OWNER=ownerId();
const HELP=`Web test battery (/webbench): the assistant does real consumer errands on the web by itself (flights, hotels, a reservation it must stop before, rental cars, price comparison, research, store hours, a bank it must refuse) and each one is graded.
/webbench            quick set (7 cases) on both browsers, about 30-60 min
/webbench full       all 15 cases on both browsers, about 2-4 hours
/webbench cloud | home   only that browser (add quick or full)
/webbench C05 C07    just those cases     ·  /webbench compat   the 15-site compatibility set
/webbench list · /webbench last (latest report) · /webbench stop
Read-only research starts under your /webbench command; anything that would book, buy, sign in or submit is never approved by a test (it must stop and say so). Don't use the assistant's browser while a test runs.`;

export function parseBench(text){
 const words=String(text||'').trim().split(/\s+/).filter(Boolean).map(w=>w.toLowerCase());
 const out={lanes:'both',suite:'quick',set:'consumer',cases:[]};
 for(const w of words){
  if(['cloud','home','both'].includes(w))out.lanes=w;
  else if(['quick','full'].includes(w))out.suite=w;
  else if(w==='compat'){out.set='compat';}
  else if(/^[cw]\d{2}$/.test(w))out.cases.push(w.toUpperCase());
  else return {error:`I don't know "${w}". `+HELP.split('\n').slice(1,6).join('\n')};
 }
 if(out.cases.some(c=>c.startsWith('W')))out.set='compat';
 return out;
}

// Runs every case of a started run, in order. Resolves with the final report text.
export async function runBench(call,gateway,notify,run,{wait=awaitRun,shouldStop=()=>false,retire=retireSession}={}){
 for(const p of run.plan){
  if(shouldStop())break;
  let begin;
  try{begin=await call('bench_case_begin',{run:run.run,case:p.case,lane:p.lane},true);}
  catch(e){await notify(`Web test ${run.run}: stopped before ${p.case} (${String(e?.message||e).slice(0,160)}).`,`${run.run}:halt`);break;}
  let answer='',error='';
  try{
   const first=await gateway('agent',{agentId:'main',sessionKey:begin.session_key,message:begin.prompt,deliver:false,
    idempotencyKey:`procurator-webbench:${run.run}:${p.case}:${p.lane}`},60000);
   const done=await wait(gateway,first,{timeoutMs:begin.timeout_seconds*1000,sessionKey:begin.session_key});
   answer=String(done?.text||'');
  }catch(e){error=String(e?.message||e).slice(0,300);}
  let res;
  try{res=await call('bench_case_end',{run:run.run,case:p.case,lane:p.lane,answer,error},true);}
  catch(e){res={outcome:'Error',why:String(e?.message||e)};}
  // 0.20.7: the case's conversation is finished; deleting it retires its MCP helper processes (see retireSession).
  await retire(gateway,begin.session_key);
  const icon={Pass:'✅',Partial:'🟡',Blocked:'🧱',Fail:'❌',Unsafe:'⛔',Error:'⚠️'}[res.outcome]||'•';
  await notify(`${icon} ${p.case} (${p.lane}): ${res.outcome}${res.why&&res.outcome!=='Pass'?' — '+String(res.why).slice(0,120):''}`,`${run.run}:${p.case}:${p.lane}`).catch(()=>{});
  if(res.outcome==='Unsafe'){await notify(`⛔ Web test ${run.run} stopped: ${p.case} was UNSAFE (a gate did not hold). Don't use the browser for real errands until this is fixed.`,`${run.run}:unsafe`).catch(()=>{});
   await call('bench_stop',{run:run.run},true).catch(()=>{});break;}
 }
 const rep=await call('bench_report',{run:run.run},true);
 await notify(rep.text,`${run.run}:report`).catch(()=>{});
 return rep.text;
}

export function registerWebBench(api,call,ownerContext,{gateway}={}){
 const notify=(message,key)=>gateway('send',{channel:'telegram',to:OWNER,message,idempotencyKey:'procurator-webbench:'+key});
 let running=null,stop=false;
 api.registerCommand({name:'webbench',description:'Web test battery: /webbench [quick|full] [cloud|home|both] [C01 C05…] | list | last | stop',channels:['telegram'],requireAuth:true,acceptsArgs:true,
  async handler(ctx){try{
   ownerContext(ctx);
   const a=(ctx.args||'').trim().toLowerCase();
   if(a==='help'||a==='?')return {text:HELP};
   if(a==='list'){const r=await call('bench_list',{},true);
    return {text:'Web test cases ('+r.set+'):\n'+r.cases.map(c=>`${c.id} ${c.name.slice(4)} · ${c.site} · ${c.lanes==='none'?'no browser':c.lanes} · ${c.suites.includes('quick')?'quick+full':'full'}`).join('\n')};}
   if(a==='last'||a==='report')return {text:(await call('bench_report',{},true)).text};
   if(a==='stop'){stop=true;const r=await call('bench_stop',{},true);return {text:r.stopped?`Web test ${r.run} stopped; its browsers are closed. /webbench last shows what finished.`:r.note};}
   if(!gateway)return {text:'The web test runner is not available on this gateway.'};
   if(running)return {text:'A web test is already running (/webbench stop ends it).'};
   const opts=parseBench(a);if(opts.error)return {text:opts.error};
   const run=await call('bench_start',opts,true);
   stop=false;
   running=runBench(call,gateway,notify,run,{shouldStop:()=>stop}).catch(e=>notify('Web test '+run.run+' failed: '+String(e?.message||e).slice(0,200),run.run+':crash').catch(()=>{})).finally(()=>{running=null;});
   const browsers={cloud:'the cloud browser',home:'your PC browser',both:'the cloud browser and your PC browser'}[run.lanes];
   return {text:`Web test ${run.run} started: ${run.plan.length} runs (${[...new Set(run.plan.map(p=>p.case))].join(', ')}) on ${browsers}, about ${run.estimated_minutes} min at most.${run.notes?.length?' '+run.notes.join(' '):''}\nOne line per case comes here, then the full report. Read-only research is approved by this command; nothing is booked, bought or submitted. Don't use the assistant's browser meanwhile. /webbench stop ends it.`};
  }catch(e){return {text:String(e?.message||e)};}}});
}
