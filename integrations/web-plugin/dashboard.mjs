// Dashboard sign-in from Telegram (Nov 6 2026). The page itself is served by the browser broker (web/dashboard.py);
// this only asks the broker to send the owner a single-use sign-in link, or to sign every device out.
// 0.19.1 (Nov 6 QA): OpenClaw has its own built-in /dashboard that takes precedence; /panel is the working name.
// 0.24.0: /compose [description] opens the Packet Composer (guided chat that builds a draft task packet) on the dashboard;
// the assistant can offer the same with mission_compose. The Composer never runs anything: saving makes a DRAFT packet.
export function registerDashboard(api,call,ownerContext){
 for(const name of ['panel','dashboard'])api.registerCommand({name,description:'Your private dashboard: /panel sends a sign-in link · /panel logout signs out every device',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);
  const verb=String(ctx.args||'').trim().toLowerCase();
  if(verb==='logout'){const r=await call('dashboard_logout',{},true);return {text:`Signed out of the dashboard on ${r.revoked} device${r.revoked===1?'':'s'}. Send /panel for a new link.`};}
  await call('dashboard_link',{},true);
  return {text:'Your sign-in link is above (private, works once, 45 minutes). The dashboard shows packets, builds, usage, machines and activity; approvals stay here in Telegram.'};
 }catch(e){return {text:String(e?.message||e)};}}});
 api.registerCommand({name:'compose',description:'Packet Composer: /compose [what you need done] — a guided chat on the dashboard that builds a draft task packet',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);
  const seed=String(ctx.args||'').trim().slice(0,4000);
  await call('composer_start',seed?{seed}:{},true);
  return {text:'The Packet Composer link is above. Describe the task there; saving creates a draft packet that only runs after you activate it.'};
 }catch(e){return {text:String(e?.message||e)};}}});
 api.registerCommand({name:'composerbench',description:'Test the Packet Composer: /composerbench (8 scripted owners, about $1–4) · /composerbench quick (3)',channels:['telegram'],requireAuth:true,acceptsArgs:true,async handler(ctx){try{
  ownerContext(ctx);
  const quick=/^quick$/i.test(String(ctx.args||'').trim());
  const r=await call('composer_bench',{quick},true);
  return {text:`Composer bench started: ${r.personas} scripted owners talk to the Packet Composer (nothing is saved). The report arrives here in a few minutes.`};
 }catch(e){return {text:String(e?.message||e)};}}});
 api.registerTool({name:'mission_compose_attach',label:'mission_compose_attach',description:'After mission_compose: add a document you read for this task (drive_read_document, gmail_attachment_read, or text the owner pasted) to that Composer session as a packet file the work sessions will read. Only for a session you opened in the last hour; .md or .txt name; at most 100,000 characters per file; never documents carrying passwords, codes, card or ID numbers (they are refused).',
  parameters:{type:'object',properties:{session:{type:'string',description:'The session id mission_compose returned.',maxLength:16},name:{type:'string',description:'File name, e.g. 01-employer-policy.md',maxLength:80},text:{type:'string',maxLength:100000}},required:['session','name','text'],additionalProperties:false},
  async execute(_id,args){try{const r=await call('composer_offer_attach',{session:args?.session,name:args?.name,text:args?.text});return {content:[{type:'text',text:JSON.stringify({attached:r.attached,chars:r.chars,attachments:r.attachments})}]};}
  catch(e){return {content:[{type:'text',text:JSON.stringify({error:String(e?.message||e)})}],isError:true};}}});
 api.registerTool({name:'mission_compose',label:'mission_compose',description:'When the owner wants a multi-step task set up as a PACKET (a mission PROCURATOR works through over days), open the Packet Composer for them: a guided chat on their dashboard, seeded with your short summary of what the owner said (no passwords, codes, card or ID numbers; for health tasks only what the task needs). The owner gets a private link; the Composer asks what is missing and saves a DRAFT the owner activates themselves. Returns only a session id; to include a document you read (Drive, an email attachment), call mission_compose_attach with that id.',
  parameters:{type:'object',properties:{summary:{type:'string',description:'2-6 sentences: the task, dates, people/sites involved, what done looks like.',maxLength:4000}},required:['summary'],additionalProperties:false},
  async execute(_id,args){try{const r=await call('composer_start',{seed:String(args?.summary||'').slice(0,4000)});return {content:[{type:'text',text:JSON.stringify({session:r.session,sent_to_owner:true,next:'Tell them in one line the Composer link is in their messages.'})}]};}
  catch(e){return {content:[{type:'text',text:JSON.stringify({error:String(e?.message||e)})}],isError:true};}}});
}
