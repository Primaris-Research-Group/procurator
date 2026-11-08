import {definePluginEntry} from 'openclaw/plugin-sdk/plugin-entry';
import {callGatewayFromCli} from 'openclaw/plugin-sdk/gateway-runtime';
import {chatRunner} from './directrun.mjs';
import {registerRecall} from './recall.mjs';
import {withInventory,registerSupport} from './support.mjs';
import {registerWeb,ownerContext,broker} from './register.mjs';
import {Workflows,registerWorkflows} from './workflow.mjs';
import {OwnerProfile,registerProfile} from './profile.mjs';
import {Welcome,registerWelcome} from './welcome.mjs';
import {registerProjectSources} from './project-sources.mjs';
import {registerNews} from './news.mjs';
import {registerCapabilities} from './capabilities.mjs';
import {OwnerPreferences,registerPreferences} from './preferences.mjs';
import {MorningBrief,registerBrief,Reminders,registerReminders} from './brief.mjs';
import {registerMissions} from './missions.mjs';
import {registerActivity,mailBroker} from './activity.mjs';
import {UsageMeter,registerUsage} from './usage.mjs';
import {registerSitePolicy,registerSigninWake} from './sites.mjs';
import {registerCalculators} from './calculators.mjs';
import {registerEvidence} from './evidence.mjs';
import {Builds,registerBuilds} from './builds.mjs';
import {ModelSwitch,registerModelSwitch,PRESETS} from './models.mjs';
import {packetRunner} from './missions.mjs';
import {registerDashboard} from './dashboard.mjs';
import {registerDocRead} from './docread.mjs';
import {registerWebExtras} from './webextras.mjs';
import {registerWebBench} from './webbench.mjs';
import {registerHealth,usedHealthTools} from './health.mjs';
import {withConnectorGate} from './connector-gate.mjs';
import {registerConnectors,registerMcpRun} from './connectors.mjs';
import {registerUrgentCare} from './urgent.mjs';
import {registerLookups} from './lookups.mjs';
import {registerSocial} from './social.mjs';
import {registerHome} from './home.mjs';
import {registerLibrary} from './library.mjs';
import {registerAccounts} from './accounts.mjs';
import {registerCoach} from './coach.mjs';
export default definePluginEntry({id:'procurator-web',name:'PROCURATOR Web and Administrative Workflows',description:'Private workflow checkpoints, bounded owner-approved browser tasks and exact final-action approvals.',register(api){
 // 0.20.0: record every tool and command this plugin registers, for the manual (support.mjs).
 // 0.25.0: every tool passes the connector gate (declared in a manifest; outbound tools checked for health details).
 api=withConnectorGate(api,{plugin:'procurator-web'});
 const inventory=withInventory(api);api=inventory.api;
 registerWeb(api);registerWorkflows(api,new Workflows('/home/node/.openclaw/procurator-admin'));
 const ownerProfile=new OwnerProfile('/home/node/.openclaw/procurator-admin');
 registerProfile(api,ownerProfile,ownerContext);
 registerWelcome(api,new Welcome('/home/node/.openclaw/procurator-admin',ownerProfile),ownerContext);   // 0.30.0: first-run interview, name + pronouns
 registerProjectSources(api,broker,ownerContext);
 // Whole Drive files and Gmail attachments in pages (Nov 6): docs_get stops at 64 KB and drive_get gives only metadata.
 registerDocRead(api,broker);
 registerNews(api);registerCalculators(api);registerEvidence(api);
 // 0.25.0: places_search and npi_lookup (verified business and clinician listings before a call or booking).
 registerLookups(api,broker);
 registerSocial(api,broker,ownerContext);   // 0.27.0: public posts (/postsend), media, connections, inbound events
 registerHome(api,broker,ownerContext);     // 0.28.0: home policy (/homeapprove, /homeoff, /homegrant), devices, /livereveal
 registerAccounts(api,broker,ownerContext);  // 0.29.0: Apple account tools (calendar_apple_*, contacts_apple_search, accounts_list)
 registerLibrary(api,broker,ownerContext);  // 0.28.0: reference library the owner uploads (library_*), pinned project context
 registerCapabilities(api,broker,undefined,ownerContext);
 registerPreferences(api,new OwnerPreferences('/home/node/.openclaw/procurator-admin'),ownerContext);
 const brief=new MorningBrief('/home/node/.openclaw/procurator-admin',api);
 registerBrief(api,brief,ownerContext);
 registerReminders(api,new Reminders('/home/node/.openclaw/procurator-admin',brief));
 const gateway=(method,params,timeoutMs=30000)=>callGatewayFromCli(method,{json:true,timeout:String(timeoutMs)},params,{progress:false});
 // 0.19.2: brief, reminders, mission and sign-in wakes run the assistant directly in their conversation (see directrun.mjs).
 const chat=chatRunner(gateway);brief.runner=chat;
 // Phase 3 (Nov 6): a packet with its own model (Model: opus, /mission model, dashboard) runs in its own session; their chat keeps the /ai model.
 registerMissions(api,broker,ownerContext,{wake:(text,key,reason)=>brief.wake(text,key,reason),mail:mailBroker,runAgent:packetRunner(gateway,PRESETS,broker),
  notify:message=>gateway('send',{channel:'telegram',to:'100000001',message,idempotencyKey:'procurator-mission-model:'+Date.now()})});
 registerSigninWake(broker,(text,key,reason)=>brief.wake(text,key,reason));
 registerActivity(api,broker,ownerContext);
 const meter=new UsageMeter('/home/node/.openclaw/procurator-admin');
 registerUsage(api,meter,ownerContext,{call:broker});
 registerSitePolicy(api,broker,ownerContext);
 // Builds on the owner's Mac (Nov 5): packet -> /buildapprove -> Mac builder pulls it -> draft PR. Telegram notices are plain sends.
 const builds=registerBuilds(api,new Builds('/home/node/.openclaw/procurator-build',gateway),ownerContext);
 // A finished build (Nov 6): its coding-agent cost goes to the usage meter and to its packet's budget; the PR becomes step evidence.
 builds.onFinish=async(r,rep)=>{
  try{meter.addBuild(rep.cost_usd,r.mission||null);}catch{}
  if(r.mission&&rep.cost_usd>0)await broker('mission_spend',{mission:r.mission,cost_usd:rep.cost_usd},true).catch(()=>{});
  if(r.mission&&r.step){const [thread,step_id]=r.step.split('/');
   const evidence=[`build ${r.id} ${rep.status}`,...(rep.pr_url?[rep.pr_url]:[]),...(rep.tests?.status?['tests '+rep.tests.status]:[])].slice(0,4);
   if(rep.status==='done')await broker('mission_thread',{mission:r.mission,thread,action:'update',step_id,status:'done',evidence,note:(rep.summary||'').slice(0,600)},true).catch(()=>{});
   else await broker('mission_thread',{mission:r.mission,thread,action:'fail',step_id,reason:`build ${r.id} ${rep.status}: ${(rep.message||'no reason given').slice(0,300)}`,transient:false,notify:false},true).catch(()=>{});
   await broker('mission_wake',{mission:r.mission,reason:`build ${r.id} ${rep.status}`},true).catch(()=>{});}
 };
 // A build prepared for a packet step marks that step as waiting for /buildapprove (Phase 3).
 const prepare=builds.prepare.bind(builds);
 builds.prepare=async a=>{const out=await prepare(a);
  if(a?.mission&&a?.step&&out?.id&&out.status==='awaiting_approval'){const [thread,step_id]=String(a.step).split('/');
   await broker('mission_thread',{mission:String(a.mission).toLowerCase(),thread,action:'update',step_id,status:'waiting_for_approval',awaiting:'build:'+out.id},true).catch(()=>{});}
  return out;};
 // /dashboard: single-use sign-in link for the private dashboard (web/dashboard.py).
 registerDashboard(api,broker,ownerContext);
 // /ai: switch the main assistant's AI model from Telegram (checked, with a fallback; phone agent unchanged).
 const modelSwitch=new ModelSwitch(gateway,{stateDir:'/home/node/.openclaw/procurator-admin'});registerModelSwitch(api,modelSwitch,ownerContext,broker);
 setTimeout(()=>{void modelSwitch.resume()?.catch?.(()=>{});},5000).unref?.();   // 0.24.3: send Ready after a model-switch restart   // 0.24.0: also /ai packets MODEL and /ai composer MODEL
 // 0.18.0: page-vision checks, owner-fact keys, uploads, watch-only live view, router/adapters, two-speed task loop.
 registerWebExtras(api,broker,ownerContext,{gateway,chat});
 // 0.20.6: /webbench, the consumer web battery (web/bench.py grades; cases in web/bench/*.json).
 registerWebBench(api,broker,ownerContext,{gateway});
 // 0.21.0 (Nov 7): consumer health module — grants, effects, receipts; health turns stay out of memory (recall.mjs).
 registerHealth(api,broker,ownerContext,{gateway,wake:(text,key,reason)=>brief.wake(text,key,reason)});
 // 0.25.0 (F5): a fixed 911/988 notice when their message describes an emergency; health tasks pause for that turn.
 registerUrgentCare(api,{gateway});
 // 0.26.0: optional health and wellness coach (/coach on): questionnaire, calorie plan, energy balance, metrics, fixed-text reminders.
 registerCoach(api,broker,ownerContext,{gateway,wake:(text,key,reason)=>brief.wake(text,key,reason)});
 // 0.20.0: conversation memory (recall.mjs + broker recall.py) and the agent support module (manual, identity, /manual).
 registerRecall(api,broker,ownerContext,{exclude:usedHealthTools});
 registerSupport(api,inventory.inv,ownerContext);
 // 0.25.0: /connectors — what is connected, each connector's tools by risk, and any manifest problems.
 registerConnectors(api,broker,ownerContext);
 // 0.25.0: /mcprun — tasks queued by Claude Desktop / Claude Code / ChatGPT through bin/procurator-mcp.py start only when the owner says so.
 registerMcpRun(api,broker,ownerContext,(text,key,reason)=>brief.wake(text,key,reason));
 // Tap-to-approve buttons need OpenClaw's plugin interactive handler; log what this gateway offers (deploy check reads it).
 try{console.log('[procurator-web] interactive api: '+(Object.keys(api).filter(k=>/interactive|callback|button/i.test(k)).join(',')||'none'));}catch{}
}});
