import {definePluginEntry} from 'openclaw/plugin-sdk/plugin-entry';
import {callGatewayFromCli} from 'openclaw/plugin-sdk/gateway-runtime';
import {registerVoice} from './core.mjs';
import {Assignments,registerAssignments} from './assignments.mjs';
import {Reports,registerReports} from './reports.mjs';
import {readCallRecord} from './call-records.mjs';
import {CallActivity} from './activity.mjs';
import {MenuMemory} from './callflow.mjs';
import {withConnectorGate} from './connector-gate.mjs';
export default definePluginEntry({id:'procurator-voice',name:'PROCURATOR Phone Assignments',description:'Scoped phone briefs and native owner approval; no model calling authority.',register(api){
 api=withConnectorGate(api,{plugin:'procurator-voice'});   // 0.25.0: declared tools only; phone briefs checked for health details
 const gateway=(method,params,wait=false)=>callGatewayFromCli(method,{json:true,timeout:wait?'70000':'30000',expectFinal:wait},params,{progress:false});
 const calls=new Assignments('/home/node/.openclaw/procurator-voice',gateway);
 const reports=new Reports(calls,id=>readCallRecord('/home/node/.openclaw/voice-calls/state/openclaw.sqlite',id));
 calls.reports=reports;
 reports.summaryModel=api.config?.plugins?.entries?.['voice-call']?.config?.reports?.summaryModel||api.config?.agents?.defaults?.model?.primary;
 // Phone-menu key paths that reached a person, per number (offered to the next call to that number).
 calls.menus=reports.menus=new MenuMemory('/home/node/.openclaw/procurator-voice');
 // Fallback when a call summary cannot be generated: wake the main assistant in their Telegram chat.
 reports.wakeMain=async(text,contextKey)=>{
  const sys=api.runtime?.system;
  if(typeof sys?.captureSessionEventTarget!=='function'||typeof sys?.enqueueSessionEvent!=='function')throw new Error('Cannot wake the assistant');
  const options={agentId:'main',sessionKey:'agent:main:main',contextKey,deliveryContext:{channel:'telegram',to:'100000001'}};
  const expectedTarget=await sys.captureSessionEventTarget('main','agent:main:main');
  const admission=await sys.enqueueSessionEvent(text,{...options,expectedTarget}).accepted;
  if(admission?.ok!==true)throw new Error('Not admitted');
 };
 // Nov 5 v2: listen-in calls ring their cell as a normal approved call; no inbound watcher (stop one left from 0.8.0).
 globalThis[Symbol.for('owner.voice.bridge')]?.stop?.();
 registerVoice(api,calls);registerAssignments(api,calls);registerReports(api,calls,reports);
 // One recovery timer per process, replaced when the plugin registry reloads.
 const KEY=Symbol.for('owner.voice.reportSweep');
 if(globalThis[KEY])clearInterval(globalThis[KEY]);
 let busy=false;
 // Activity log (Nov 5): every ended call gets one entry in the assistant's log, updated when its summary arrives.
 const activity=new CallActivity(calls,reports);
 const tick=()=>{if(busy)return;busy=true;reports.sweepTranscripts().catch(()=>{}).then(()=>reports.sweep()).catch(()=>{}).then(()=>activity.sweep()).catch(()=>{}).finally(()=>{busy=false;});};
 globalThis[KEY]=setInterval(tick,60000);globalThis[KEY].unref?.();
 setTimeout(tick,30000).unref?.();
}});
