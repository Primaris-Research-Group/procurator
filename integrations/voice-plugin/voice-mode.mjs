export function voiceSnapshot(settings){
 const voiceMode=settings.voiceMode??'classic';
 if(!['classic','openai-realtime'].includes(voiceMode))throw new Error('Phone test voice mode is not recognized. No call placed.');
 return {voiceMode,processors:voiceMode==='openai-realtime'?['Twilio','OpenAI realtime voice','Anthropic private call summaries']:['Twilio speech','Anthropic conversation and private summaries']};
}
export function voiceProcessing(plan){
 return plan.voiceMode==='openai-realtime'?'Speech is processed by Twilio and OpenAI realtime voice; private text summaries use the existing Anthropic API.':'Speech is processed by Twilio and the existing Anthropic API.';
}
export function assertVoiceCurrent(plan,settings){
 if((plan.voiceMode??'classic')!==voiceSnapshot(settings).voiceMode)throw new Error('Phone test voice processing changed. Request a fresh call review before approving.');
}
