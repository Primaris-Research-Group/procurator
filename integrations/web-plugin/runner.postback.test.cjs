const test=require('node:test');
const assert=require('node:assert/strict');
const http=require('node:http');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot,digest,run}=require('./runner.cjs');

// Nov 4 MyLeaveco "Tell us about your absence from work" (ASP.NET WebForms, absenceentry.aspx).
// Picking the absence reason posts the whole page back to the server (AutoPostBack), which
// re-renders it with a new "Type of Episode or Care" box; the dates post back to compute the
// total hours. The bot's one batch (reason, dates, times, two Yes/No dropdowns) came back
// "uncertain" after the first postback and the two dropdowns were never set.
const REASONS=['Bonding With Child','Episode of Incapacity or General Care of Family Member','Medical Appointment','Other'];
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function page(v,{partial=false}={}){
  const sel=(id,label,opts,auto)=>`<div class="row"><label for="${id}">${label} *</label><br><select id="${id}" name="ctl00$Main$${id}"${auto?` onchange="__doPostBack('${id}')"`:''}>`+
    ['<option value="">- make a selection</option>',...opts.map(o=>`<option value="${esc(o)}"${v[id]===o?' selected':''}>${esc(o)}</option>`)].join('')+'</select></div>';
  const inp=(id,label,auto)=>`<div class="row"><label for="${id}">${label}</label><input id="${id}" name="ctl00$Main$${id}" value="${esc(v[id])}"${auto?` onchange="__doPostBack('${id}')"`:''}></div>`;
  const hours=v.begin&&v.end&&v.btime&&v.etime?'9':'';
  return `<!doctype html><html><head><title>MyLeaveco for Members</title></head><body><form id="aspnetForm" method="post" action="/absence">
  <h1>Tell us about your absence from work</h1>
  <input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="">
  ${sel('reason','Absence reason:',REASONS,!partial)}
  ${v.reason?inp('episode','Type of Episode or Care:'):''}
  ${inp('begin','Begin date:',true)}${inp('btime','Begin time:')}
  ${inp('end','End date:',true)}${inp('etime','End time:')}
  <div>Total Absent Time: <input id="hours" name="ctl00$Main$hours" value="${hours}" readonly> Hours</div>
  ${sel('lunch','Does the absence include unpaid Lunch/Dinner/Break?',['Yes','No'])}
  ${sel('overtime','Does the absence include any mandatory overtime?',['Yes','No'])}
  <button type="submit" name="ctl00$Main$btnSubmit" value="1">Submit Absence</button></form>
  <script>function __doPostBack(t){document.getElementById('__EVENTTARGET').value=t;
   ${partial?"":"setTimeout(()=>document.getElementById('aspnetForm').submit(),150);"}}</script></body></html>`;
}
async function server(t){
  const posts=[];
  const s=http.createServer((req,res)=>{
    let body='';req.on('data',c=>body+=c);req.on('end',()=>{
      const v={};for(const [k,x] of new URLSearchParams(body))v[k.replace('ctl00$Main$','')]=x;
      if(req.method==='POST')posts.push(v);
      res.writeHead(200,{'content-type':'text/html'});res.end(page(v));
    });
  });
  await new Promise(r=>s.listen(0,'127.0.0.1',r));t.after(()=>s.close());
  return {origin:'http://127.0.0.1:'+s.address().port,posts};
}
async function open(t,url){
  const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
  const p=await browser.newPage();await p.goto(url);return {browser,page:p};
}
const act=(browser,origin,before,steps)=>run({connectUrl:'unused',origin,operation:'act',expected:digest(before),steps},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));
const ctl=(snap,id)=>snap.controls.find(c=>c.id===id);

test('a whole batch survives full-page postbacks: reason, dates, times and both Yes/No dropdowns',async t=>{
  const {origin,posts}=await server(t);const {browser,page:p}=await open(t,origin+'/absence');
  const before=await snapshot(p);
  const steps=[{kind:'select',control:ctl(before,'reason'),value:'Episode of Incapacity or General Care of Family Member'},
    {kind:'fill',control:ctl(before,'begin'),value:'11/02/2026'},{kind:'fill',control:ctl(before,'btime'),value:'08:00 AM'},
    {kind:'fill',control:ctl(before,'end'),value:'11/02/2026'},{kind:'fill',control:ctl(before,'etime'),value:'05:00 PM'},
    {kind:'select',control:ctl(before,'lunch'),value:'No'},{kind:'select',control:ctl(before,'overtime'),value:'No'}];
  const r=await act(browser,origin,before,steps);
  assert.equal(r.ok,true,JSON.stringify(r).slice(0,400));
  assert.equal(await p.inputValue('#reason'),'Episode of Incapacity or General Care of Family Member');
  assert.equal(await p.inputValue('#begin'),'11/02/2026');assert.equal(await p.inputValue('#etime'),'05:00 PM');
  assert.equal(await p.inputValue('#lunch'),'No');assert.equal(await p.inputValue('#overtime'),'No');
  assert.ok(posts.length>=2,'the page really posted back');
  assert.ok(posts.every(x=>!x.btnSubmit),'Submit Absence was never pressed');
});

test('a dropdown whose own change posts back is confirmed after the reload',async t=>{
  const {origin}=await server(t);const {browser,page:p}=await open(t,origin+'/absence');
  const before=await snapshot(p);
  const r=await act(browser,origin,before,[{kind:'select',control:ctl(before,'reason'),value:'Medical Appointment'}]);
  assert.equal(r.ok,true,JSON.stringify(r).slice(0,400));
  assert.equal(await p.inputValue('#reason'),'Medical Appointment');
  assert.ok(r.snapshot.controls.find(c=>c.id==='episode'),'the new box after the postback is visible to the bot');
});

test('a field that disappeared or changed meaning still stops the batch safely',async t=>{
  const {origin}=await server(t);const {browser,page:p}=await open(t,origin+'/absence');
  const before=await snapshot(p);
  const fake={...ctl(before,'lunch'),label:'Something else entirely',ref:'e999'};
  const r=await act(browser,origin,before,[{kind:'select',control:fake,value:'No'}]);
  assert.equal(r.ok,false);assert.equal(r.reason,'page_changed');
  assert.equal(await p.inputValue('#lunch'),'');
});
