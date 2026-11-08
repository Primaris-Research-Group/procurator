// Health and wellness coach, plugin side (0.26.0). Broker: integrations/web/health_coach.py. Optional: off until /coach on.
// The assistant gets health_coach_* tools; the owner gets /coach; a 5-minute timer sends the coach's fixed reminder texts
// (generic wording, never numbers or health details) straight to their Telegram, without a model.
const str=(description,maxLength=100)=>({type:'string',description,maxLength});
const schema=(properties,required=[])=>({type:'object',properties,required,additionalProperties:false});
const out=r=>({content:[{type:'text',text:JSON.stringify(r)}]});
const err=e=>({content:[{type:'text',text:JSON.stringify({error:String(e?.message||e)})}],isError:true});
const GOALS=['weight_loss','heart_health','track_calories','activity','stress','sleep','maintain'];
const SUPERVISED=['diabetes_meds','kidney','heart_failure','weight_meds','bariatric'];
const REMINDERS=['meals','exercise','weigh_in','stress','breathing','sleep','blood_pressure'];
const hhmm=str('HH:MM (24-hour) or "off".',5);
const STYLE='Coach style: warm, brief, practical. Never shame, never praise skipping meals or eating very little, never push a bigger deficit. '+
 'If the owner sounds distressed about food, weight or their body, stop coaching numbers and gently suggest support from their clinician. You do not diagnose or treat.';

export const COACH_TOOLS=[
 ['health_coach_status','Wellness coach (optional): on/off, their goals, reminder times, what the coach may not do for them (from their screening answers) and any caution. '+STYLE,'health_coach_status',schema({})],
 ['health_coach_questionnaire','The wellness questionnaire (goals, a few screening questions, reminder times). Ask the questions one or two at a time in plain words, accept "skip", never press on the screening questions; then send the answers with health_coach_answers.','health_coach_questionnaire',schema({})],
 ['health_coach_answers','Save their questionnaire answers. goals from the list; eating_disorder yes/no/skip; supervised = conditions the owner named (diabetes_meds = insulin or a sulfonylurea, kidney, heart_failure, weight_meds, bariatric) or empty; reminders = only the times the owner changed. Weight, height, birth date and the equation sex input go to health_nutrition_measure / health_nutrition_profile.','health_coach_answers',
  schema({goals:{type:'array',minItems:1,maxItems:7,items:{type:'string',enum:GOALS}},target_weight:{type:'number',minimum:30,maximum:900,description:'Optional goal weight.'},target_weight_unit:{type:'string',enum:['kg','lb']},
   pace:{type:'string',enum:['gentle','standard','faster'],description:'Weight-loss pace: about 250, 500 or 750 kcal/day (faster only with BMI 30+).'},activity_level:{type:'string',enum:['sedentary','light','moderate','very','extra']},
   eating_disorder:{type:'string',enum:['yes','no','skip']},supervised:{type:'array',maxItems:5,items:{type:'string',enum:SUPERVISED}},pregnant:{type:'string',enum:['yes','no','skip']},
   reminders:{type:'object',additionalProperties:false,description:'Only reminders the owner changed.',properties:Object.fromEntries(REMINDERS.map(k=>[k,hhmm]))},note:str('Anything else the owner wants the coach to know (no medical details needed).',300)},['goals','eating_disorder','supervised'])],
 ['health_coach_plan','Weight-loss calorie plan from their BMI, the maintenance estimate and published guidance (deficit 250/500/750 kcal/day with floors: never below their resting energy, 1,200 kcal women / 1,500 men; at most 1%/week). propose:true also creates a calorie-goal proposal the owner confirms with /healthgoal. It is an ESTIMATE pending physician review, not a prescription. '+STYLE,'health_coach_plan',
  schema({pace:{type:'string',enum:['gentle','standard','faster']},propose:{type:'boolean'}})],
 ['health_coach_balance','Energy balance: calories eaten (food log) vs calories burned (Apple Health resting + active energy when synced, otherwise the maintenance estimate), the daily deficit, totals over complete days, a rough weight-change guide and, after a few weeks, their maintenance observed from their own intake and weight trend. Never add Apple active calories to the activity-level estimate.','health_coach_balance',
  schema({days:{type:'integer',minimum:1,maximum:60},to:str('Last day, YYYY-MM-DD (default today).',10)})],
 ['health_coach_metric','Log a wellness metric the owner tells you: weight (value + unit), blood_pressure ("124/78"), resting_hr, waist (cm or in), sleep_hours, stress 1-5, mood 1-5, water (ml, cups or oz), exercise_minutes (+ intensity light/moderate/vigorous, activity), meditation_minutes, steps. If the result has "urgent", give that text word for word first.','health_coach_metric',
  schema({kind:{type:'string',enum:['weight','blood_pressure','resting_hr','waist','sleep_hours','stress','mood','water','exercise_minutes','meditation_minutes','steps']},value:str('The number (e.g. "72"), or "SYS/DIA" for blood pressure (e.g. "124/78").',12),
   unit:str('kg/lb, cm/in, ml/cups/oz when it matters.',8),intensity:{type:'string',enum:['light','moderate','vigorous']},activity:str('e.g. "brisk walk".',60),date:str('YYYY-MM-DD if not today.',10)},['kind','value'])],
 ['health_coach_metrics','Their wellness metrics over recent days: blood pressure average and its category, resting heart rate, sleep, stress, mood, water, waist, and this week\'s activity minutes against 150 (vigorous counts double).','health_coach_metrics',schema({days:{type:'integer',minimum:1,maximum:180}})],
];

export function parseCoach(args){
 const p=String(args||'').trim().split(/\s+/).filter(Boolean);const v=(p[0]||'status').toLowerCase();
 if(['on','off','resume','status','forget'].includes(v)&&p.length<=1)return {set:v};
 if(v==='pause'&&p.length<=2){const days=p[1]?Number(p[1]):1;return Number.isInteger(days)&&days>=1&&days<=60?{set:'pause',days}:null;}
 if(REMINDERS.includes(v)&&p.length===2&&(/^\d{1,2}:\d{2}$/.test(p[1])||p[1].toLowerCase()==='off'))return {set:'reminder',kind:v,time:p[1]};
 return null;
}

const HELP='Wellness coach (optional): /coach on · /coach off · /coach pause DAYS · /coach resume · /coach meals 19:00 (or exercise, weigh_in, stress, breathing, sleep, blood_pressure; "off" to stop one) · /coach forget (deletes coach data) · /coach status';

export function registerCoach(api,call,ownerContext,{gateway,wake,owner='100000001',every=300000,setTimer=(f,ms)=>setInterval(f,ms)}={}){
 for(const [name,description,action,parameters] of COACH_TOOLS)api.registerTool({name,label:name,description,parameters,async execute(_id,args){try{return out(await call(action,args??{}));}catch(e){return err(e);}}});
 api.registerCommand({name:'coach',description:HELP,channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);const a=parseCoach(ctx.args);if(!a)return {text:HELP};
  if(a.set==='status'){const s=await call('health_coach_status',{},true);
   return {text:s.enabled?`Coach is on. Goals: ${(s.goals||[]).join(', ')||'not set yet'}. Reminders: ${Object.entries(s.reminders||{}).map(([k,t])=>k+' '+t).join(', ')||'none'}.${s.limits?.length?' Limits: '+s.limits.join('; ')+'.':''}${s.paused_until?' Paused.':''}`:'Coach is off. /coach on to start (it asks a short questionnaire first).'};}
  const r=await call('health_coach_settings',a,true);
  if(a.set==='on'){
   if(!r.questionnaire_done)try{await wake?.('WELLNESS COACH TURNED ON by the owner. Run the short questionnaire now: call health_coach_questionnaire, ask one or two questions at a time in plain words, accept "skip", then save with health_coach_answers. Keep it friendly and quick.','procurator-coach-on:'+Date.now(),'coach on');}catch{}
   return {text:r.questionnaire_done?'Coach is on again with your earlier answers. /coach status shows the reminders.':'Coach is on. I\'ll ask a few quick questions now (goals, a couple of safety questions, reminder times).'};}
  if(a.set==='off')return {text:'Coach is off: no reminders. Your logs stay; /coach forget deletes the coach data.'};
  if(a.set==='pause')return {text:`Coach reminders paused for ${r.paused_days} day(s).`};
  if(a.set==='resume')return {text:'Coach reminders resumed.'};
  if(a.set==='forget')return {text:`Coach data deleted (${r.deleted_metrics} metric entries) and the coach is off. Food log and weights stay in the nutrition log.`};
  return {text:'Reminders: '+(Object.entries(r.reminders||{}).map(([k,t])=>k+' '+t).join(', ')||'none')};
 }catch(e){return {text:String(e?.message||e)};}}});
 // Reminder timer: one per process (the registry may reload), fixed texts only, idempotent per kind and day.
 const KEY=Symbol.for('owner.coach.timer');try{globalThis[KEY]?.unref?.();clearInterval(globalThis[KEY]);}catch{}
 const tick=async()=>{try{const r=await call('health_coach_due',{},true);
  for(const m of r?.send||[]){const day=new Date().toISOString().slice(0,10);
   try{await gateway?.('send',{channel:'telegram',to:owner,message:m.text,idempotencyKey:`procurator-coach:${m.kind}:${day}`});}catch{}}}catch{}};
 const t=setTimer(()=>{void tick();},every);t?.unref?.();globalThis[KEY]=t;
 return {tick};
}
