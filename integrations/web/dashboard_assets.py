"""Inline CSS, JavaScript and SVG chart helpers for the PROCURATOR dashboard (Nov 6 2026).

Everything is served from the page itself: no CDN, no fonts, no third-party script. Charts are plain
SVG strings built server-side so the page works with scripts disabled (the script only refreshes
numbers and drives the packet upload and action buttons).
"""
import html
import math

CSS = '''img.live{width:100%;border-radius:8px;border:1px solid #ddd;min-height:60px;background:#f4f4f4}

:root{color-scheme:light dark;--bg:#f4f5f7;--fg:#15171a;--muted:#5d6370;--card:#fff;--line:#d9dce2;--accent:#0b63ce;--accent2:#1d9e75;--warn:#b26a00;--bad:#b3261e;--chip:#eef1f6;--mono:ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#101215;--fg:#ebedf0;--muted:#a3a9b5;--card:#1a1d22;--line:#2d323b;--accent:#5aa2ff;--accent2:#3ec28f;--warn:#f0b35a;--bad:#ff8a80;--chip:#242932}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;font:15px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line)}
header .bar{display:flex;align-items:center;gap:10px;padding:10px 14px;max-width:1100px;margin:0 auto}
header h1{font-size:17px;margin:0;letter-spacing:.02em}header h1 span{color:var(--muted);font-weight:400;font-size:13px;margin-left:8px}
nav{display:flex;gap:2px;overflow-x:auto;max-width:1100px;margin:0 auto;padding:0 8px 0}nav a{padding:8px 11px;border-radius:9px 9px 0 0;color:var(--muted);white-space:nowrap;font-size:14px}
nav a.on{color:var(--fg);background:var(--bg);font-weight:600}nav a:hover{text-decoration:none;color:var(--fg)}
main{max-width:1100px;margin:0 auto;padding:12px 12px 60px}
.grid{display:grid;gap:12px;grid-template-columns:1fr}@media(min-width:720px){.grid{grid-template-columns:1fr 1fr}.grid .wide{grid-column:1/-1}}
section.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;min-width:0}
section.card h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 8px;font-weight:600}
.big{font-size:26px;font-weight:700;line-height:1.1}.sub{color:var(--muted);font-size:13px}.row{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:14px}.kv b{font-weight:600;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:6px 6px 4px;border-bottom:1px solid var(--line)}
td{padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-word}tr:last-child td{border-bottom:0}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap}
.chip.active,.chip.done,.chip.online,.chip.completed,.chip.ready{background:#dff5ea;color:#0f6b46}.chip.paused,.chip.blocked,.chip.over_budget,.chip.uncertain,.chip.waiting_for_owner,.chip.waiting_for_approval,.chip.awaiting_owner{background:#fff1d6;color:#7a4a00}
ol.steps{margin:6px 0 10px;padding-left:22px}ol.steps li{margin:4px 0;color:var(--muted)}
.chip.needed{background:#fff1d1;color:#7a4b00}
.chip.failed,.chip.offline,.chip.cancelled,.chip.rejected,.chip.expired,.chip.bad{background:#fde3e1;color:#8c1d16}.chip.ok,.chip.passed,.chip.all_gates_passed,.chip.approve{background:#dff5ea;color:#0f6b46}.chip.warn,.chip.open_items,.chip.missing,.chip.timed_out,.chip.request_changes,.chip.waiting_owner{background:#fff1d6;color:#7a4a00}.chip.running,.chip.in_progress,.chip.claimed,.chip.preparing,.chip.testing,.chip.pushing,.chip.queued{background:#dde9fb;color:#114a8f}
@media (prefers-color-scheme:dark){.chip.bad{background:#4a1612;color:#ffb3ab}.chip.ok,.chip.passed,.chip.all_gates_passed,.chip.approve{background:#143b2b;color:#7fe0b3}.chip.warn,.chip.open_items,.chip.missing,.chip.timed_out,.chip.request_changes,.chip.waiting_owner{background:#3f2d0c;color:#f3c777}.chip.active,.chip.done,.chip.online,.chip.completed,.chip.ready{background:#143b2b;color:#7fe0b3}.chip.paused,.chip.blocked,.chip.over_budget,.chip.uncertain,.chip.waiting_for_owner,.chip.waiting_for_approval,.chip.awaiting_owner{background:#3f2d0c;color:#f3c777}.chip.failed,.chip.offline,.chip.cancelled,.chip.rejected,.chip.expired{background:#4a1612;color:#ffb3ab}.chip.running,.chip.in_progress,.chip.claimed,.chip.preparing,.chip.testing,.chip.pushing,.chip.queued{background:#13304f;color:#9cc4ff}}
button,.btn{font:inherit;font-size:14px;padding:8px 13px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{border-color:var(--bad);color:var(--bad)}button:disabled{opacity:.5;cursor:default}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
input[type=text],input[type=number],select,textarea{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--fg);max-width:100%}
label{display:block;font-size:13px;color:var(--muted);margin:8px 0 3px}form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:end}form.inline label{margin:0}
.bar-h{height:8px;border-radius:999px;background:var(--chip);overflow:hidden}.bar-h i{display:block;height:100%;background:var(--accent)}.bar-h i.warn{background:var(--warn)}.bar-h i.bad{background:var(--bad)}
.tg{font-family:var(--mono);font-size:13px;background:var(--chip);padding:3px 7px;border-radius:7px;user-select:all;white-space:nowrap}
.plog{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:6px 10px;font-size:13.5px}.pl{padding:4px 0;border-bottom:1px solid var(--line)}.pl:last-child{border-bottom:0}.pl .sub{display:inline-block;min-width:92px}
pre{font-family:var(--mono);font-size:12px;background:var(--chip);padding:10px;border-radius:10px;overflow:auto;max-height:360px;white-space:pre-wrap;word-break:break-word}
ul.plain{list-style:none;margin:0;padding:0}ul.plain li{padding:6px 0;border-bottom:1px solid var(--line)}ul.plain li:last-child{border-bottom:0}
.steps li{display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:start}.steps .evid{font-size:12px;color:var(--muted)}
.msg{margin:8px 0;padding:9px 12px;border-radius:10px;font-size:14px}.msg.bad{background:#fde3e1;color:#8c1d16}.msg.warn{background:#fff1d6;color:#7a4a00}.msg.ok{background:#dff5ea;color:#0f6b46}
@media (prefers-color-scheme:dark){.msg.bad{background:#4a1612;color:#ffb3ab}.msg.warn{background:#3f2d0c;color:#f3c777}.msg.ok{background:#143b2b;color:#7fe0b3}}
svg.chart{width:100%;height:auto;display:block}.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:4px}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.empty{color:var(--muted);font-style:italic}.right{text-align:right}.small{font-size:13px}details summary{cursor:pointer;color:var(--accent)}
footer{color:var(--muted);font-size:12px;text-align:center;padding:16px}
.compose{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}@media (max-width:900px){.compose{grid-template-columns:1fr}}
.chat{display:flex;flex-direction:column;gap:8px;max-height:60vh;overflow:auto;margin-bottom:8px}
.chat .m{padding:8px 11px;border-radius:12px;max-width:92%;white-space:pre-wrap;font-size:14px}.chat .owner{align-self:flex-end;background:var(--accent);color:#fff}
.chat .assistant{align-self:flex-start;background:var(--chip)}.chat .m ul{margin:6px 0 0 18px;padding:0}
.gaps li.missing{color:#8c1d16}@media (prefers-color-scheme:dark){.gaps li.missing{color:#ffb3ab}}
/* 0.24.3 (B16): a wide table scrolls inside its card instead of widening the page (Usage was 826 px wide at 390). */
.grid{grid-template-columns:minmax(0,1fr)}@media(min-width:720px){.grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
.grid>*,.card{min-width:0}.card{overflow-x:auto;-webkit-overflow-scrolling:touch}pre{max-width:100%;overflow-x:auto}
@media(max-width:600px){th,td{white-space:nowrap}}
'''

JS = r'''
(function(){
const csrf=document.querySelector('meta[name=csrf]')?.content||'';
const base='/assistant/dashboard';
const $=(s,r)=>(r||document).querySelector(s),$$=(s,r)=>[...(r||document).querySelectorAll(s)];
// 0.24.3 (B05): a check that answers ok:false with its findings (packet validation) is a RESULT, not a failure: pass
// {result:true} to get it back. Only transport errors and server errors throw.
async function api(path,method,body,opt){
 const r=await fetch(base+'/api'+path,{method:method||'GET',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Procurator-CSRF':csrf},body:body==null?undefined:JSON.stringify(body)});
 let j={};try{j=await r.json();}catch{}
 if(opt&&opt.result&&r.ok&&!j.error)return j;
 if(!r.ok||j.ok===false)throw new Error(j.error||(Array.isArray(j.errors)&&j.errors.length?j.errors.join(' '):'')||('Request failed ('+r.status+')'));
 return j;
}
function say(el,text,kind){if(!el)return;el.textContent=text;el.className='msg '+(kind||'');el.hidden=!text;}
// Action buttons: data-post="/packets/bg/state" data-body='{"action":"pause"}' (confirm via data-confirm)
document.addEventListener('click',async e=>{
 const b=e.target.closest('button[data-post]');if(!b)return;
 if(b.dataset.confirm&&!confirm(b.dataset.confirm))return;
 const out=$(b.dataset.msg||'#msg');b.disabled=true;
 try{const j=await api(b.dataset.post,'POST',JSON.parse(b.dataset.body||'{}'));say(out,j.message||'Done.','ok');if(j.go&&/^https?:\/\//.test(j.go)){location.href=j.go;return;}if(b.dataset.reload!=='no')setTimeout(()=>location.reload(),600);}
 catch(err){say(out,err.message,'bad');b.disabled=false;}
});
// Settings forms: <form data-post="/packets/bg/settings"> with named inputs
document.addEventListener('submit',async e=>{
 const f=e.target.closest('form[data-post]');if(!f)return;e.preventDefault();
 const body={};for(const [k,v] of new FormData(f))body[k]=v;
 const out=$(f.dataset.msg||'#msg');
 try{const j=await api(f.dataset.post,'POST',body);say(out,j.message||'Saved.','ok');if(f.dataset.reload!=='no')setTimeout(()=>location.reload(),600);}
 catch(err){say(out,err.message,'bad');}
});
// 0.28.0 Library upload: the file's bytes go to /api/v1/library/upload with the choices in the query string.
const libSend=$('#lib-send');
if(libSend){libSend.addEventListener('click',async()=>{
 const f=$('#lib-file').files[0],out=$('#msg');if(!f){say(out,'Choose a file first.','bad');return;}
 if(f.size>25*1024*1024){say(out,'Uploads are limited to 25 MB.','bad');return;}
 const q=new URLSearchParams({name:f.name,collection:$('#lib-collection').value||'general',use_chat:$('#lib-chat').checked?'1':'0',use_packets:$('#lib-packets').checked?'1':'0',pinned:$('#lib-pin').checked?'1':'0'});
 libSend.disabled=true;say(out,'Uploading '+f.name+'…','');
 try{const r=await fetch(base+'/api/v1/library/upload?'+q,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/octet-stream','X-Procurator-CSRF':csrf},body:await f.arrayBuffer()});
  const j=await r.json();if(!j.ok)throw new Error(j.error||'Upload failed');say(out,j.message,'ok');setTimeout(()=>location.reload(),800);}
 catch(err){say(out,err.message,'bad');libSend.disabled=false;}
});}
// 0.28.0 Live: poll the latest frame of each browser card about once a second while the page is open (no page open = no frames).
const liveImgs=$$('img[data-live]');
if(liveImgs.length){const tags={};setInterval(async()=>{for(const img of liveImgs){const t=img.dataset.live,note=$('[data-live-note="'+t+'"]');
 try{const r=await fetch(base+'/api/v1/live/'+encodeURIComponent(t)+'/frame',{credentials:'same-origin',headers:tags[t]?{'If-None-Match':tags[t]}:{}});
  if(r.status===204||r.status===304)continue;
  if((r.headers.get('Content-Type')||'').startsWith('application/json')){const j=await r.json();img.removeAttribute('src');tags[t]=j.etag;if(note)note.textContent='Hidden here: '+(j.placeholder.domain||'')+' — '+(j.placeholder.reason||'');continue;}
  tags[t]=r.headers.get('ETag');const b=await r.blob();const u=URL.createObjectURL(b);const old=img.src;img.src=u;if(old&&old.startsWith('blob:'))URL.revokeObjectURL(old);if(note)note.textContent='Live · view only';}
 catch{}}},1000);}
// Overview numbers refresh every 30 s (elements carry data-key="path.to.value")
function get(o,p){return p.split('.').reduce((a,k)=>a==null?a:a[k],o);}
async function refresh(){try{const j=await api('/overview');$$('[data-key]').forEach(el=>{const v=get(j,el.dataset.key);if(v!=null)el.textContent=el.dataset.fmt==='usd'?'$'+Number(v).toFixed(2):el.dataset.fmt==='k'?fmtK(v):String(v);});}catch{}}
function fmtK(n){n=Number(n)||0;return n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n);}
if($('[data-key]'))setInterval(refresh,30000);
// Packet upload: read files in the browser, validate, then save.
const up=$('#packet-files');
if(up){
 let packet={},token=null;const list=$('#packet-list'),report=$('#packet-report'),save=$('#packet-save'),out=$('#packet-msg');
 up.addEventListener('change',async()=>{
  packet={};token=null;list.textContent='';report.innerHTML='';save.disabled=true;$('#packet-activate-save').disabled=true;say(out,'');
  const picked=[...up.files].sort((a,b)=>a.name.localeCompare(b.name));
  if(!picked.length)return;if(picked.length>25){say(out,'Pick at most 25 files.','bad');return;}
  for(const f of picked){if(!/\.(md|txt)$/i.test(f.name)){say(out,f.name+' is not a .md or .txt file.','bad');return;}packet[f.name]=await f.text();
   const li=document.createElement('li');li.textContent=f.name+' · '+(Math.round(packet[f.name].length/100)/10)+'k characters';list.appendChild(li);}
  say(out,'Checking…','');
  try{
   const j=await api('/packets/validate','POST',{files:packet},{result:true});token=j.ok?(j.token||null):null;
   const p=j.packet||{};const rows=[];
   for(const e of j.errors||[])rows.push('<div class="msg bad">'+esc(e)+'</div>');
   for(const w of j.warnings||[])rows.push('<div class="msg warn">'+esc(w)+'</div>');
   if(p.kind){rows.push('<div class="kv"><b>Mission</b><span><input id="packet-mission" type="text" maxlength="24" value="'+esc(p.mission||'')+'"></span><b>Title</b><span>'+esc(p.title||'')+'</span><b>Revision</b><span>'+esc(p.revision||'(date)')+'</span><b>Kind</b><span>'+esc(p.kind)+(p.kind_source==='stated'?'':' <span class="sub">(inferred)</span>')+'</span><b>Goal</b><span>'+esc(p.goal||'')+'</span><b>Done when</b><span>'+(p.done_when||[]).map(esc).join('<br>')+'</span><b>Cadence</b><span>'+p.cadence+' min · hours '+p.hours[0]+':00–'+p.hours[1]+':00</span><b>Budget</b><span>'+(p.budget_usd!=null?'$'+p.budget_usd:'none')+'</span><b>Model</b><span>'+esc(p.model||'main model')+'</span>'+(p.sites?.length?'<b>Sites</b><span>'+esc(p.sites.join(', '))+'</span>':'')+(p.repo?'<b>Repo</b><span>'+esc(p.repo)+' · '+esc(p.builder)+' · '+esc(p.agent||'default agent')+'</span>':'')+'</div>');
    if(p.steps?.length)rows.push('<h3 class="small">Steps</h3><ol class="small">'+p.steps.map(s=>'<li><span class="chip">'+esc(s.type)+'</span> '+esc(s.title)+'</li>').join('')+'</ol>');
    if(p.threads?.length)rows.push('<h3 class="small">Journals</h3>'+p.threads.map(t=>'<p class="small"><b>'+esc(t.thread)+'</b>: '+t.steps.map(esc).join(' → ')+'</p>').join(''));}
   report.innerHTML=rows.join('');
   say(out,j.ok?'Packet accepted. Choose Save or Save and activate.':'Fix the problems above and pick the files again.',j.ok?'ok':'bad');
   save.disabled=!j.ok;$('#packet-activate-save').disabled=!j.ok;
  }catch(err){say(out,err.message,'bad');}
 });
 // Picking the same file again must check it again (the browser fires no change event for an identical choice).
 up.addEventListener('click',()=>{up.value='';});
 async function doSave(activate){
  save.disabled=true;$('#packet-activate-save').disabled=true;say(out,'Saving…','');
  try{const j=await api('/packets','POST',{files:packet,token,mission:($('#packet-mission')?.value||'').trim().toLowerCase()||null,activate});
   say(out,'Saved '+j.mission+' · '+j.revision+' · '+j.status+'.','ok');setTimeout(()=>location.href=base+'/packets/'+encodeURIComponent(j.mission),800);}
  catch(err){say(out,err.message,'bad');save.disabled=false;$('#packet-activate-save').disabled=false;}
 }
 save.addEventListener('click',()=>doSave(false));$('#packet-activate-save').addEventListener('click',()=>doSave(true));
}
// 0.24.0 Packet Composer: new session, conversation (the reply is computed in the background; this page polls), draft, preview, save.
const newBtn=$('#c-new');
if(newBtn)newBtn.addEventListener('click',async()=>{newBtn.disabled=true;say($('#c-msg'),'Starting…','');
 try{const j=await api('/compose','POST',{seed:($('#c-seed').value||'').trim()||null});location.href=base+'/compose/'+j.id;}catch(err){say($('#c-msg'),err.message,'bad');newBtn.disabled=false;}});
document.addEventListener('click',async e=>{const b=e.target.closest('button[data-compose-from]');if(!b)return;b.disabled=true;
 try{const j=await api('/compose','POST',{from_mission:b.dataset.composeFrom});location.href=base+'/compose/'+j.id;}catch(err){say($('#msg'),err.message,'bad');b.disabled=false;}});
const comp=$('#compose');
if(comp){
 const id=comp.dataset.id,chat=$('#c-chat'),file=$('#c-file'),checks=$('#c-checks'),busy=$('#c-busy'),send=$('#c-send'),save=$('#c-save');
 let timer=null;
 function render(g){
  chat.innerHTML=g.messages.map(m=>'<div class="m '+(m.role==='owner'?'owner':'assistant')+'">'+esc(m.text)+(m.questions&&m.questions.length?'<ul>'+m.questions.map(q=>'<li>'+esc(q)+'</li>').join('')+'</ul>':'')+'</div>').join('');
  chat.scrollTop=chat.scrollHeight;
  file.textContent=(g.files&&g.files['00-start.md'])||'';
  const rows=[];for(const x of g.errors||[])rows.push('<div class="msg bad">'+esc(x)+'</div>');
  const gaps=(g.gaps||[]);if(gaps.length)rows.push('<ul class="small gaps">'+gaps.map(x=>'<li class="'+x.level+'">'+(x.level==='missing'?'Missing: ':'Note: ')+esc(x.text)+'</li>').join('')+'</ul>');
  for(const w of (g.warnings||[]).slice(0,4))rows.push('<div class="msg warn">'+esc(w)+'</div>');
  checks.innerHTML=rows.join('');
  // 0.24.3 (B03): the heading and Save follow the server: never "ready" while a question is open or a fact is a placeholder.
  const r=$('#c-ready');r.textContent=g.ready?'ready to save':(g.save_blocked?'in progress':'can be saved as a draft');
  save.disabled=!!g.save_blocked||!!(g.errors&&g.errors.length)||g.busy;send.disabled=g.busy;busy.hidden=!g.busy;
  const why=$('#c-why');if(why){why.textContent=g.save_blocked||'';why.hidden=!g.save_blocked||g.busy;}
  $('#c-cost').textContent='Model cost this session: $'+Number(g.cost_usd||0).toFixed(2)+' of $'+Number(g.cap_usd||0).toFixed(2)+(g.ready||g.save_blocked?'':' · notes beside the draft stay open after saving');
  const files=$('#c-files');if(files)files.innerHTML=(g.attachments||[]).map(f=>'<li>'+esc(f.name)+' <span class="sub">'+Math.round(f.chars/100)/10+'k characters</span> <button class="small" data-detach="'+esc(f.name)+'">Remove</button></li>').join('')||'<li class="empty">none</li>';
  const db=$('#c-diff-box');if(db){const has=g.diff!=null&&g.diff!=='';db.hidden=!has;if(has)$('#c-diff').textContent=g.changed?g.diff:'(no changes yet)';}
  if(g.from_mission){save.textContent=g.changed?'Save as the next revision':'No changes yet';if(!g.changed)save.disabled=true;}
  const sel=$('#c-model');if(sel&&document.activeElement!==sel)sel.value=g.model_choice||'default';
  const now=$('#c-model-now');if(now)now.textContent='now: '+(g.model_label||'')+(g.model_why?' ('+g.model_why+')':'');
  if(g.busy&&!timer)timer=setTimeout(poll,1500);
 }
 const msel=$('#c-model');if(msel)msel.addEventListener('change',async()=>{try{const j=await api('/compose/'+id+'/model','POST',{model:msel.value});say($('#msg'),j.message,'ok');poll();}catch(err){say($('#msg'),err.message,'bad');}});
 async function poll(){timer=null;try{render(await api('/compose/'+id));}catch(err){say($('#msg'),err.message,'bad');}}
 poll();
 $('#c-form').addEventListener('submit',async e=>{e.preventDefault();const t=$('#c-text').value.trim();if(!t)return;send.disabled=true;
  try{await api('/compose/'+id+'/message','POST',{text:t});$('#c-text').value='';poll();}catch(err){say($('#msg'),err.message,'bad');send.disabled=false;}});
 $('#c-text').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();$('#c-form').requestSubmit();}});
 $('#c-preview').addEventListener('click',async()=>{const p=$('#c-plan');try{const j=await api('/compose/'+id+'/preview','POST',{});p.hidden=false;
  p.textContent=j.ok?(j.build_md?'BUILD.md the builder will get:\n\n'+j.build_md+'\n\n':'')+j.text+((j.approvals||[]).length?'\n\nWhat you will approve, step by step:\n'+j.approvals.map(x=>'• '+x).join('\n'):'\n\nNo step needs an approval (read-only work).'):'Fix first: '+(j.errors||[]).join(' ');}catch(err){say($('#msg'),err.message,'bad');}});
 const att=$('#c-attach');if(att)att.addEventListener('change',async()=>{for(const f of [...att.files]){if(!/\.(md|txt)$/i.test(f.name)){say($('#msg'),f.name+' is not a .md or .txt file.','bad');continue;}
  try{const j=await api('/compose/'+id+'/attach','POST',{name:f.name,text:await f.text()});say($('#msg'),j.message,'ok');}catch(err){say($('#msg'),err.message,'bad');}}att.value='';poll();});
 comp.addEventListener('click',async e=>{const b=e.target.closest('button[data-detach]');if(!b)return;e.preventDefault();
  try{const j=await api('/compose/'+id+'/attach','POST',{name:b.dataset.detach,remove:true});say($('#msg'),j.message,'ok');poll();}catch(err){say($('#msg'),err.message,'bad');}});
 save.addEventListener('click',async()=>{if(!confirm('Save this as a DRAFT packet? Nothing runs until you activate it on the packet page.'))return;save.disabled=true;
  try{const j=await api('/compose/'+id+'/save','POST',{});const h=j.build_handoff&&j.build_handoff.command;
   say($('#msg'),'Saved '+j.mission+' ('+j.revision+') as a draft.'+(h?' To build it, send '+h+' to your bot in Telegram (the BUILD REVIEW and approval line follow).':''),'ok');setTimeout(()=>location.href=base+'/packets/'+encodeURIComponent(j.mission)+(h?'?buildfrom=1':''),h?2500:800);}
  catch(err){say($('#msg'),err.message,'bad');save.disabled=false;}});
 $('#c-discard').addEventListener('click',async()=>{if(!confirm('Discard this session? The conversation and draft are deleted.'))return;
  try{await api('/compose/'+id+'/discard','POST',{});location.href=base+'/compose';}catch(err){say($('#msg'),err.message,'bad');}});
}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
})();
'''

PALETTE = ['#0b63ce', '#1d9e75', '#b26a00', '#7c4dff', '#b3261e', '#5d6370']


def esc(v):
    return html.escape(str(v if v is not None else ''), quote=True)


def fmt_k(n):
    n = n or 0
    if n >= 1e6:
        return '%.2fM' % (n / 1e6)
    if n >= 1e3:
        return '%dk' % round(n / 1e3)
    return str(int(n))


def fmt_bytes(n):
    if n is None:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%.0f %s' if unit in ('B', 'KB') else '%.1f %s') % (n, unit)
        n /= 1024.0


def fmt_usd(v):
    return '$%.2f' % (v or 0)


def chip(status, tone=''):
    s = str(status or '').lower().replace(' ', '_')
    return '<span class="chip %s%s">%s</span>' % (esc(s), (' ' + esc(tone)) if tone else '', esc(s.replace('_', ' ')))


def bar(fraction, warn=0.8):
    f = max(0.0, min(1.0, fraction or 0))
    cls = 'bad' if f >= 1 else 'warn' if f >= warn else ''
    return '<div class="bar-h"><i class="%s" style="width:%.0f%%"></i></div>' % (cls, f * 100)


def stacked_bars(days, series, width=600, height=160, fmt=fmt_k):
    """days: ['2026-11-01', …]; series: [(label, [v per day])]. Returns an SVG string with a legend."""
    if not days:
        return '<p class="empty">Nothing recorded yet.</p>'
    totals = [sum((s[1][i] or 0) for s in series) for i in range(len(days))]
    top = max(totals) or 1
    pad_l, pad_b, pad_t = 6, 22, 14
    n = len(days)
    slot = (width - pad_l * 2) / n
    bw = max(4, slot * 0.66)
    out = ['<svg class="chart" viewBox="0 0 %d %d" role="img" aria-label="daily totals">' % (width, height)]
    scale = (height - pad_b - pad_t) / top
    for i, day in enumerate(days):
        x = pad_l + slot * i + (slot - bw) / 2
        y = height - pad_b
        for k, (label, values) in enumerate(series):
            v = values[i] or 0
            if v <= 0:
                continue
            h = v * scale
            y -= h
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="1.5" fill="%s"><title>%s %s: %s</title></rect>' % (x, y, bw, h, PALETTE[k % len(PALETTE)], esc(day), esc(label), esc(fmt(v))))
        if totals[i] and n <= 16:
            out.append('<text x="%.1f" y="%.1f" font-size="9" text-anchor="middle" fill="currentColor" opacity=".7">%s</text>' % (x + bw / 2, y - 3, esc(fmt(totals[i]))))
        if n <= 16 or i % max(1, n // 8) == 0:
            out.append('<text x="%.1f" y="%d" font-size="9" text-anchor="middle" fill="currentColor" opacity=".6">%s</text>' % (x + bw / 2, height - 7, esc(day[5:])))
    out.append('</svg>')
    legend = ''.join('<span><i style="background:%s"></i>%s</span>' % (PALETTE[k % len(PALETTE)], esc(label)) for k, (label, vals) in enumerate(series) if any(vals))
    return ''.join(out) + ('<div class="legend">%s</div>' % legend if legend else '')


def line_chart(points, keys, width=600, height=130, labels=None, maxima=None, fmt=None):
    """points: [{t, k1, k2…}] oldest first; keys: ['cpu', …]. Each key is scaled by maxima[key] (or its own max)."""
    if not points:
        return '<p class="empty">No samples yet.</p>'
    pad = 6
    n = len(points)
    out = ['<svg class="chart" viewBox="0 0 %d %d" role="img">' % (width, height)]
    for y in (0.25, 0.5, 0.75):
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="currentColor" opacity=".12"/>' % (pad, width - pad, height * y, height * y))
    for k, key in enumerate(keys):
        vals = [p.get(key) for p in points]
        top = (maxima or {}).get(key) or max((v for v in vals if v is not None), default=0) or 1
        pts = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            x = pad + (width - 2 * pad) * (i / max(1, n - 1))
            y = height - pad - (height - 2 * pad) * min(1.0, v / top)
            pts.append('%.1f,%.1f' % (x, y))
        if pts:
            out.append('<polyline fill="none" stroke="%s" stroke-width="1.8" stroke-linejoin="round" points="%s"/>' % (PALETTE[k % len(PALETTE)], ' '.join(pts)))
    out.append('</svg>')
    if labels:
        out.append('<div class="legend">%s</div>' % ''.join('<span><i style="background:%s"></i>%s</span>' % (PALETTE[k % len(PALETTE)], esc(l)) for k, l in enumerate(labels)))
    return ''.join(out)


def donut(fraction, label, size=64):
    f = max(0.0, min(1.0, fraction or 0))
    r = size / 2 - 6
    c = 2 * math.pi * r
    color = '#b3261e' if f >= 0.9 else '#b26a00' if f >= 0.75 else '#1d9e75'
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="%s"><circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="currentColor" opacity=".12" stroke-width="8"/>'
            '<circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="%s" stroke-width="8" stroke-dasharray="%.1f %.1f" transform="rotate(-90 %d %d)" stroke-linecap="round"/>'
            '<text x="50%%" y="54%%" text-anchor="middle" font-size="13" font-weight="600" fill="currentColor">%d%%</text></svg>'
            % (size, size, size, size, esc(label), size // 2, size // 2, r, size // 2, size // 2, r, color, c * f, c, size // 2, size // 2, round(f * 100)))
