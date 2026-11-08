const ZONE='America/New_York';
const date=new Intl.DateTimeFormat('en-US',{timeZone:ZONE,month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'});
function time(value){return Number.isFinite(value)&&value>0?date.format(new Date(value)):undefined;}
function phone(value){const m=/^\+1(\d{3})(\d{3})(\d{4})$/.exec(value??'');return m?`(${m[1]}) ${m[2]}-${m[3]}`:undefined;}
export function formatCallStatus(result){
 const call=result.call?.found===true?result.call.call:undefined;
 const ended=Number.isFinite(call?.endedAt);
 const terminal=ended||result.status==='call_ended';
 const state=call?.state;
 const labels={pending:'Awaiting your approval',cancelled:'Call cancelled',expired:'Approval expired',attempting:'Placing your call',uncertain:'Call placement is unconfirmed',hangup_requested:'Hangup requested',initiated:'Call placed',bridge_waiting:'Listen-in call approved: waiting for you to call the assistant',bridge_expired:'Listen-in call expired (you did not call within 15 minutes)'};
 const terminalLabels={completed:'Call finished','hangup-user':'Call finished','hangup-bot':'Call finished',timeout:'Call ended at its time limit',failed:'Call failed','no-answer':'No answer',busy:'Line was busy',error:'Call ended with an error',voicemail:'Voicemail reached'};
 let title=terminal?(terminalLabels[state]??'Call ended'):(labels[result.status]??'No call prepared');
 if(!terminal&&['ringing','answered','active','speaking','listening'].includes(state))title=state==='ringing'?'Phone is ringing':'Call in progress';
 const lines=[title];
 const to=phone(result.to);if(to)lines.push('To: '+to);
 if(call?.answeredAt){const when=time(call.answeredAt);if(when)lines.push('Answered: '+when);}
 if(ended&&time(call.endedAt))lines.push('Ended: '+time(call.endedAt));
 if(ended&&Number.isFinite(call.answeredAt)&&call.endedAt>=call.answeredAt)lines.push('Connected: '+Math.round((call.endedAt-call.answeredAt)/1000)+' seconds');
 else if(ended&&!call.answeredAt)lines.push('No answered conversation was confirmed.');
 if(result.status==='uncertain')lines.push('Please wait for the call record to be checked before trying again.');
 if(result.status==='pending')lines.push('Use the complete approval command from the call review when ready.');
 if(terminal){
  const report=result.report?.status;
  if(report==='sent'||report==='delivered')lines.push('Summary: sent to your Telegram chat.');
  else if(['generating','pending','sending'].includes(report))lines.push('Summary: still being prepared or delivered.');
  else if(report==='uncertain')lines.push('Summary delivery is unconfirmed. It will not be resent automatically.');
  else lines.push(`Summary: ${report==='failed'?'could not be delivered':'not yet confirmed'}.${result.id?' Send /callreport '+result.id+' to retrieve it.':''}`);
  if(result.reached?.summary){
   lines.push('Reached: '+result.reached.summary.replace(/\.$/,'')+(result.reached.path?.length?' ('+result.reached.path.join(' → ')+')':'')+' — estimated from the transcript');
   if(['menu_only','hold_only','voicemail','closed','no_answer','busy','silence'].includes(result.reached.reached))lines.push('Nothing is retried automatically. Ask me to call again (a longer call if it was a hold, or at a better time).');
  }
  if(result.id)lines.push('Transcript: /calltranscript '+result.id);
  lines.push('The call record confirms the connection, not completion of an administrative task.');
 }
 if(result.id)lines.push('Call reference: '+result.id);
 return lines.join('\n');
}
