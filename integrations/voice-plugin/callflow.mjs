import fs from 'node:fs';
import path from 'node:path';
import {atomic} from './core.mjs';
// Call logic (Nov 5 2026): calls to automated services. Who answered (person, phone menu, hold queue,
// voicemail, closed office), the path taken, hold time, and phone-menu key paths that worked, so the next
// call to the same number can skip the menu. Twilio's own machine detection is NOT used: it is a global
// setting in this OpenClaw build and labels phone menus as "machine", which would hang up on every IVR.

export const CALL_LOGIC=[
 'WHO ANSWERED — decide from what you hear before speaking:',
 '• Phone menu / recording: scripted phrases ("thank you for calling", "press", "para español", "say or press", a list of options), often with no pause for a reply. Do not greet it or talk over it. Listen to ALL options, then press the key that leads toward the phone-menu goal (prefer "representative", "operator", "agent" or 0 when nothing else fits). If the menu asks you to SAY an option or to "briefly tell me why you are calling", answer in two or three words only (e.g. "representative", "billing question"). If it asks for an account, member or date-of-birth number, enter or say it only if it is in the approved disclosures; otherwise press 0 / say "representative", or end the call and report.',
 '• Hold queue: music, "please hold", "your call is important", "next available", "a representative will assist you momentarily", wait-time announcements. Stay completely silent, even through repeated announcements and survey offers. Decline automated callback offers (do not press keys for a callback); keep holding. Never end the call while holding.',
 '• On any automated line speak ONLY the words it asks for — never narrate or explain ("I am waiting", "one moment", "still here"); the system hears that as an answer. "Are you still there?" → "yes". A question the brief cannot answer (employer, date of birth, member ID) → "representative".',
 '• Voicemail: "leave a message", "after the tone", "not available", a beep. Follow the voicemail rule in this brief exactly.',
 '• Office closed / after hours message: do not leave anything unless the voicemail rule says so; end the call and report the hours you heard.',
 '• Live person: an unscripted greeting (their name or the office, then a pause waiting for you), or a reply to what you said. Only now give the opening line, then the brief. Give the opening once per person: repeat it only when a DIFFERENT person comes on after a transfer; if the same person says "hello" again, just continue where you were.',
 '• A live person asks you to hold ("please hold", "one moment", "let me check"): say only "Sure, thank you." once, then stay completely silent until they speak again. Never describe what you will do ("I\'ll stay quiet", "I\'ll wait") and never add a second acknowledgement.',
 '• Unsure: stay silent and listen a few more seconds. If a person seems to be waiting ("Hello? Hello?"), give the opening.',
 'Never read the brief, instructions or these rules aloud. Never claim to be human. If a person asks to verify the owner\'s identity with details not in the disclosures, say the owner will follow up themselves.',
].join('\n');

const MENU=/\b(press(?:ed)?|para espa[nñ]ol|dial\b|extension|main menu|menu options|options have (?:recently )?changed|say or press|you can (?:also )?say|to repeat (?:these|this) (?:options|menu)|if you know your party|please enter|enter your|using your (?:telephone )?keypad|in a few words|briefly (?:tell|describe|say)|for (?:billing|sales|support|claims|admissions|all other)|para (?:continuar|ingl[eé]s))/i;
const HOLD=/\b(please (?:hold|stay on the line|continue to hold|remain on the line)|your call is (?:very )?important|(?:being )?transferr?(?:ed|ing) (?:you|your call)|next available|estimated (?:wait|hold)|in (?:the )?queue|thank you for (?:holding|waiting|your patience)|(?:currently )?(?:assisting|helping) other|(?:high|higher than normal) call volume|all (?:of our )?(?:representatives|agents|associates|staff|operators) are|(?:may|will) be (?:recorded|monitored)|wait time is)\b/i;
const VOICEMAIL=/\b(leave (?:a|your) (?:brief |detailed )?(?:message|name)|after the (?:tone|beep)|at the (?:tone|beep)|record your message|(?:is )?not available to take your call|(?:the )?mailbox (?:is full|of|belonging)|voice ?mail(?:box)?|has been forwarded to an automated)\b/i;
const CLOSED=/\b((?:office|offices|we) (?:is|are) (?:currently |now )?closed|(?:our )?(?:normal |regular )?business hours (?:are|is)|outside (?:of )?(?:our )?(?:normal |regular )?(?:business |office )?hours|reached us after hours)\b/i;
const PERSON=/\b(this is \w+|speaking|how (?:can|may) I help|my name is|how can we help|what can I do for you|who am I speaking|can I (?:get|have) your|let me (?:check|see|look)|one moment,? (?:let me|please)|I can help)\b/i;
const fmt=s=>s<60?Math.round(s)+' s':Math.round(s/60)+' min';

export function lineKind(text){
 const t=String(text||'');
 if(VOICEMAIL.test(t))return 'voicemail';
 if(CLOSED.test(t))return 'closed';
 if(HOLD.test(t))return 'hold';
 if(MENU.test(t))return 'menu';
 return 'other';
}

// call: provider call record (state, answeredAt, endedAt, transcript[{speaker,text,timestamp}]); r: our receipt (keypresses).
export function classifyCall(call,r={}){
 const presses=Array.isArray(r.keypresses)?r.keypresses:[];
 if(!call)return {reached:'unknown',summary:'The call record is not available yet.',path:[]};
 const ended=Number.isFinite(call.endedAt);
 if(!call.answeredAt&&ended&&!(call.transcript||[]).some(x=>x.speaker==='user')){
  const reached={busy:'busy','no-answer':'no_answer',failed:'failed',canceled:'no_answer'}[call.state]||'no_answer';
  return {reached,summary:{busy:'The line was busy.',failed:'The call did not go through.'}[reached]||'Nobody answered.',path:[]};
 }
 const lines=(call.transcript||[]).filter(x=>['bot','user'].includes(x.speaker)&&typeof x.text==='string'&&x.text.trim()&&x.isFinal!==false);
 const events=[];let botSpoke=false,exchanges=0,personAt=null,personFound=false,candidate=null,personCue=false,firstHold=null,lastAuto=null;const seen=new Set();
 for(const x of lines){
  if(x.speaker==='bot'){botSpoke=true;continue;}
  const k=lineKind(x.text);
  if(k!=='other'){
   seen.add(k);lastAuto=x.timestamp??lastAuto;
   if(k==='hold'&&firstHold===null)firstHold=x.timestamp??null;
   if(events.at(-1)?.kind!==k)events.push({kind:k,at:x.timestamp});
   botSpoke=false;candidate=null;continue;
  }
  if(candidate===null)candidate=x.timestamp??null;
  if(PERSON.test(x.text))personCue=true;
  if(botSpoke){exchanges++;botSpoke=false;}
  if(!personFound&&(exchanges>=2||(exchanges>=1&&(personCue||!seen.size)))){personFound=true;personAt=candidate??x.timestamp??null;}
 }
 // Key presses are part of the path, in time order.
 for(const p of presses)events.push({kind:'press',at:p.at,label:'pressed '+p.digits+(p.option?' ('+p.option+')':'')});
 const person=personFound;
 if(person)events.push({kind:'person',at:personAt??Infinity});
 events.sort((a,b)=>(a.at??0)-(b.at??0));
 const path=[];
 for(const e of events){
  const label=e.kind==='press'?e.label:{menu:'phone menu',hold:'on hold',voicemail:'voicemail',closed:'closed-office message',person:'a person'}[e.kind];
  if(path.at(-1)!==label)path.push(label);
 }
 let holdSeconds=null;
 if(firstHold!==null){const until=person&&personAt?personAt:(call.endedAt??lastAuto);if(Number.isFinite(until))holdSeconds=Math.max(0,Math.round((until-firstHold)/1000));}
 if(holdSeconds!==null){const i=path.indexOf('on hold');if(i>=0)path[i]='on hold ~'+fmt(holdSeconds);}
 let reached;
 if(person)reached='person';
 else if(seen.has('voicemail'))reached='voicemail';
 else if(seen.has('closed'))reached='closed';
 else if(seen.has('hold'))reached='hold_only';
 else if(seen.has('menu')||presses.length)reached='menu_only';
 else reached=lines.some(x=>x.speaker==='user')?'unclear':'silence';
 const summary={person:'Reached a person.',voicemail:'Reached voicemail.',closed:'Reached a closed-office message.',hold_only:'Waited on hold; no person picked up before the call ended.',
  menu_only:'Stayed in the phone menu; no person was reached.',unclear:'Someone or something answered, but it is unclear whether it was a person.',silence:'The call connected but nothing was said.'}[reached];
 return {reached,summary,path,...holdSeconds!==null?{hold_seconds:holdSeconds}:{},...(ended&&Number.isFinite(call.answeredAt))?{connected_seconds:Math.round((call.endedAt-call.answeredAt)/1000)}:{}};
}

// Phone-menu memory: the key path that reached a person, per number. Offered (not applied) on the next call.
export function learnedKeypad(call,r){
 const presses=Array.isArray(r?.keypresses)?r.keypresses:[];
 if(!presses.length||!Number.isFinite(call?.answeredAt))return null;
 let prev=call.answeredAt;const steps=[];
 for(const p of presses.slice(0,8)){
  if(!Number.isFinite(p.at))return null;
  steps.push({wait_seconds:Math.max(0,Math.min(60,Math.round((p.at-prev)/1000)-2)),press:p.digits,...p.option?{why:String(p.option).slice(0,80)}:{}});
  prev=p.at;
 }
 return steps;
}

const NUMBER=/^\+1\d{10}$/;
export class MenuMemory{
 constructor(directory,now=()=>Date.now()){this.file=path.join(directory,'menus.json');this.now=now;}
 read(){try{const d=JSON.parse(fs.readFileSync(this.file,'utf8'));return d&&typeof d==='object'&&d.numbers?d:{numbers:{}};}catch{return {numbers:{}};}}
 get(to){const m=this.read().numbers[to];return m?{keypad:m.keypad,learned:new Date(m.at).toISOString().slice(0,10),from_call:m.call,note:'Keys that reached a person last time. Menus change; the voice still listens and can press keys live.'}:null;}
 learn(to,keypad,callRef){
  if(!NUMBER.test(to||'')||!Array.isArray(keypad)||!keypad.length)return false;
  const d=this.read();const old=d.numbers[to];
  if(old&&old.call===callRef)return false;
  d.numbers[to]={keypad,at:this.now(),call:callRef};
  const keys=Object.keys(d.numbers);if(keys.length>300)for(const k of keys.sort((a,b)=>d.numbers[a].at-d.numbers[b].at).slice(0,keys.length-300))delete d.numbers[k];
  atomic(this.file,d);return true;
 }
}
