// Run inside the pinned gateway. Credentials arrive only over private stdin.
import fs from 'node:fs';
import WebSocket from '/app/node_modules/ws/wrapper.mjs';

const credentials = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = {model:'gpt-realtime-2.1',voice:'marin',phoneCallsPlaced:0,sessionUpdated:false,audioBytes:0};
if (credentials.model !== result.model || !/^sk-[A-Za-z0-9_-]{20,500}$/.test(credentials.api_key ?? '')) {
  console.log(JSON.stringify({...result,passed:false,error:'invalid_credentials'}));
  process.exit(1);
}
const errors = new Set(['credit_balance_exhausted','insufficient_quota','organization_spend_limit_exceeded','project_spend_limit_exceeded','organization_usage_limit_exceeded','invalid_api_key','model_not_found','rate_limit_exceeded']);
const safeError = code => errors.has(code) ? code : 'provider_error';
const ws = new WebSocket('wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1', {
  headers:{Authorization:'Bearer '+credentials.api_key}, followRedirects:false,
});
let finished = false;
function finish(ok, error) {
  if (finished) return;
  finished = true;
  clearTimeout(timer);
  console.log(JSON.stringify({...result,passed:ok,...error ? {error} : {}}));
  ws.terminate();
  process.exitCode = ok ? 0 : 1;
}
const timer = setTimeout(() => finish(false,'timeout'),35000);
ws.on('open', () => ws.send(JSON.stringify({type:'session.update',session:{
  type:'realtime',model:result.model,output_modalities:['audio'],
  instructions:'This is a synthetic voice connection test. Say only: Voice connection test successful.',
  reasoning:{effort:'low'},tools:[],
  audio:{
    input:{format:{type:'audio/pcmu'},noise_reduction:null,
      transcription:{model:'gpt-4o-mini-transcribe'},
      turn_detection:{type:'server_vad',silence_duration_ms:450,prefix_padding_ms:300,create_response:true,interrupt_response:true}},
    output:{format:{type:'audio/pcmu'},voice:'marin'},
  },
}})));
ws.on('message', data => {
  let event;
  try { event = JSON.parse(data); } catch { return finish(false,'invalid_json'); }
  if (event.type === 'error') return finish(false,safeError(event.error?.code));
  if (event.type === 'session.updated') {
    if (result.sessionUpdated) return;
    result.sessionUpdated = true;
    ws.send(JSON.stringify({type:'response.create',response:{input:[{
      type:'message',role:'user',content:[{type:'input_text',text:'Please perform the voice connection test.'}],
    }]}}));
  }
  if (event.type === 'response.output_audio.delta' && event.delta) {
    result.audioBytes += Buffer.from(event.delta,'base64').length;
  }
  if (event.type === 'response.done') {
    const complete = event.response?.status === 'completed';
    finish(result.sessionUpdated && result.audioBytes > 0 && complete,
      complete ? undefined : safeError(event.response?.status_details?.error?.code));
  }
});
ws.on('unexpected-response', (_req,response) => finish(false,'http_'+response.statusCode));
ws.on('error', () => finish(false,'websocket_error'));
ws.on('close', () => { if (!finished) finish(false,'closed_before_audio'); });
