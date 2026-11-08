// Usage meter (Nov 5 2026, from Muse's usage meter). Counts model tokens per day from the gateway's
// llm_output hook, split by where the work came from (their chat, mission sessions, phone calls, other).
// Best effort: if this OpenClaw build does not report usage on that hook, /meter says so plainly.
import path from 'node:path';
import {readJson,writeJson} from './preferences.mjs';
import {taskFor} from './taskbind.mjs';

const TZ='America/New_York';
const day=t=>new Intl.DateTimeFormat('en-CA',{timeZone:TZ,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(t));
const num=v=>Number.isFinite(v)&&v>=0?v:0;

export function readUsage(u){
 if(!u||typeof u!=='object')return null;
 const input=num(u.input??u.input_tokens??u.inputTokens??u.prompt_tokens),output=num(u.output??u.output_tokens??u.outputTokens??u.completion_tokens);
 const cacheRead=num(u.cacheRead??u.cache_read_input_tokens??u.cacheReadTokens),cacheWrite=num(u.cacheWrite??u.cache_creation_input_tokens??u.cacheWriteTokens);
 const cost=num(u.cost?.total??u.costUsd??u.cost_usd);
 if(!input&&!output&&!cacheRead&&!cacheWrite)return null;
 return {input,output,cacheRead,cacheWrite,cost};
}
export function source(ctx){
 const k=String(ctx?.sessionKey||''),a=String(ctx?.agentId||'');
 if(a==='phone'||/:phone:/.test(k))return 'phone';
 if(/mission/.test(String(ctx?.contextKey||''))||/mission/.test(k))return 'missions';
 if(a==='main')return 'chat';
 return 'other';
}
// Packet (mission) slug from a work-session context key such as procurator-mission:bg:1762440000000 (Nov 6).
export function missionOf(ctx){
 const text=String(ctx?.contextKey||'')+' '+String(ctx?.sessionKey||'');
 const m=/procurator-mission:([a-z0-9-]{2,24})(?::|\s|$)/.exec(text)||/agent:main:mission-([a-z0-9-]{2,24})(?:\s|$)/.exec(text);
 return m?m[1]:null;
}

export class UsageMeter{
 constructor(directory,now=()=>Date.now()){this.file=path.join(directory,'usage.json');this.now=now;}
 read(){const d=readJson(this.file,()=>({version:1,days:{}}),400000);if(d.version!==1||typeof d.days!=='object')throw new Error('Usage storage needs operator inspection.');return d;}
 add(usage,src='other',model='',mission=null){
  const u=readUsage(usage);if(!u)return false;
  const d=this.read(),key=day(this.now()),t=d.days[key]??={total:{calls:0,input:0,output:0,cacheRead:0,cacheWrite:0,cost:0},by:{}};
  const buckets=[t.total,t.by[src]??={calls:0,input:0,output:0,cacheRead:0,cacheWrite:0,cost:0}];
  if(mission&&/^[a-z0-9-]{2,24}$/.test(mission)){t.by_mission??={};buckets.push(t.by_mission[mission]??={calls:0,input:0,output:0,cacheRead:0,cacheWrite:0,cost:0});}
  for(const bucket of buckets){bucket.calls++;for(const f of ['input','output','cacheRead','cacheWrite','cost'])bucket[f]+=u[f];}
  if(model)t.model=String(model).slice(0,60);
  this.trim(d);writeJson(this.file,d);return u;
 }
 // A finished build's agent usage (reported by the builder), kept apart from model replies (Nov 6).
 addBuild(cost,mission=null){
  const c=num(cost);if(!c)return false;
  const d=this.read(),key=day(this.now()),t=d.days[key]??={total:{calls:0,input:0,output:0,cacheRead:0,cacheWrite:0,cost:0},by:{}};
  const b=t.builds??={count:0,cost:0};b.count++;b.cost+=c;
  if(mission&&/^[a-z0-9-]{2,24}$/.test(mission)){t.by_mission??={};const m=t.by_mission[mission]??={calls:0,input:0,output:0,cacheRead:0,cacheWrite:0,cost:0};m.cost+=c;}
  this.trim(d);writeJson(this.file,d);return true;
 }
 trim(d){const keys=Object.keys(d.days).sort();for(const k of keys.slice(0,Math.max(0,keys.length-120)))delete d.days[k];}
 summary(days=7){
  const d=this.read(),keys=Object.keys(d.days).sort().slice(-days);
  if(!keys.length)return null;
  const sum=(list)=>list.reduce((a,b)=>{for(const f of ['calls','input','output','cacheRead','cacheWrite','cost'])a[f]+=b?.[f]||0;return a;},{calls:0,input:0,output:0,cacheRead:0,cacheWrite:0,cost:0});
  const today=d.days[day(this.now())];
  const by={};for(const k of keys)for(const [s,v] of Object.entries(d.days[k].by||{}))by[s]=sum([by[s],v]);
  const byMission={};for(const k of keys)for(const [s,v] of Object.entries(d.days[k].by_mission||{}))byMission[s]=sum([byMission[s],v]);
  const builds=keys.reduce((a,k)=>{const b=d.days[k].builds;return b?{count:a.count+(b.count||0),cost:a.cost+(b.cost||0)}:a;},{count:0,cost:0});
  return {today:today?.total||null,period:sum(keys.map(k=>d.days[k].total)),by,by_mission:byMission,builds,days:keys.length,model:d.days[keys.at(-1)].model};
 }
}

const k=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n);
export function usageText(s,composer=null){
 if(!s&&composer?.calls)return `Assistant usage: the gateway meter has nothing yet.\n• Packet Composer: ${composer.calls} model repl${composer.calls===1?'y':'ies'} · about $${composer.cost.toFixed(2)}\n\nFull dashboard: /panel`;
 if(!s)return 'No usage recorded yet. The meter counts from the first model reply after this deploy; if it stays empty, this OpenClaw version does not report usage to plugins (its own /status shows the current session).';
 const line=t=>t?`${t.calls} model repl${t.calls===1?'y':'ies'} · ${k(t.input+t.cacheRead+t.cacheWrite)} tokens in (${k(t.cacheRead)} from cache) · ${k(t.output)} out`+(t.cost?` · about $${t.cost.toFixed(2)}`:''):'nothing yet';
 const names={chat:'Your chat',missions:'Mission work sessions',phone:'Phone calls (summaries)',other:'Other'};
 const missions=Object.entries(s.by_mission||{}).sort((a,b)=>b[1].cost-a[1].cost).slice(0,8);
 return ['Assistant usage (Eastern days):','Today: '+line(s.today),`Last ${s.days} day${s.days===1?'':'s'}: `+line(s.period),'',
  ...Object.entries(s.by).sort((a,b)=>(b[1].input+b[1].output)-(a[1].input+a[1].output)).map(([n,t])=>'• '+(names[n]||n)+': '+line(t)),
  ...(missions.length?['','By packet:',...missions.map(([n,t])=>'• '+n+': '+line(t))]:[]),
  ...(composer?.calls?[`• Packet Composer: ${composer.calls} model repl${composer.calls===1?'y':'ies'} · about $${composer.cost.toFixed(2)} (broker-side; included on the Usage tab)`]:[]),
  ...(s.builds?.count?['',`Builds: ${s.builds.count}, about $${s.builds.cost.toFixed(2)} of coding-agent usage`]:[]),
  ...(s.model?['','Model: '+s.model]:[]),...(s.period.cost?[]:['','Cost is not reported by the gateway; check console.anthropic.com for billing.']),
  '','Full dashboard: /panel'].join('\n');
}

export function registerUsage(api,meter,ownerContext,{call}={}){
 // call: broker(action,args,privileged). A packet work session's cost is also charged to that packet's budget (mission_spend).
 try{api.on?.('llm_output',(event,ctx)=>{try{
  const c={...ctx,contextKey:event?.contextKey??ctx?.contextKey};const src=source(c),mission=src==='missions'?missionOf(c):null;
  const u=meter.add(event?.usage??event?.lastAssistant?.usage??event?.message?.usage,src,event?.model??event?.lastAssistant?.model,mission);
  if(u&&mission&&u.cost>0&&call)call('mission_spend',{mission,cost_usd:u.cost},true).catch(()=>{});
  // Per-task model cost (0.17.0): replies in a conversation that is working a browser task are charged to that task.
  const task=u&&call?taskFor(c.sessionKey):null;
  if(task)call('task_cost',{id:task,cost_usd:Math.min(49,Number(u.cost)||0),tokens:Math.min(4_999_999,Math.round(u.input+u.output+u.cacheRead+u.cacheWrite)),model:String(event?.model??'').slice(0,60)},true).catch(()=>{});
 }catch{}},{registrationId:'procurator-usage-meter'});}catch{}
 api.registerCommand({name:'meter',description:'How much the assistant has used (tokens, by chat / missions / calls): /meter or /meter 30',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);let n=Number((ctx.args||'').trim()||7);n=Number.isInteger(n)&&n>=1&&n<=120?n:7;
  // 0.24.3 ($13-01): the Packet Composer calls the model from the broker, outside this meter; add its spend like the Usage tab does.
  let comp=null;if(call)try{const d=(await call('composer_daily',{days:n},true))?.days||{};const v=Object.values(d);if(v.length)comp={calls:v.reduce((a,x)=>a+(x.calls||0),0),cost:v.reduce((a,x)=>a+(x.cost||0),0)};}catch{}
  return {text:usageText(meter.summary(n),comp)};
 }catch(e){return {text:String(e?.message||e)};}}});
}
