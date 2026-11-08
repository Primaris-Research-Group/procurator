/* Private, fixed CDP driver. Never exposed as a model tool or shell command. */
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH || '/app/node_modules/playwright-core');
const crypto=require('node:crypto');
const canonical=x=>Array.isArray(x)?x.map(canonical):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
// Display-only fields (change markers, saved-login notices) never invalidate an approval.
const VOLATILE=['new_refs','site_login_status','site_login_guidance'];
const stable=x=>x&&typeof x==='object'&&!Array.isArray(x)?Object.fromEntries(Object.entries(x).filter(([k])=>!VOLATILE.includes(k))):x;
const digest=x=>crypto.createHash('sha256').update(JSON.stringify(canonical(stable(x)))).digest('hex');
// Same page = same website and path (query strings and fragments change while a site works); used for routine steps.
const pageKey=u=>{try{const x=new URL(String(u||''));return crypto.createHash('sha256').update(x.origin.toLowerCase()+x.pathname.replace(/\/+$/,'')).digest('hex');}catch{return '';}};
const CONTROL_QUERY='input,textarea,select,button,a[href],[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex],[aria-haspopup]';
const PRACTICE='<!doctype html><html><head><title>The owner browser practice</title></head><body><h1>Practice appointment form</h1><p>This is a private practice form. No real reservation, appointment, application, or payment is made.</p><form id="practice"><label>Name <input name="name" required></label><label>Preferred time <select name="time"><option value="morning">Morning</option><option value="afternoon">Afternoon</option></select></label><button type="submit">Submit practice form</button></form><p id="confirmation" role="status"></p><script>document.getElementById("practice").addEventListener("submit",e=>{e.preventDefault();const d=new FormData(e.target);document.getElementById("confirmation").textContent="PRACTICE CONFIRMED: "+d.get("name")+" / "+d.get("time");});</script></body></html>';
// Choose a dropdown value the way the site expects. Native selects (also hidden ones behind a
// styled button) are set through the element and fire input/change; custom comboboxes are opened
// and the exact visible option is clicked; closed web components get keyboard type-ahead.
// Every path verifies the committed value; otherwise "Choice did not register".
// Mark the login boxes in one frame: the first visible password box and the username box before it
// (same form, else page), or for two-step sign-ins the visible username/email box alone.
async function markLogin(frame){
  return frame.evaluate(()=>{
    for(const e of document.querySelectorAll('[data-procurator-login]'))e.removeAttribute('data-procurator-login');
    const vis=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.disabled&&!e.readOnly;
    const all=[...document.querySelectorAll('input')].filter(vis);
    const pass=all.find(e=>e.type==='password');
    const userish=e=>['text','email','tel',''].includes(e.type)&&(/username|email/.test(e.autocomplete||'')||/user|email|login|account|member|id\b/i.test([e.name,e.id,e.placeholder,e.getAttribute('aria-label'),...[...(e.labels||[])].map(l=>l.textContent)].join(' '))||e.type==='email');
    let user=null;
    if(pass){
      const scope=pass.form?all.filter(e=>e.form===pass.form):all;
      const before=scope.filter(e=>e!==pass&&['text','email','tel',''].includes(e.type)&&(e.compareDocumentPosition(pass)&Node.DOCUMENT_POSITION_FOLLOWING));
      user=before.reverse().find(userish)||before[0]||null;
    } else user=all.find(userish)||null;
    if(pass)pass.setAttribute('data-procurator-login','pass');
    if(user)user.setAttribute('data-procurator-login','user');
    return {pass:!!pass,user:!!user};
  }).catch(()=>({pass:false,user:false}));
}
async function findLoginFrame(page){
  for(const frame of page.frames()){const m=await markLogin(frame);if(m.pass||m.user)return {frame,...m};}
  return null;
}
async function fillSecret(loc,value){
  await loc.fill(value,{timeout:6000}).catch(()=>{});
  const len=await loc.evaluate(e=>e.value.length).catch(()=>-1);
  if(len===value.length)return true;
  await loc.click({timeout:6000}).catch(()=>{});await loc.fill('',{timeout:3000}).catch(()=>{});
  await loc.pressSequentially(value,{delay:20,timeout:15000}).catch(()=>{});
  return (await loc.evaluate(e=>e.value.length).catch(()=>-1))===value.length;
}
async function ownerPage(context){
  const pages=context.pages();let page=pages.find(p=>p.url()!=='about:blank')||pages[0];
  for(const p of pages){if(await p.evaluate(()=>document.hasFocus()).catch(()=>false))page=p;}
  return page;
}
async function ownerView(context,input){
  const page=await ownerPage(context);if(!page)return {ok:false,reason:'no_page'};
  const size=await page.evaluate(()=>[window.innerWidth,window.innerHeight]).catch(()=>null);
  const [w,h]=Array.isArray(size)&&size[0]>0&&size[1]>0?size:[1280,720];
  if(input.operation==='owner_tap'){
    const x=Number(input.x),y=Number(input.y);
    if(!(x>=0&&x<=1&&y>=0&&y<=1))return {ok:false,reason:'bad_point'};
    await page.bringToFront().catch(()=>{});
    await page.mouse.click(Math.round(x*w),Math.round(y*h));
    await page.waitForTimeout(400);
    return {ok:true};
  }
  if(input.operation==='owner_scroll'){
    await page.mouse.move(Math.round(w/2),Math.round(h/2));
    await page.mouse.wheel(0,(input.direction==='up'?-1:1)*Math.round(h*0.7));
    await page.waitForTimeout(350);
    return {ok:true};
  }
  const shot=await page.screenshot({type:'jpeg',quality:55,fullPage:false,timeout:12000});
  let host='';try{host=new URL(page.url()).hostname.slice(0,120);}catch{}
  return {ok:true,image_b64:shot.toString('base64'),host};
}
async function ownerSignIn(context,input){
  const pages=context.pages();let page=pages.find(p=>p.url()!=='about:blank')||pages[0];
  for(const p of pages){if(await p.evaluate(()=>document.hasFocus()).catch(()=>false))page=p;}
  if(input.operation==='owner_code'){
    const code=String(input.code||'');
    for(const frame of page.frames()){
      const found=await frame.evaluate(()=>{
        for(const e of document.querySelectorAll('[data-procurator-login]'))e.removeAttribute('data-procurator-login');
        const vis=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.disabled&&!e.readOnly;
        const all=[...document.querySelectorAll('input')].filter(vis).filter(e=>!['hidden','checkbox','radio','submit','button'].includes(e.type));
        const words=e=>[e.name,e.id,e.placeholder,e.getAttribute('aria-label'),...[...(e.labels||[])].map(l=>l.textContent)].join(' ');
        const box=all.find(e=>e.autocomplete==='one-time-code')||all.find(e=>/code|otp|passcode|verif|token|\bpin\b/i.test(words(e)))||(all.includes(document.activeElement)?document.activeElement:null);
        if(box)box.setAttribute('data-procurator-login','code');return !!box;
      }).catch(()=>false);
      if(!found)continue;
      const box=frame.locator('[data-procurator-login="code"]').first();
      if(!(await fillSecret(box,code)))return {ok:false,reason:'not_filled'};
      await box.press('Enter',{timeout:5000}).catch(()=>{});
      await frame.evaluate(()=>{for(const e of document.querySelectorAll('[data-procurator-login]'))e.removeAttribute('data-procurator-login');}).catch(()=>{});
      return {ok:true,submitted:true};
    }
    return {ok:false,reason:'no_code_box'};
  }
  const username=String(input.username||''),password=String(input.password||'');
  let found=await findLoginFrame(page);
  if(!found)return {ok:false,reason:'no_login_form'};
  let steps='one_page';
  if(!found.pass){
    // Two-step sign-in: user ID first, then the password page.
    if(!username)return {ok:false,reason:'username_needed'};
    const u=found.frame.locator('[data-procurator-login="user"]').first();
    if(!(await fillSecret(u,username)))return {ok:false,reason:'not_filled'};
    await u.press('Enter',{timeout:5000}).catch(()=>{});
    steps='two_step';found=null;
    for(let i=0;i<20&&!(found&&found.pass);i++){await page.waitForTimeout(500);found=await findLoginFrame(page);}
    if(!found||!found.pass)return {ok:false,reason:'password_box_not_shown',steps};
  } else if(username&&found.user){
    const u=found.frame.locator('[data-procurator-login="user"]').first();
    if(!(await fillSecret(u,username)))return {ok:false,reason:'not_filled'};
  }
  const pw=found.frame.locator('[data-procurator-login="pass"]').first();
  if(!(await fillSecret(pw,password)))return {ok:false,reason:'not_filled'};
  await pw.press('Enter',{timeout:5000}).catch(()=>{});
  await found.frame.evaluate(()=>{for(const e of document.querySelectorAll('[data-procurator-login]'))e.removeAttribute('data-procurator-login');}).catch(()=>{});
  await page.waitForTimeout(1500);
  return {ok:true,submitted:true,steps};
}
// Same origin, or for a saved site login any HTTPS host of its domain (www → services.leaveco.example).
function inScope(input,u){
  try{const x=new URL(u);if(x.origin===input.origin)return true;const d=input.site_domain;
    return typeof d==='string'&&!!d&&x.protocol==='https:'&&(x.port===''||x.port==='443')&&(x.hostname===d||x.hostname.endsWith('.'+d));}catch{return false;}
}
const normText=x=>String(x??'').replace(/\s+/g,' ').trim().toLowerCase();
async function chooseOption(page,loc,live,value){
  if(live.tag==='select'){
    const opt=live.options.find(o=>o.value===value)||live.options.find(o=>normText(o.label)===normText(value));
    if(!opt)throw new Error('Choice did not register: no such option');
    // A server form may post the whole page back when the choice changes (ASP.NET AutoPostBack,
    // MyLeaveco Nov 4): wait for the reload, then check the select again by its id.
    const target=()=>live.id?page.locator('[id='+JSON.stringify(live.id)+']').first():loc;
    let t0=Date.now();
    const ok=async()=>{await quiet(page,t0);return (await target().inputValue().catch(()=>null))===opt.value;};
    if(!live.hidden_native){await loc.selectOption(opt.value,{timeout:6000}).catch(()=>{});if(await ok())return 'native';}
    t0=Date.now();await target().selectOption(opt.value,{force:true,timeout:6000}).catch(()=>{});
    t0=Date.now();if(!(await ok()))await target().evaluate((e,v)=>{Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,v);
      e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));},opt.value);
    if(!(await ok()))throw new Error('Choice did not register');
    return live.hidden_native?'hidden_native':'native_forced';
  }
  const want=normText(value);if(!want)throw new Error('Choice did not register: empty');
  // Closed web components hide their text from page scripts; the browser's accessibility tree
  // still reports the focused control's chosen value (e.g. combobox "Georgia").
  const axFocused=async()=>{try{const c=await page.context().newCDPSession(page);const t=await c.send('Accessibility.getFullAXTree');await c.detach().catch(()=>{});
    const f=t.nodes.filter(n=>(n.properties||[]).some(p=>p.name==='focused'&&p.value?.value)&&n.role?.value!=='RootWebArea').at(-1);
    return f?String(f.value?.value??''):'';}catch{return '';}};
  const shows=async()=>normText(await loc.evaluate(e=>[e.value??'',e.innerText||e.textContent||''].join(' | ')).catch(()=>'')).includes(want)||(live.custom_field&&normText(await axFocused())===want);
  const findOption=()=>page.evaluateHandle(want=>{
    const norm=x=>String(x??'').replace(/\s+/g,' ').trim().toLowerCase();
    const roots=[document];for(let i=0;i<roots.length;i++)for(const e of roots[i].querySelectorAll('*'))if(e.shadowRoot)roots.push(e.shadowRoot);
    const opts=roots.flatMap(r=>Array.from(r.querySelectorAll('[role="option"],mat-option,[role="menuitem"],[role="menuitemradio"]')))
      .filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[aria-hidden="true"]'));
    const exact=opts.filter(e=>norm(e.innerText||e.textContent)===want||norm(e.getAttribute('aria-label'))===want);
    if(exact.length===1)return exact[0];
    const starts=opts.filter(e=>norm(e.innerText||e.textContent).startsWith(want));
    return exact.length===0&&starts.length===1?starts[0]:null;
  },want);
  const typing=live.tag==='input'||live.tag==='textarea';
  await loc.click({timeout:6000});
  if(typing){await loc.fill('').catch(()=>{});await loc.pressSequentially(String(value),{delay:25});}
  await page.waitForTimeout(400);
  let option=(await findOption()).asElement();
  if(option){await option.click({timeout:6000});await page.waitForTimeout(300);if(await shows())return 'listbox';}
  // Closed component or keyboard-only widget: type-ahead on the focused control, then commit.
  if(!typing){await loc.focus().catch(()=>{});await page.keyboard.type(String(value),{delay:40});}
  await page.keyboard.press('Enter');await page.waitForTimeout(400);
  if(await shows())return 'keyboard';
  await page.keyboard.press('Escape').catch(()=>{});
  throw new Error('Choice did not register');
}
// One document's serializer (main page or an inlined frame). Runs inside the page.
const SERIALIZE=({query,projectScope,projectPreview,boxes,find})=>{
    const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[aria-hidden="true"],[inert]');
    const preview=projectPreview?Array.from(document.querySelectorAll('[role="dialog"],dialog[open]')).filter(visible).at(-1):null;
    const main=projectScope?(preview||document.querySelector('main,[role="main"]')):null;
    const roots=projectScope?(main?[main]:[]):[document];
    for(let index=0;index<roots.length;index++)for(const e of roots[index].querySelectorAll('*'))if(e.shadowRoot)roots.push(e.shadowRoot);
    const dialogs=roots.flatMap(root=>Array.from(root.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog[open]'))).filter(visible);
    const dialog=dialogs.at(-1);
    const inDialog=e=>{let current=e;while(current){if(dialog.contains(current))return true;current=current.getRootNode()?.host;}return false;};
    // Each selector segment belongs to one document/open shadow root. The model
    // supplies only refs; selectors are generated and frozen by the broker.
    function selectorFor(element){
      const parts=[];let e=element;
      while(e){
        const root=e.getRootNode();const siblings=Array.from(e.parentElement?.children||root.children).filter(s=>s.localName===e.localName);
        parts.unshift(CSS.escape(e.localName)+':nth-of-type('+(siblings.indexOf(e)+1)+')');
        if(e.parentElement)e=e.parentElement;
        else return (root instanceof ShadowRoot?selectorFor(root.host)+' >> ':'')+parts.join(' > ');
      }
    }
    const elements=[],newRefs=[];
    const KEY=Symbol.for('owner.ids');
    if(!window[KEY])Object.defineProperty(window,KEY,{value:{map:new WeakMap(),next:1,gen:0},enumerable:false});
    const REG=window[KEY];REG.gen++;
    // Section context: fieldset legend, labelled group/dialog/form, or nearest heading above in the same form.
    const groupOf=e=>{
      for(let p=e.parentElement,d=0;p&&d<15;p=p.parentElement,d++){
        if(p.tagName==='FIELDSET'){const l=p.querySelector(':scope > legend');if(l&&l.textContent.trim())return l.textContent.replace(/\s+/g,' ').trim().slice(0,80);}
        const role=p.getAttribute('role')||'';
        if(/^(group|radiogroup|dialog|region|form)$/.test(role)||p.tagName==='FORM'||p.tagName==='DIALOG'){
          const t=p.getAttribute('aria-label')||(p.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' ');
          if(t&&t.trim())return t.replace(/\s+/g,' ').trim().slice(0,80);
        }
      }
      return '';
    };
    const queried=roots.flatMap(root=>Array.from(root.querySelectorAll(query)));const seenQ=new Set(queried);
    // Web-component fields (closed shadow roots hide their inner <select>/<input>): form-associated
    // custom elements, or custom elements that expose a value and are labelled.
    const labelFor=e=>{if(!e.id)return '';const l=e.getRootNode().querySelector?.('label[for="'+CSS.escape(e.id)+'"]');return l?(l.textContent||''):'';};
    const customField=e=>e.localName.includes('-')&&!e.shadowRoot&&(customElements.get(e.localName)?.formAssociated||('value' in e&&(e.hasAttribute('label')||e.hasAttribute('aria-label')||labelFor(e))));
    const custom=roots.flatMap(root=>Array.from(root.querySelectorAll('*'))).filter(e=>!seenQ.has(e)&&customField(e));
    // A real <select> hidden behind a styled replacement (opacity 0, aria-hidden, display:none)
    // is still the field the site reads. Expose it when its label or wrapper is on screen.
    const shown=e=>e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
    const hiddenNative=e=>e.tagName==='SELECT'&&!e.disabled&&!e.closest('[inert]')&&(Array.from(e.labels||[]).some(shown)||shown(e.parentElement)||shown(e.parentElement?.parentElement));
    const inChrome=new Map();
    const labelText=l=>{const copy=l.cloneNode(true);for(const child of copy.querySelectorAll('input,textarea,select,button'))child.remove();return copy.textContent||'';};
    const controls=queried.concat(custom).map((e,index)=>{
      const hidden_native=!visible(e)&&hiddenNative(e);
      if((!visible(e)&&!hidden_native)||e.type==='hidden'||(dialog&&!inDialog(e)))return null;
      elements.push(e);
      const type=typeof e.type==='string'?e.type.toLowerCase():'';
      // A wrapping <label> contains the select's option text as well as its name.
      // Capture the field label itself so exact task grants remain stable.
      const labelledBy=(e.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean).map(id=>(e.getRootNode().getElementById?.(id)||document.getElementById(id))?.textContent||'').join(' ');
      const isCustom=custom.includes(e);
      const label=(e.getAttribute('aria-label')||labelledBy||Array.from(e.labels||[]).map(labelText).join(' ')||(isCustom||hidden_native?labelFor(e)||e.getAttribute('label'):'')||e.innerText||e.getAttribute('placeholder')||'').replace(/\s+/g,' ').trim().slice(0,300);
      const sensitive=type==='password'||/password|secret|token|ssn|social.security|card.number|credit.card|cvv|cvc|security.code|one.time|verification.code|\botp\b|cc-number|cc-csc|cc-exp/i.test([e.name,e.id,label,e.getAttribute('autocomplete')].join(' '));
      // Stable per-document element number (survives across snapshots while the node lives).
      let rec=REG.map.get(e);if(!rec){rec={n:REG.next++,gen:REG.gen};REG.map.set(e,rec);}
      if(rec.gen===REG.gen&&REG.gen>1)newRefs.push('e'+index);
      const describedBy=(e.getAttribute('aria-errormessage')||'')+' '+(e.getAttribute('aria-describedby')||'');
      const fieldError=e.getAttribute('aria-invalid')==='true'||(e.validity&&e.matches(':user-invalid'))?(describedBy.split(/\s+/).filter(Boolean).map(id=>(e.getRootNode().getElementById?.(id)||document.getElementById(id))?.textContent||'').join(' ').replace(/\s+/g,' ').trim().slice(0,200)||'invalid'):'';
      const hints={};
      if(e.getAttribute('placeholder'))hints.placeholder=String(e.getAttribute('placeholder')).slice(0,80);
      if(e.required||e.getAttribute('aria-required')==='true')hints.required=true;
      if(fieldError)hints.error=fieldError;
      // Phase 1 classifier: a warning the page attaches to a button ("This cannot be undone") via aria-describedby.
      else if(describedBy.trim()&&(e.tagName==='BUTTON'||type==='submit'||e.getAttribute('role')==='button'||e.tagName==='A')){const d=describedBy.split(/\s+/).filter(Boolean).map(id=>(e.getRootNode().getElementById?.(id)||document.getElementById(id))?.textContent||'').join(' ').replace(/\s+/g,' ').trim();if(d)hints.described=d.slice(0,160);}
      const fmt={date:'YYYY-MM-DD','datetime-local':'YYYY-MM-DDTHH:MM',time:'HH:MM',month:'YYYY-MM',week:'YYYY-W##'}[type];if(fmt)hints.format=fmt;
      if(e.getAttribute('role')==='combobox'||e.getAttribute('aria-autocomplete')){hints.combobox=true;hints.expanded=e.getAttribute('aria-expanded')==='true';}
      if(e.getAttribute('maxlength')>0)hints.maxlength=Number(e.getAttribute('maxlength'));
      const popup=e.getAttribute('aria-haspopup');if(popup&&popup!=='false')hints.haspopup=popup==='true'?'menu':popup.slice(0,20);
      if(hidden_native)hints.hidden_native=true;
      if(isCustom)hints.custom_field=true;
      const group=groupOf(e);if(group)hints.group=group;
      if(e.getRootNode() instanceof ShadowRoot)hints.in_shadow=true;
      if(boxes){const r=e.getBoundingClientRect();if(r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth)hints.box=[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)];}
      return {ref:'e'+index,stable_id:rec.n,selector:selectorFor(e),tag:e.tagName.toLowerCase(),type,role:e.getAttribute('role')||'',name:String(e.name||'').slice(0,300),id:String(e.id||'').slice(0,300),
        label,...hints,
        href:e.tagName==='A'?e.href:'',form_action:e.form?.action||'',disabled:!!e.disabled,checked:!!e.checked||e.getAttribute('aria-checked')==='true',
        pressed:e.getAttribute('aria-pressed'),selected:e.getAttribute('aria-selected')==='true',
        value:sensitive?'[manual entry only]':String(e.value??'').slice(0,500),value_truncated:!sensitive&&String(e.value||'').length>500,
        options:e.tagName==='SELECT'?Array.from(e.options).slice(0,300).map(o=>({value:o.value.slice(0,120),label:o.label.replace(/\s+/g,' ').trim().slice(0,80)})):[]};
    }).filter(Boolean);
    controls.forEach((c,i)=>inChrome.set(c,!!elements[i].closest('header,nav,footer,[role="banner"],[role="navigation"],[role="contentinfo"]')));
    // Repeated buttons ("Add to Order") need nearby item text so the model can
    // pick the right ref. Context is descriptive only; refs/selectors still bind.
    const counts={};for(const c of controls)counts[c.label]=(counts[c.label]||0)+1;
    controls.forEach((c,i)=>{
      const choice=c.tag==='input'&&['radio','checkbox'].includes(c.type);
      if(!c.label||counts[c.label]<2||(['input','textarea','select'].includes(c.tag)&&!choice))return;
      // The card for this item: the largest ancestor that contains no other control with the
      // same label (e.g. one store's slot list among several stores offering "2:30 PM").
      const twins=elements.filter((e,j)=>j!==i&&controls[j]&&controls[j].label===c.label);
      let card=elements[i];
      while(card.parentElement&&card.parentElement!==document.body&&!twins.some(t=>card.parentElement.contains(t)))card=card.parentElement;
      let t=(card.innerText||'').replace(/\s+/g,' ').trim();
      if(t.length<=c.label.length+8){let p=elements[i].parentElement;for(let depth=0;depth<6&&p;depth++,p=p.parentElement){const x=(p.innerText||'').replace(/\s+/g,' ').trim();if(x.length>c.label.length+8){t=x;break;}}}
      if(t.length>c.label.length+8)c.near=t.slice(0,160);
    });
    const actions=controls.filter(c=>c.tag!=='a'),links=controls.filter(c=>c.tag==='a');
    // Over the cap, drop header/nav/footer buttons before any form control (document order kept).
    // 0.20.7: `find` (a word or two from the model) puts matching controls first, so the Sort menu or a store picker on a
    // long results page is listed even when the page has far more than 120 controls.
    const want=String(find||'').trim().toLowerCase();
    const wanted=c=>!!want&&[c.label,c.name,c.near].some(v=>String(v||'').toLowerCase().includes(want));
    const capped=list=>{if(list.length<=120)return list;const hits=want?list.filter(wanted).slice(0,40):[];const rest=list.filter(c=>!hits.includes(c));let room=120-hits.length-rest.filter(c=>!inChrome.get(c)).length;return hits.concat(rest.filter(c=>!inChrome.get(c)||room-->0)).slice(0,120);};
    // Labelled fields the bot cannot operate (e.g. a dropdown drawn by a closed component). Lets the
    // model ask for takeover with the field's name instead of guessing, and records evidence.
    const gaps=[];
    const covers=x=>elements.some(e=>e===x||e.contains(x)||x.contains(e));
    for(const root of roots)for(const l of root.querySelectorAll('label[for]')){
      if(!visible(l)||(dialog&&!inDialog(l)))continue;
      const text=labelText(l).replace(/\s+/g,' ').trim().slice(0,80);if(!text)continue;
      const target=l.control||root.getElementById?.(l.htmlFor)||document.getElementById(l.htmlFor);
      if(target&&covers(target))continue;
      gaps.push({label:text,target:target?target.localName:'missing',why:!target?'label points to nothing':!visible(target)?'field is hidden':target.localName.includes('-')?'closed web component':'not a form control'});
      if(gaps.length>=8)break;
    }
    const errors=roots.flatMap(root=>Array.from(root.querySelectorAll('[role="alert"],[aria-live="assertive"],ps-alert-bar')))
      .filter(e=>visible(e)&&(!dialog||inDialog(e)))
      .filter(e=>e.tagName.toLowerCase()!=='ps-alert-bar'||e.querySelector('[aria-label="Error"]')||/not right|technical issues|unable|failed|error/i.test(e.textContent||''))
      .map(e=>({text:(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim().slice(0,500),
        // Site-marked errors (CVS ps-alert-bar, error/danger styling or labels) always count.
        marked:e.tagName.toLowerCase()==='ps-alert-bar'||!!(e.matches('[class*="error" i],[class*="danger" i],[aria-label*="error" i],[aria-invalid="true"]')||e.querySelector('[class*="error" i],[class*="danger" i],[aria-label*="error" i]'))}))
      .filter(x=>x.text);
    // Live regions also announce result counts and status ("5 of 18 locations near…").
    // Only text that reads like a failure blocks routine work; the rest is a notice.
    const ERROR_TEXT=/\b(error|invalid|required|unable|fail(?:ed|ure)?|not available|unavailable|try again|incorrect|problem|couldn'?t|could not|cannot|can'?t be|missing|expired|denied|declined|no longer|went wrong|not right|please (?:enter|select|choose|correct|check|provide|complete))\b/i;
    const isError=x=>x.marked||ERROR_TEXT.test(x.text);
    const notices=errors.filter(x=>!isError(x)).map(x=>x.text).slice(0,8);
    // 0.21.0 (health foundation; Nov 6 review): errors BOUND TO A FORM — an alert inside the form, or a field marked
    // aria-invalid — so that verify-before-retry can tell "this submission failed" from an unrelated page-wide message.
    const formOf=e=>{const f=e.closest?e.closest('form'):null;return f?(f.getAttribute('action')||f.action||'form'):'';};
    const formErrors=[];
    for(const e of roots.flatMap(root=>Array.from(root.querySelectorAll('[role="alert"],[aria-live="assertive"],ps-alert-bar,[aria-invalid="true"]')))){
      if(!visible(e)||(dialog&&!inDialog(e)))continue;
      const form=formOf(e);if(!form)continue;
      let t=(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
      if(e.getAttribute('aria-invalid')==='true'){const desc=(e.getAttribute('aria-describedby')||'').split(/\s+/).map(id=>document.getElementById(id)).filter(Boolean).map(d=>(d.innerText||d.textContent||'').trim()).join(' ');
        t=(((e.labels&&e.labels[0]?labelText(e.labels[0]):'')||e.getAttribute('aria-label')||e.name||'field').trim()+': '+(desc||'invalid')).replace(/\s+/g,' ').trim();}
      if(t&&(e.getAttribute('aria-invalid')==='true'||isError({marked:e.tagName.toLowerCase()==='ps-alert-bar'||!!e.matches('[class*="error" i],[class*="danger" i],[aria-label*="error" i]'),text:t})))formErrors.push({form:String(form).slice(0,300),text:t.slice(0,300)});
      if(formErrors.length>=8)break;
    }
    errors.splice(0,errors.length,...errors.filter(isError).map(x=>x.text).slice(0,8));
    // Preserve every form/action control before optional navigation links. Large
    // site headers/footers must not hide the ZIP field or relax form review.
    const allFrames=roots.flatMap(root=>Array.from(root.querySelectorAll('iframe')));
    const embeddedFrames=allFrames.filter(e=>visible(e)&&(!dialog||inDialog(e))).slice(0,8).map(e=>{
      let source=null,origin=null,reason='No public HTTPS source is available';
      try{
        const u=new URL(e.getAttribute('src')||'',location.href);origin=u.origin;
        const secret=/token|secret|password|session|auth|code|signature|jwt|key|sso/i;
        const privateHost=/^(?:localhost|\d+\.\d+\.\d+\.\d+)$|(?:\.local|\.internal|\.localhost)$/i.test(u.hostname)||u.hostname.includes(':');
        if(e.getAttribute('src')&&u.protocol==='https:'&&!privateHost&&!u.username&&!u.password&&!u.hash&&!Array.from(u.searchParams.keys()).some(k=>secret.test(k))){source=u.href;reason=null;}
      }catch{}
      return {title:(e.title||e.getAttribute('aria-label')||'Embedded page').slice(0,200),source_url:source,origin,
        can_open_for_read_only_inspection:!!source,source_withheld_reason:reason};
    });
    const scroll={y:Math.round(scrollY),viewport:innerHeight,page:document.documentElement.scrollHeight,more_below:document.documentElement.scrollHeight-scrollY-innerHeight>200};
    const cappedActions=capped(actions),foundLinks=want?links.filter(wanted).slice(0,20):[];
    return {url:location.href,title:document.title.slice(0,300),scroll,new_refs:REG.gen>1?newRefs.slice(0,60):[],text:((dialog||main||(projectScope?null:document.body))?.innerText||'').slice(0,12000),controls:cappedActions.concat(foundLinks,links.filter(l=>!foundLinks.includes(l))).slice(0,120),form_gaps:gaps,controls_truncated:actions.length>120,navigation_truncated:controls.length>120,total_control_count:controls.length,...(want?{find:want,find_matches:actions.filter(wanted).length+links.filter(wanted).length}:{}),
      frame_count:allFrames.length,embedded_frames:embeddedFrames,open_shadow_root_count:roots.length-1,page_errors:errors,page_notices:notices,form_errors:formErrors};
  };
// Phase 6 (0.18.0): frames and shadow roots. Same-origin frames (and frames from origins the task allows) are
// serialized too: their controls get refs f<N>e<i>, a frame-entering selector, frame:{origin,index}, and their text
// follows a |IFRAME origin| line. Open shadow roots were already traversed; their controls now carry in_shadow.
const RUN={frameOrigins:[],input:null};
async function elementPath(handle){
  return handle.evaluate(e=>{const parts=[];let x=e;while(x&&x.parentElement){const sib=Array.from(x.parentElement.children).filter(s=>s.localName===x.localName);parts.unshift(CSS.escape(x.localName)+':nth-of-type('+(sib.indexOf(x)+1)+')');x=x.parentElement;}
    return (x?CSS.escape(x.localName)+' > ':'')+parts.join(' > ');});
}
async function inlineFrames(page,result,{frameOrigins=[],boxes=false}={}){
  const main=page.mainFrame();let mainOrigin;try{mainOrigin=new URL(main.url()).origin;}catch{return;}
  const prefix=new Map([[main,'']]),offset=new Map([[main,[0,0]]]);let n=0;const inlined=[];
  const actions=result.controls.filter(c=>c.tag!=='a'),links=result.controls.filter(c=>c.tag==='a'),fActions=[],fLinks=[];
  for(const frame of page.frames()){
    if(frame===main||n>=4||!prefix.has(frame.parentFrame()))continue;
    let depth=0;for(let f=frame;f.parentFrame();f=f.parentFrame())depth++;if(depth>2)continue;
    const url=frame.url();let origin;try{origin=new URL(url).origin;}catch{continue;}
    const inherits=/^about:(blank|srcdoc)/.test(url);
    const ok=inherits||origin===mainOrigin||frameOrigins.includes(origin)||(RUN.input&&inScope(RUN.input,url));
    if(!ok)continue;
    try{
      const el=await frame.frameElement();const box=await el.boundingBox();if(!box||box.width<20||box.height<20)continue;
      const sel=prefix.get(frame.parentFrame())+(await elementPath(el))+' >> internal:control=enter-frame >> ';
      const sub=await frame.evaluate(SERIALIZE,{query:CONTROL_QUERY,projectScope:false,projectPreview:false,boxes});
      n++;prefix.set(frame,sel);const base=offset.get(frame.parentFrame())||[0,0],at=[base[0]+box.x,base[1]+box.y];offset.set(frame,at);
      const tag='f'+n,shown=inherits?mainOrigin:origin;
      for(const c of sub.controls){c.ref=tag+c.ref;c.stable_id=tag+':'+c.stable_id;c.selector=sel+c.selector;c.frame={origin:shown,index:n};if(c.box)c.box=[Math.round(c.box[0]+at[0]),Math.round(c.box[1]+at[1]),c.box[2],c.box[3]];(c.tag==='a'?fLinks:fActions).push(c);}
      result.new_refs=(result.new_refs||[]).concat((sub.new_refs||[]).map(r=>tag+r)).slice(0,60);
      if(sub.text)result.text=(result.text+'\n|IFRAME '+shown+'|\n'+sub.text.slice(0,3000)).slice(0,15000);
      result.page_errors=result.page_errors.concat(sub.page_errors.map(x=>'['+shown+'] '+x)).slice(0,10);
      result.page_notices=(result.page_notices||[]).concat((sub.page_notices||[]).map(x=>'['+shown+'] '+x)).slice(0,10);
      result.form_gaps=(result.form_gaps||[]).concat((sub.form_gaps||[]).map(g=>({...g,frame:shown}))).slice(0,10);
      inlined.push({index:n,origin:shown,controls:sub.controls.length});
    }catch{/* a frame that navigates away mid-read is skipped */}
  }
  if(!inlined.length)return;
  const all=actions.concat(fActions),cappedActions=all.slice(0,120);
  if(all.length>120)result.controls_truncated=true;
  result.controls=cappedActions.concat(links,fLinks).slice(0,120);
  if(links.length+fLinks.length+cappedActions.length>120)result.navigation_truncated=true;
  result.inlined_frames=inlined;
}
async function snapshot(page,{projectScope=false,projectPreview=false,boxes=false,frameOrigins=RUN.frameOrigins,find=RUN.input?.find}={}) {
  const result=await page.evaluate(SERIALIZE,{query:CONTROL_QUERY,projectScope,projectPreview,boxes,find:typeof find==='string'?find.slice(0,80):''});
  if(!projectScope)await inlineFrames(page,result,{frameOrigins,boxes});
  while(Buffer.byteLength(JSON.stringify(result))>100000&&result.controls.length){const removed=result.controls.pop();if(removed.tag!=='a')result.controls_truncated=true;else result.navigation_truncated=true;}
  if(!projectScope){const extra=await pageTools(page);if(extra.platform)result.platform=extra.platform;if(extra.tools)result.page_tools=extra.tools;}
  return {...result,readiness:readiness(result)};
}
// WebMCP (Nov 5 2026): some sites (every Shopify storefront today) register structured tools with the
// browser ("document.modelContext"). Listing them is read-only; the broker decides which may be called.
async function pageTools(page){
  return await Promise.race([page.evaluate(async()=>{
    const out={};
    try{if(window.Shopify&&window.Shopify.shop)out.platform='shopify';}catch{}
    const mc=document.modelContext||navigator.modelContext;
    if(mc&&typeof mc.getTools==='function'){
      try{const list=await mc.getTools();out.tools=(Array.isArray(list)?list:[]).slice(0,30).map(t=>({name:String(t?.name||'').slice(0,80),description:String(t?.description||'').replace(/\s+/g,' ').slice(0,200)})).filter(t=>t.name);}catch{}
    }
    return out;
  }).catch(()=>({})),new Promise(r=>setTimeout(()=>r({}),2500))]);
}
async function callPageTool(page,name,args){
  return await page.evaluate(async({name,json})=>{
    const mc=document.modelContext||navigator.modelContext;
    if(!mc||typeof mc.executeTool!=='function')return {ok:false,error:'This page does not offer structured tools.'};
    const tools=typeof mc.getTools==='function'?await mc.getTools():[];
    if(!(Array.isArray(tools)?tools:[]).some(t=>t&&t.name===name))return {ok:false,error:'The page does not offer the tool '+name+' right now.'};
    let r;
    try{r=mc.executeTool.length>=2?await mc.executeTool(name,json):await mc.executeTool({name,arguments:json});}catch(e){return {ok:false,error:String(e&&e.message||e).slice(0,300)};}
    let text;try{text=typeof r==='string'?r:JSON.stringify(r);}catch{text=String(r);}
    return {ok:true,result:String(text).slice(0,20000)};
  },{name,json:JSON.stringify(args||{})});
}
function readiness(snapshot,solveActive=false){
  const title=snapshot.title||'',text=snapshot.text||'';
  const challenge=/^just a moment[.!\s]*$|security verification|verify (?:that )?you(?:'re| are) (?:a )?human|performing security|confirm you(?:'re| are) a real user/i.test(title+'\n'+text);
  if(solveActive)return {state:'verification_in_progress',reason:'The provider reported an active verification solve. Do not click, close or take over while it is active.'};
  if(challenge)return {state:'verification_required',reason:'Verification is still visible after the bounded read. Preserve the session for supported recovery or owner takeover; do not claim a site-wide permanent block.'};
  if(snapshot.embedded_frames?.length&&/\bloading\b/i.test(text)&&snapshot.controls.length<=3)return {state:'embedded_content',reason:'The visible form is embedded. Inspect the public frame source instead of treating the outer page as the complete booking form.'};
  if(!snapshot.controls.length&&text.split('\n').map(x=>x.trim()).filter(x=>x&&!/^(?:loading(?:\.{3}|…)?|projects|\/)$/i.test(x)).length===0)return {state:'loading',reason:'The app is still showing its loading skeleton.'};
  if(!text.trim()&&!snapshot.controls.length)return {state:'loading',reason:'The rendered page has no readable text or controls yet.'};
  return {state:'ready'};
}
async function readableSnapshot(page,solve,options={}){
  const read=options.read||snapshot,now=options.now||Date.now,sleep=options.sleep||(ms=>new Promise(resolve=>setTimeout(resolve,ms)));
  const deadline=now()+Math.max(0,options.waitMs??15000);
  for(;;){
    const result=await read(page),state=readiness(result,solve.active);
    if(!['verification_in_progress','verification_required','loading'].includes(state.state)||now()>=deadline)return {...result,readiness:state};
    await sleep(Math.min(1000,Math.max(0,deadline-now())));
  }
}
async function locatorFor(page,control){
  if(typeof control.selector!=='string'||!control.selector)throw new Error('Missing reviewed selector');
  const locator=page.locator(control.selector);
  if(await locator.count()!==1)throw new Error('Reviewed control is ambiguous');
  return locator;
}
// Wait for a settled page: brief network quiet plus two animation frames (bounded).
async function settle(page,ms=2500){
  await page.waitForLoadState('networkidle',{timeout:ms}).catch(()=>{});
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r())))).catch(()=>{});
}
// Postbacks: classic server forms (ASP.NET WebForms, e.g. MyLeaveco's absence form) reload the whole
// page, or swap parts of it via AJAX, when a dropdown or date changes. Wait for that to finish
// before reading or touching the next field (Nov 4: the Leaveco batch went "uncertain" after the
// reason dropdown reloaded the page, and the two Yes/No dropdowns were never set).
const TRACK=new WeakMap();
function trackPage(page){
  if(!page||TRACK.has(page))return;
  const t={navs:0,inflight:new Map()};TRACK.set(page,t);
  page.on('framenavigated',f=>{if(f===page.mainFrame())t.navs++;});
  const counts=r=>{const k=r.resourceType();return k==='xhr'||k==='fetch'||(k==='document'&&r.frame()===page.mainFrame());};
  page.on('request',r=>{if(counts(r))t.inflight.set(r,Date.now());});
  const done=r=>t.inflight.delete(r);page.on('requestfinished',done);page.on('requestfailed',done);
}
async function quiet(page,since=Date.now(),{wait=350,limit=8000}={}){
  const t=TRACK.get(page);const navs=t?t.navs:0;
  await page.waitForTimeout(wait).catch(()=>{});
  if(!t)return false;
  const until=Date.now()+limit,mine=()=>[...t.inflight.values()].some(at=>at>=since-50);
  while(mine()&&Date.now()<until)await page.waitForTimeout(100).catch(()=>{});
  const reloaded=t.navs!==navs||t.navs>(t.seen??0);t.seen=t.navs;
  if(reloaded)await page.waitForLoadState('domcontentloaded',{timeout:15000}).catch(()=>{});
  await settle(page,1200);
  return reloaded;
}
async function snapshotSafe(page){
  try{return await snapshot(page);}
  catch(e){
    if(!/context was destroyed|navigat|detached|Target closed/i.test(String(e?.message)))throw e;
    await page.waitForLoadState('domcontentloaded',{timeout:15000}).catch(()=>{});await settle(page,1200);
    return snapshot(page);
  }
}
// The reviewed control in the live page. After a postback the page is rebuilt, so refs and
// positions shift, but a field keeps its id/name, label, type and options: fields (never buttons)
// may be re-found that way when exactly one control matches.
function findLive(current,ctl,kind){
  const same=c=>['tag','type','role','name','id','label','href','form_action'].every(k=>c[k]===ctl[k])&&JSON.stringify(c.options)===JSON.stringify(ctl.options);
  const byRef=current.controls.find(c=>c.ref===ctl.ref);
  if(byRef&&byRef.selector===ctl.selector&&same(byRef))return byRef;
  if((kind==='fill'||kind==='select')&&(ctl.id||ctl.name)){const m=current.controls.filter(same);if(m.length===1)return m[0];}
  return null;
}
const sameValue=(got,want)=>{
  if(got===want)return true;const a=String(got||''),b=String(want||'');
  const alnum=x=>x.replace(/[^0-9a-z]/gi,'').toLowerCase(),digits=x=>x.replace(/\D/g,'');
  return alnum(a)===alnum(b)||(digits(b).length>=4&&digits(a)===digits(b));
};
async function fillReviewedField(locator,value,control){
  const read=()=>locator.inputValue().catch(()=>null);
  const viaKeyboard=control.type==='tel'||/\b(date of birth|birth ?date|dob)\b/i.test(control.label);
  if(viaKeyboard){await locator.fill('');await locator.pressSequentially(value);}
  else await locator.fill(value);
  let got=await read(),method=viaKeyboard?'keyboard':'fill';
  // Masked inputs and framework-controlled fields can swallow a bulk fill: type it, then
  // fall back to the native value setter plus input/change events React/Vue listen for.
  if(got!==null&&!sameValue(got,value)&&!viaKeyboard){await locator.fill('');await locator.pressSequentially(value,{delay:25});got=await read();method='keyboard';}
  if(got!==null&&!sameValue(got,value)){
    await locator.evaluate((el,v)=>{const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto,'value').set.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));},value);
    got=await read();method='native_setter';
  }
  await locator.blur();
  return {ok:got===null||sameValue(got,value),method};
}
function previewPage(input,text,projectUrl){
  const offset=input.offset??0,limit=input.max_chars??12000,hash=crypto.createHash('sha256').update(text,'utf8').digest('hex');
  if(!Number.isInteger(offset)||offset<0||offset>2000000||!Number.isInteger(limit)||limit<1||limit>20000)throw new Error('Invalid preview page');
  if(offset>0&&input.expected_sha256!==hash)return {ok:false,kind:'file_changed'};
  let content=text.slice(offset,offset+limit);
  while(Buffer.byteLength(JSON.stringify(content))>64000)content=content.slice(0,Math.floor(content.length*.8));
  const next=offset+content.length;
  return {ok:true,file:{name:input.file_name,project_url:projectUrl,content,offset,next_offset:next<text.length?next:null,
    visible_text_chars:text.length,sha256:hash,extraction:'rendered_preview',original_file_bytes_downloaded:false,
    note:'Only text exposed by the site’s current preview is available; this is not a byte-for-byte file download.'}};
}
const CHAT_OPS=['chatgpt_chats','chatgpt_chat','claude_chats','claude_chat'];
const CHAT_LINK={chatgpt:'^https://chatgpt\\.com/(?:g/g-p-[a-zA-Z0-9-]+/)?c/[a-zA-Z0-9-]{8,100}$',claude:'^https://claude\\.ai/chat/[a-fA-F0-9-]{20,64}$'};
const TURNS={
  chatgpt:{all:'[data-conversation-role],[data-message-author-role],article[data-turn],[data-testid^="conversation-turn-"]',user:'[data-conversation-role="user"],[data-message-author-role="user"],[data-turn="user"]'},
  claude:{all:'[data-testid="user-message"],.font-claude-response,.font-claude-message,[data-testid="assistant-message"]',user:'[data-testid="user-message"]'}
};
// Exact name first; otherwise case/space-insensitive, still requiring one unique match.
const norm=x=>String(x||'').replace(/\s+/g,' ').trim().toLowerCase();
function pick(list,name,key='name'){
  const exact=list.filter(x=>x[key]===name);if(exact.length)return exact;
  return list.filter(x=>norm(x[key])===norm(name));
}
// Lists chat links inside the project's main area only (never the global sidebar).
// Read-only: the only interactions are the Chats tab (if present) and scrolling.
async function projectChats(page,service,deadline){
  const main=page.locator('main,[role="main"]').first();
  if(service==='chatgpt'){
    const tab=main.getByRole('tab',{name:'Chats',exact:true});
    if(await tab.count()===1&&await tab.getAttribute('aria-selected')!=='true')await tab.click({timeout:10000});
  }
  const pattern=CHAT_LINK[service];
  await page.waitForFunction(re=>{const m=document.querySelector('main,[role="main"]');if(!m)return false;
    return [...m.querySelectorAll('a[href]')].some(a=>new RegExp(re).test(a.href))||/no chats|start a (?:new )?chat|no conversations/i.test(m.innerText||'');},pattern,{timeout:Math.min(15000,Math.max(1000,deadline-Date.now()-8000))}).catch(()=>{});
  // 0.21.1 (Nov 7, "the agent can't see my newer chats"): ChatGPT first paints the project's chat list from its local cache and
  // replaces it with the server's list a moment later. Reading at the first paint returned an old list without the newest chats.
  // Wait for the network to go quiet, then re-read until the first rows are the same twice in a row.
  await page.waitForLoadState('networkidle',{timeout:Math.min(8000,Math.max(500,deadline-Date.now()-10000))}).catch(()=>{});
  const head=async()=>page.evaluate(re=>{const m=document.querySelector('main,[role="main"]');if(!m)return '';
    return [...m.querySelectorAll('a[href]')].map(a=>a.href.split('?')[0]).filter(u=>new RegExp(re).test(u)).slice(0,12).join('|');},pattern);
  let prev=await head();
  for(let i=0;i<5&&Date.now()<deadline-9000;i++){
    await page.waitForTimeout(1500);
    const now=await head();
    if(now&&now===prev)break;
    prev=now;
  }
  let chats=[];
  for(let i=0;i<10&&Date.now()<deadline-6000;i++){
    const found=await page.evaluate(re=>{
      const m=document.querySelector('main,[role="main"]');if(!m)return [];
      const seen=new Set(),out=[];
      // A date or relative time shown on the row ("Nov 6", "2 hours ago", "Yesterday", <time datetime>), so the order can be checked.
      const DATE=/^(?:today|yesterday|\d+\s*(?:m|min|minutes?|h|hrs?|hours?|d|days?|w|weeks?|mo|months?)\s*ago|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}\/\d{1,2}(?:\/\d{2,4})?)$/i;
      for(const a of m.querySelectorAll('a[href]')){
        const u=a.href.split('?')[0];if(!new RegExp(re).test(u)||seen.has(u))continue;seen.add(u);
        const row=a.closest('li,[role="listitem"]')||a;
        const t=row.querySelector('time');
        const lines=(a.innerText||a.textContent||'').split('\n').map(x=>x.trim()).filter(Boolean);
        const rowLines=(row.innerText||'').split('\n').map(x=>x.trim()).filter(Boolean);
        const when=(t&&(t.getAttribute('datetime')||t.innerText||'').trim())||rowLines.find(x=>DATE.test(x))||'';
        const title=(a.getAttribute('aria-label')||lines[0]||'').replace(/\s+/g,' ').trim().slice(0,300);
        out.push({title,url:u,position:out.length+1,...(when?{updated:when.slice(0,40)}:{}),preview:lines.slice(title===lines[0]?1:0).filter(x=>x!==when).join(' ').slice(0,200)});
      }
      const last=[...m.querySelectorAll('a[href]')].filter(a=>new RegExp(re).test(a.href.split('?')[0])).at(-1);
      last?.scrollIntoView({block:'end'});
      return out.slice(0,500);
    },pattern);
    if(found.length<=chats.length){chats=found.length?found:chats;break;}
    chats=found;await page.waitForTimeout(900);
  }
  return chats;
}
// Collects every turn, including text deferred by content-visibility, via the DOM.
async function chatTranscript(page,service,deadline){
  const spec=TURNS[service];
  const found=await page.waitForFunction(sel=>document.querySelectorAll(sel).length>0,spec.all,{timeout:Math.min(25000,Math.max(1000,deadline-Date.now()-7000))}).then(()=>true,()=>false);
  if(!found)return [];
  let last=-1;
  for(let i=0;i<8&&Date.now()<deadline-6000;i++){
    const n=await page.locator(spec.all).count();if(n===last)break;last=n;
    await page.evaluate(sel=>document.querySelector(sel)?.scrollIntoView({block:'start'}),spec.all);
    await page.waitForTimeout(700);
  }
  return page.evaluate(spec=>{
    const nodes=[...document.querySelectorAll(spec.all)].filter((e,_,all)=>!all.some(o=>o!==e&&o.contains(e)));
    const collect=n=>{
      if(n.nodeType===Node.TEXT_NODE)return n.nodeValue||'';
      if(n.nodeType!==Node.ELEMENT_NODE||['SCRIPT','STYLE','BUTTON','SVG','svg','INPUT','TEXTAREA'].includes(n.tagName)||n.getAttribute('aria-hidden')==='true'||n.classList.contains('sr-only'))return '';
      if(/^H[1-6]$/.test(n.tagName)&&/^(?:you|chatgpt|claude) said:?$/i.test((n.textContent||'').trim()))return '';
      const text=[...n.childNodes].map(collect).join('');
      return /^(?:P|H[1-6]|DIV|LI|TR|PRE|BLOCKQUOTE|TABLE|UL|OL)$/.test(n.tagName)?'\n'+text+'\n':text;
    };
    // Role from whichever role attribute the site currently uses, on the turn or inside it.
    const ROLE_ATTRS=['data-conversation-role','data-message-author-role','data-turn'];
    const roleOf=e=>{
      const r=ROLE_ATTRS.map(a=>e.getAttribute(a)||e.querySelector('['+a+']')?.getAttribute(a)).find(Boolean)||((e.matches(spec.user)||e.querySelector(spec.user))?'user':'assistant');
      return r==='user'?'User':r==='assistant'?'Assistant':r.charAt(0).toUpperCase()+r.slice(1,40);
    };
    // If a role marker holds no text itself, read the largest enclosing block that still
    // contains only that one marker (the site may tag a sibling of the message body).
    const main=document.querySelector('main,[role="main"]')||document.body;
    const blockFor=m=>{let c=m;while(c.parentElement&&c.parentElement!==main&&c.parentElement!==document.body&&nodes.filter(n=>c.parentElement.contains(n)).length===1)c=c.parentElement;return c;};
    const clean=x=>x.replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
    return nodes.map(e=>{let text=clean(collect(e));if(!text)text=clean(collect(blockFor(e)));return {role:roleOf(e),text};}).filter(t=>t.text);
  },spec);
}
// Structure only (tag/attribute names and counts), never conversation text: used to
// repair selectors when a site changes its markup.
async function domProbe(page,markerSelector='',rootSelector='main,[role="main"]'){
  try{return await Promise.race([page.evaluate(([markerSelector,rootSelector])=>{
    const root=[...document.querySelectorAll(rootSelector)].at(-1)||document.querySelector('main,[role="main"]')||document.body,count={},testids={},attrs={};
    const bump=(o,k)=>{o[k]=(o[k]||0)+1;};
    for(const e of root.querySelectorAll('*')){
      bump(count,e.tagName.toLowerCase());
      for(const a of e.getAttributeNames())if(a.startsWith('data-'))bump(attrs,a+(['data-turn','data-message-author-role','data-conversation-role','data-is-streaming'].includes(a)?'='+e.getAttribute(a):''));
      const t=e.getAttribute('data-testid');if(t)bump(testids,t.replace(/[0-9a-f-]{8,}|\d+/gi,'#'));
    }
    const top=o=>Object.fromEntries(Object.entries(o).sort((a,b)=>b[1]-a[1]).slice(0,30));
    return {path_kind:location.pathname.replace(/[0-9a-zA-Z-]{8,}/g,'#'),has_main:!!document.querySelector('main,[role="main"]'),
      main_text_chars:(root.innerText||'').length,articles:root.querySelectorAll('article').length,tags:top(count),data_testids:top(testids),data_attrs:top(attrs),
      markers:markerSelector?[...document.querySelectorAll(markerSelector)].slice(0,12).map(m=>{let depth=0,c=m;while(c&&c!==root){depth++;c=c.parentElement;}
        return {tag:m.tagName.toLowerCase(),own_text_chars:(m.textContent||'').trim().length,children:m.children.length,depth,parent_text_chars:(m.parentElement?.textContent||'').trim().length,parent_children:m.parentElement?.children.length||0};}):[]};
  },[markerSelector,rootSelector]),new Promise(r=>setTimeout(()=>r(null),4000))]);}catch{return null;}
}
const MAX_ORIGINAL=20*1024*1024;
const kindOf=b=>b.length>4&&b.subarray(0,4).toString('latin1')==='%PDF'?'pdf':b.length>4&&b[0]===0x50&&b[1]===0x4b?'docx':null;
// Full original bytes from a captured viewer response; if the viewer used range requests
// (206), re-fetch the same URL from inside the page (read-only GET, same session).
async function captureOriginal(page,responses){
  for(const r of responses.slice(-6).reverse()){
    try{
      const len=Number(r.headers()['content-length']||0);if(len>MAX_ORIGINAL)continue;
      if(r.status()===200){const b=await r.body();if(b.length<=MAX_ORIGINAL&&kindOf(b))return {kind:kindOf(b),bytes_b64:b.toString('base64'),size:b.length};}
      const b64=await page.evaluate(async([url,max])=>{
        const res=await fetch(url,{credentials:'include'});if(!res.ok)return null;
        const buf=new Uint8Array(await res.arrayBuffer());if(buf.length>max)return null;
        let s='';for(let i=0;i<buf.length;i+=32768)s+=String.fromCharCode.apply(null,buf.subarray(i,i+32768));return btoa(s);
      },[r.url(),MAX_ORIGINAL]).catch(()=>null);
      if(b64){const b=Buffer.from(b64,'base64');if(kindOf(b))return {kind:kindOf(b),bytes_b64:b64,size:b.length};}
    }catch{}
  }
  return null;
}
const MOBILE={width:412,height:915},DESKTOP={width:1280,height:800};
const LOGIN_URL=/sign.?in|log.?in|\/auth|\/sso|password|verify|mfa|otp/i;
async function probePoint(page,x,y){
  return page.evaluate(([x,y])=>{
    if(!(x>=0&&y>=0&&x<innerWidth&&y<innerHeight))return {in_viewport:false};
    let e=document.elementFromPoint(x,y);
    while(e&&e.shadowRoot){const inner=e.shadowRoot.elementFromPoint(x,y);if(!inner||inner===e)break;e=inner;}
    const hit=e&&(e.closest('button,a[href],input,select,textarea,label,[role="button"],[role="link"],[role="option"],[role="tab"],[role="menuitem"],[role="checkbox"],[role="radio"],[onclick],[tabindex]')||e);
    if(!hit)return {in_viewport:true,found:false};
    const label=(hit.getAttribute('aria-label')||hit.innerText||hit.value||hit.getAttribute('title')||hit.getAttribute('alt')||'').replace(/\s+/g,' ').trim().slice(0,120);
    return {in_viewport:true,found:true,tag:hit.tagName.toLowerCase(),type:typeof hit.type==='string'?hit.type.toLowerCase():'',role:hit.getAttribute('role')||'',label,
      href:hit.tagName==='A'?hit.href:'',form_action:hit.form?.action||'',described:(hit.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean).map(id=>document.getElementById(id)?.textContent||'').join(' ').replace(/\s+/g,' ').trim().slice(0,160)};
  },[x,y]);
}
// Phase 2 (0.17.0): traces and page archives go only to the broker's inbox; the broker moves them to its own state.
const INBOX_PREFIX='/run/procurator-web/inbox/';
function inboxPath(p,ext){return typeof p==='string'&&p.startsWith(process.env.PROCURATOR_INBOX_PREFIX||INBOX_PREFIX)&&!p.includes('..')&&p.endsWith(ext)?p:null;}
const within=(promise,ms)=>Promise.race([promise,new Promise(r=>setTimeout(()=>r('timeout'),ms))]);
async function run(input,progress=()=>{},connect=(url,options)=>chromium.connectOverCDP(url,options)){
  let browser,page,tracing=null;
  const deadline=Date.now()+55000,solve={active:false};
  let stage='connect',effects=0;
  const phase=value=>{stage=value;progress({event:'phase',stage});};
  // Announced synchronously immediately before each page effect. If none was
  // announced, nothing was typed or clicked and the action can be re-planned.
  const effect=()=>{effects++;progress({event:'effect_started'});};
  progress({event:'protocol',version:2});
  try {
    phase('connect');
    browser=await connect(input.connectUrl,{timeout:25000});
    const context=browser.contexts()[0];if(!context)throw new Error('No browser context');
    page=context.pages()[0]||await context.newPage();page.setDefaultTimeout(12000);
    RUN.input=input;RUN.frameOrigins=Array.isArray(input.frame_origins)?input.frame_origins.filter(x=>typeof x==='string').slice(0,4):[];
    // Phase 6 mobile mode: a narrow viewport only (no user-agent or device change: no fingerprint change).
    try{const v=await page.evaluate(()=>({width:innerWidth,height:innerHeight}));
      if(input.viewport==='mobile'&&!(v&&v.width===MOBILE.width&&v.height===MOBILE.height))await page.setViewportSize(MOBILE);
      else if(input.viewport!=='mobile'&&v&&v.width===MOBILE.width&&v.height===MOBILE.height)await page.setViewportSize(DESKTOP);}catch{}
    // Playwright trace of this act (DOM snapshots + screenshots) for /webtrace; never returned to the model.
    if(input.operation==='act'&&inboxPath(input.trace_path,'.zip')){try{if(await within(context.tracing.start({screenshots:true,snapshots:true}),4000)!=='timeout')tracing=context;}catch{}}
    if(input.operation==='look'){
      // Phase 3 vision sidecar: one viewport screenshot with numbered boxes on the snapshot's controls
      // (set-of-marks). The image goes to the broker only; the broker asks the vision model and returns text.
      phase('look');await settle(page,1200);
      const snap=await snapshot(page,{boxes:true});
      if(LOGIN_URL.test(page.url())||snap.controls.some(c=>c.type==='password'||c.value==='[manual entry only]'))return {ok:false,kind:'look_refused_signin'};
      const marks=snap.controls.filter(c=>c.box).slice(0,80).map(c=>({ref:c.ref,label:String(c.label||'').slice(0,60),tag:c.tag,type:c.type,box:c.box}));
      await page.evaluate(marks=>{const root=document.createElement('div');root.id='__procurator_marks';root.setAttribute('aria-hidden','true');
        root.style.cssText='position:fixed;inset:0;pointer-events:none;z-index:2147483647';
        for(const m of marks){const b=document.createElement('div');b.style.cssText=`position:fixed;left:${m.box[0]}px;top:${m.box[1]}px;width:${m.box[2]}px;height:${m.box[3]}px;outline:2px solid #ff2d55;box-sizing:border-box`;
          const t=document.createElement('span');t.textContent=m.ref;t.style.cssText='position:absolute;left:0;top:-14px;background:#ff2d55;color:#fff;font:bold 11px/14px monospace;padding:0 2px';b.appendChild(t);root.appendChild(b);}
        document.documentElement.appendChild(root);},marks);
      let shot;try{shot=await page.screenshot({type:'jpeg',quality:60,fullPage:false,timeout:15000});}
      finally{await page.evaluate(()=>document.getElementById('__procurator_marks')?.remove()).catch(()=>{});}
      const view=await page.evaluate(()=>({width:innerWidth,height:innerHeight,dpr:devicePixelRatio||1}));
      return {ok:true,image_b64:shot.toString('base64'),marks,viewport:view,url:page.url(),title:(await page.title()).slice(0,200),digest:digest(await snapshot(page))};
    }
    if(input.operation==='point'){
      // Read-only: what interactive element sits at (x,y) in the viewport.
      phase('point');
      return {ok:true,element:await probePoint(page,Number(input.x),Number(input.y))};
    }
    if(input.operation==='capture'){
      // Read-only page archive (MHTML) plus the serialized snapshot, for offline fixtures. Nothing is clicked or typed.
      const target=inboxPath(input.capture_path,'.mhtml');if(!target)return {ok:false,kind:'capture_refused'};
      await settle(page,800);
      const cdp=await context.newCDPSession(page);const {data}=await within(cdp.send('Page.captureSnapshot',{format:'mhtml'}),15000).then(x=>x==='timeout'?{}:x);
      if(!data)return {ok:false,kind:'capture_timeout'};
      if(data.length>15_000_000)return {ok:false,kind:'capture_too_large',bytes:data.length};
      require('node:fs').writeFileSync(target,data,{mode:0o640});
      return {ok:true,bytes:data.length,url:page.url(),snapshot:await snapshot(page)};
    }
    page.on('console',message=>{
      const text=message.text();
      if(text==='browserbase-solving-started')solve.active=true;
      else if(['browserbase-solving-finished','browserbase-solving-errored'].includes(text))solve.active=false;
    });
    const read=()=>readableSnapshot(page,solve,{read:()=>snapshot(page,{projectScope:!!input.project_scope,projectPreview:['project_file','claude_sources','claude_file'].includes(input.operation)}),waitMs:Math.min((input.wait_seconds??(input.operation==='init'?30:15))*1000,Math.max(0,deadline-Date.now()-4000))});
    const originOK=()=>input.practice?page.url()==='about:blank':inScope(input,page.url());
    if(input.operation==='owner_login'||input.operation==='owner_code'){
      // Owner sign-in relayed from the phone page (their password manager filled it there). The
      // driver finds the site's own login/code boxes, fills them and submits. Values are never
      // returned, read back or logged; only whether a form was found and submitted.
      phase(input.operation);
      return await ownerSignIn(context,input);
    }
    if(['owner_shot','owner_tap','owner_scroll'].includes(input.operation)){
      // Phone sign-in page (Nov 5): iPhone shows no keyboard while Browserbase's live view is embedded,
      // so the phone page shows this picture instead and relays their taps and scrolls. Owner-only: the
      // broker allows these for a pending owner sign-in session and never returns the picture to the bot.
      phase(input.operation);
      return await ownerView(context,input);
    }
    if(input.operation==='owner_keys'){
      // Owner typing relayed from the phone sign-in page into whatever field HE tapped in the
      // live view. The text is never returned, logged or read back (Browserbase live view has
      // no mobile keyboard). Only allowed by the broker for a pending owner sign-in session.
      phase('owner_keys');
      let target=page;
      for(const p of context.pages()){if(await p.evaluate(()=>document.hasFocus()).catch(()=>false))target=p;}
      const KEYS=['Tab','Enter','Backspace','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Space'];
      if(input.clear){await target.keyboard.press('ControlOrMeta+A');await target.keyboard.press('Backspace');}
      if(typeof input.text==='string'&&input.text)await target.keyboard.insertText(input.text);
      if(input.key){if(!KEYS.includes(input.key))throw new Error('Unsupported key');await target.keyboard.press(input.key);}
      return {ok:true};
    }
    if(['init','read_url','project_sources','project_file','project_catalog','claude_catalog','claude_sources','claude_file',...CHAT_OPS].includes(input.operation)) {
      phase('read_navigation');
      if(input.practice)await page.setContent(PRACTICE,{waitUntil:'domcontentloaded'});
      else {
        const target=input.operation==='init'?input.url:input.read_url;
        if(!inScope(input,target))throw new Error('Read navigation origin mismatch');
        if(!['project_sources','project_file','claude_sources','claude_file'].includes(input.operation)||page.url().split('?')[0]!==target.split('?')[0])await page.goto(target,{waitUntil:'domcontentloaded',timeout:25000});
        if(input.project_scope)await page.waitForFunction(()=>Array.from(document.querySelectorAll('main,[role="main"],[role="alert"]')).some(e=>(e.innerText||'').trim().length>0),{},{timeout:Math.min(20000,Math.max(1000,deadline-Date.now()-5000))});
        else await settle(page,2500);
      }
    }
    if(!originOK())return {ok:false,kind:'origin_changed',...(()=>{try{const u=new URL(page.url());u.hash='';return {host:u.hostname.slice(0,120),url:u.protocol==='https:'?u.href.slice(0,600):''};}catch{return {host:'',url:''};}})()};
    if(input.project_scope&&/\/(?:auth\/)?(?:login|signin|sign-in)(?:\/|$)/i.test(new URL(page.url()).pathname))return {ok:false,kind:'authentication_required'};
    const claudeCatalog=async()=>{
      await page.locator('main a[href*="/project/"]').first().waitFor({state:'visible',timeout:15000});
      return page.locator('main a[href]').evaluateAll(nodes=>nodes.slice(0,300).map(e=>({name:(e.getAttribute('aria-label')||e.innerText||'').replace(/\s+/g,' ').trim(),url:e.href}))
        .filter(x=>x.name&&/^https:\/\/claude\.ai\/project\/[a-fA-F0-9-]{20,64}\/?$/.test(x.url)));
    };
    if(['claude_catalog','claude_sources','claude_file'].includes(input.operation)){
      phase('claude_project_view');
      if(input.origin!=='https://claude.ai'||!input.project_scope)throw new Error('Unsupported Claude source view');
      if(input.operation==='claude_catalog')return {ok:true,catalog:await claudeCatalog()};
      if(input.project_name){
        const matches=pick(await claudeCatalog(),input.project_name);
        if(matches.length!==1)return {ok:false,kind:'project_name_unavailable',matches:matches.length,error_stage:stage};
        await page.goto(matches[0].url,{waitUntil:'domcontentloaded',timeout:25000});
      }
      await page.locator('[data-testid="project-body"]').waitFor({state:'visible',timeout:20000});
      const context=page.getByRole('dialog',{name:'Context',exact:true});
      if(!await context.isVisible()){
        await page.locator('[data-testid="project-body"] [data-testid="project-doc-uploader-dropdown-trigger"]').waitFor({state:'visible',timeout:20000});
        await page.getByRole('button',{name:'Show context',exact:true}).click({timeout:15000});
      }
      await context.waitFor({state:'visible',timeout:15000});
      const files=await context.locator('button[aria-label]').evaluateAll(nodes=>{
        const names=new Set(nodes.map(e=>e.getAttribute('aria-label')));
        return [...names].filter(name=>names.has('More options for '+name)).map(name=>({name}));
      });
      if(input.operation==='claude_file'){
        phase('claude_file_preview');
        if(!files.some(x=>x.name===input.file_name))return {ok:false,kind:'file_not_found'};
        const hasPreview=await page.evaluate(filename=>{
          const norm=x=>x.replace(/[_\s]+/g,' ').trim().toLowerCase(),stem=norm(filename.replace(/\.[^.]+$/,''));
          return [...document.querySelectorAll('[role="dialog"] h3')].some(h=>h.children.length>=2&&norm(h.children[0].textContent)===stem&&/\d[\d,.]*\s*(?:[kMGT]?B|bytes)/i.test(h.children[1].textContent));
        },input.file_name);
        if(!hasPreview)await context.getByRole('button',{name:input.file_name,exact:true}).press('Enter',{timeout:15000});
        await page.waitForFunction(filename=>{
          const norm=x=>x.replace(/[_\s]+/g,' ').trim().toLowerCase(),stem=norm(filename.replace(/\.[^.]+$/,''));
          const h=[...document.querySelectorAll('[role="dialog"] h3')].find(h=>h.children.length>=2&&norm(h.children[0].textContent)===stem&&/\d[\d,.]*\s*(?:[kMGT]?B|bytes)/i.test(h.children[1].textContent));
          const body=h?.parentElement?.nextElementSibling;
          return !!body&&(body.textContent||'').trim().length>0&&!/^loading(?:\.{3}|…)?$/i.test((body.textContent||'').trim());
        },input.file_name,{timeout:20000});
        const source=await page.evaluate(filename=>{
          const norm=x=>x.replace(/[_\s]+/g,' ').trim().toLowerCase(),stem=norm(filename.replace(/\.[^.]+$/,''));
          const h=[...document.querySelectorAll('[role="dialog"] h3')].find(h=>h.children.length>=2&&norm(h.children[0].textContent)===stem&&/\d[\d,.]*\s*(?:[kMGT]?B|bytes)/i.test(h.children[1].textContent));
          const body=h?.parentElement?.nextElementSibling;
          const collect=n=>{
            if(n.nodeType===Node.TEXT_NODE)return n.nodeValue||'';
            if(n.nodeType!==Node.ELEMENT_NODE||['SCRIPT','STYLE','BUTTON','SVG','INPUT'].includes(n.tagName)||n.getAttribute('aria-hidden')==='true')return '';
            if(n.tagName==='TEXTAREA')return n.value||'';
            const text=[...n.childNodes].map(collect).join('');return /^(?:P|H[1-6]|DIV|LI|TR|PRE|BLOCKQUOTE)$/.test(n.tagName)?'\n'+text+'\n':text;
          };
          const text=body?collect(body).replace(/\n{3,}/g,'\n\n').trim().slice(0,2000000):'';
          return {text,media:!!body?.querySelector('img,canvas,iframe,object')};
        },input.file_name);
        if(!source.text||source.media&&source.text.length<100||source.text.length<500&&/^(?:loading|preview (?:is )?(?:not|un)available|unable to (?:load|preview)|cannot (?:display|preview))/i.test(source.text))return {ok:false,kind:'file_preview_not_readable'};
        return previewPage(input,source.text,page.url().split('?')[0]);
      }
      return {ok:true,files,snapshot:{url:page.url(),title:await page.title(),text:files.map(x=>x.name).join('\n'),controls:[],readiness:{state:'ready'},scope:'Project Context file list only'}};
    }
    const catalog=async()=>{
      const selector='[data-app-action-sidebar-project-id][data-app-action-sidebar-project-label]';
      await page.locator(selector).first().waitFor({state:'visible',timeout:15000});
      return page.locator(selector).evaluateAll(nodes=>nodes.slice(0,100).filter(e=>e.getClientRects().length).map(e=>({
        name:e.getAttribute('data-app-action-sidebar-project-label'),id:e.getAttribute('data-app-action-sidebar-project-id')
      })).filter(x=>x.name&&/^g-p-[a-zA-Z0-9-]{8,100}$/.test(x.id)).map(x=>({name:x.name,url:'https://chatgpt.com/g/'+x.id+'/project'})));
    };
    if(CHAT_OPS.includes(input.operation)){
      const service=input.operation.startsWith('claude')?'claude':'chatgpt';
      phase(service+'_chats');
      if(input.origin!==(service==='claude'?'https://claude.ai':'https://chatgpt.com')||!input.project_scope)throw new Error('Unsupported chat view');
      const listOp=input.operation.endsWith('_chats'),byUrl=new RegExp(CHAT_LINK[service]).test(page.url().split('?')[0]);
      if(listOp||!byUrl){
        if(input.project_name){
          const matches=pick(await (service==='claude'?claudeCatalog():catalog()),input.project_name);
          if(matches.length!==1)return {ok:false,kind:'project_name_unavailable',matches:matches.length,error_stage:stage};
          await page.goto(matches[0].url,{waitUntil:'domcontentloaded',timeout:25000});
        }
        const projectUrl=page.url().split('?')[0],chats=await projectChats(page,service,deadline);
        if(!originOK())return {ok:false,kind:'origin_changed'};
        if(listOp)return {ok:true,chats,project_url:projectUrl,complete:Date.now()<deadline-6000};
        const matches=pick(chats,input.chat_title,'title');
        if(matches.length!==1)return {ok:false,kind:'chat_not_found',matches:matches.length,available:chats.slice(0,40).map(x=>x.title),error_stage:stage};
        await page.goto(matches[0].url,{waitUntil:'domcontentloaded',timeout:25000});
      }
      phase(service+'_chat_read');
      if(!originOK())return {ok:false,kind:'origin_changed'};
      const turns=await chatTranscript(page,service,deadline);
      if(!turns.length)return {ok:false,kind:'chat_not_readable',error_stage:stage,dom_probe:await domProbe(page,TURNS[service].all)};
      const title=(input.chat_title||(await page.title()).replace(/\s*[-|–]\s*(?:Claude|ChatGPT)\s*$/i,'')).trim().slice(0,300)||'Untitled chat';
      const text=turns.map(t=>'['+t.role+']\n'+t.text).join('\n\n');
      const r=previewPage({...input,file_name:title},text,page.url().split('?')[0]);
      if(!r.ok)return r;
      const {name,...rest}=r.file;
      return {ok:true,chat:{title,...rest,chat_url:rest.project_url,turns:turns.length,extraction:'rendered_chat',
        note:'Rendered conversation text only. Files attached inside the chat are named in the transcript but not opened.'}};
    }
    if(input.operation==='project_catalog'){
      if(input.origin!=='https://chatgpt.com'||!input.project_scope)throw new Error('Unsupported catalog');
      return {ok:true,catalog:await catalog()};
    }
    if(['project_sources','project_file'].includes(input.operation)){
      phase('project_sources_view');
      if(input.origin!=='https://chatgpt.com'||!input.project_scope)throw new Error('Unsupported source view');
      if(input.project_name){
        const matches=pick(await catalog(),input.project_name);
        if(matches.length!==1)return {ok:false,kind:'project_name_unavailable',matches:matches.length,error_stage:stage};
        await page.goto(matches[0].url,{waitUntil:'domcontentloaded',timeout:25000});
      }
      const tab=page.locator('main,[role="main"]').getByRole('tab',{name:'Sources',exact:true});
      if(await tab.getAttribute('aria-selected')!=='true')await tab.click({timeout:15000});
      await page.getByRole('tabpanel',{name:'Sources',exact:true}).waitFor({state:'visible',timeout:15000});
      if(!originOK())return {ok:false,kind:'origin_changed'};
      if(input.operation==='project_file'){
        phase('project_preview');
        if(typeof input.file_name!=='string'||!input.file_name.trim()||input.file_name.length>250||/[\x00-\x1f\x7f]/.test(input.file_name))throw new Error('Invalid project filename');
        // A hover card or overlay can intercept pointer clicks on some rows (observed on a
        // .docx row). Fall back to keyboard activation, then a DOM click on the same button.
        // Read-only capture of the original document the viewer itself loads (PDF/DOCX), so
        // canvas-rendered or scanned previews can be extracted server-side. No download click.
        const docResponses=[];
        const onResponse=r=>{try{
          const type=(r.headers()['content-type']||'').toLowerCase(),url=r.url();
          if(/application\/pdf|wordprocessingml|application\/octet-stream/.test(type)||/\.(?:pdf|docx)(?:$|\?)/i.test(url))docResponses.push(r);
        }catch{}};
        page.on('response',onResponse);
        const previewButton=page.getByRole('tabpanel',{name:'Sources',exact:true}).getByRole('button',{name:'Preview '+input.file_name,exact:true});
        if(await previewButton.count()!==1)return {ok:false,kind:'file_not_found',matches:await previewButton.count(),error_stage:stage};
        try{await previewButton.click({timeout:6000});}
        catch{try{await previewButton.focus({timeout:3000});await previewButton.press('Enter',{timeout:3000});}
          catch{await previewButton.dispatchEvent('click');}}
        phase('project_preview_wait');
        // Poll the open viewer until its text stops changing. Text may sit beside the header,
        // anywhere in the viewer dialog, in a PDF text layer, or inside an embedded frame.
        const until=Date.now()+Math.min(35000,Math.max(3000,deadline-Date.now()-8000));
        let last=null,source=null,seen={header:false,media:false,frames:0};
        while(Date.now()<until){
          const s=await page.evaluate(()=>{
            const h=document.querySelector('[data-testid="viewer-header"]');
            const dialogs=[...document.querySelectorAll('[role="dialog"],dialog[open]')].filter(e=>e.getClientRects().length);
            const container=h?(h.closest('[role="dialog"],dialog')||h.parentElement):dialogs.at(-1);
            if(!container)return null;
            const collect=n=>{
              if(n.nodeType===Node.TEXT_NODE)return n.nodeValue||'';
              if(n.nodeType!==Node.ELEMENT_NODE||n===h||['SCRIPT','STYLE','BUTTON','SVG','svg','INPUT','IFRAME','NOSCRIPT'].includes(n.tagName)||n.getAttribute('aria-hidden')==='true'&&!n.closest('.textLayer'))return '';
              const t=[...n.childNodes].map(collect).join('');
              return /^(?:P|H[1-6]|DIV|LI|TR|PRE|BLOCKQUOTE|TABLE|BR)$/.test(n.tagName)?'\n'+t+'\n':t;
            };
            // Type-specific panels (e.g. data-testid="docx-preview-panel") can sit outside the header's parent.
            const panels=[...document.querySelectorAll('[data-testid$="preview-panel"],[data-testid*="-preview-content"]')].filter(e=>e.getClientRects().length);
            const sibling=h?.nextElementSibling;
            const candidates=[...panels.map(collect),sibling?collect(sibling):'',collect(container)].map(t=>t.replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim());
            const text=candidates.reduce((a,b)=>b.length>a.length?b:a,'');
            const scope=[container,...panels];
            return {text:text.slice(0,2000000),header:!!h,media:scope.some(e=>e.querySelector('img,canvas,video,embed,object')),frames:scope.reduce((n,e)=>n+e.querySelectorAll('iframe').length,0),
              zoom:!!document.querySelector('[data-testid$="preview-zoom-trigger"]')};
          });
          if(s){
            seen={...seen,header:seen.header||s.header,media:seen.media||s.media,frames:Math.max(seen.frames,s.frames)};
            let text=s.text;
            if(!text&&s.frames){
              for(const f of page.frames()){if(f===page.mainFrame())continue;
                const t=await f.evaluate(()=>(document.body?.innerText||'').trim()).catch(()=>'');if(t.length>text.length)text=t;}
            }
            if(!text&&s.media&&s.zoom&&!seen.zoomed&&Date.now()>until-25000){
              seen.zoomed=true;await page.locator('[data-testid$="preview-zoom-trigger"]').first().click({timeout:5000}).catch(()=>{});
            }
            const placeholder=/^(?:loading(?:\.{3}|…)?|preview (?:is )?(?:not|un)available|unable to (?:load|preview))\b/i.test(text);
            if(text&&!placeholder&&text===last){source={text};break;}
            last=placeholder?null:text;
          }
          await page.waitForTimeout(1200);
        }
        if(!source&&last)source={text:last};
        page.off('response',onResponse);
        if(!source){
          const original=await captureOriginal(page,docResponses);
          if(original)return {ok:true,original,file_name:input.file_name,project_url:page.url().split('?')[0]};
        }
        const viewer=source?null:await page.evaluate(()=>{
          const h=document.querySelector('[data-testid="viewer-header"]');
          const panel=document.querySelector('[data-testid$="preview-panel"]');
          const tags={};for(const e of panel?panel.querySelectorAll('*'):[])tags[e.tagName.toLowerCase()]=(tags[e.tagName.toLowerCase()]||0)+1;
          const imgs=[...(panel||document).querySelectorAll('img')].slice(0,5).map(i=>({w:i.naturalWidth,h:i.naturalHeight,src_kind:(i.currentSrc||i.src||'').split(':')[0]}));
          const root=h?.closest('[role="dialog"],aside,section')||h?.parentElement?.parentElement||document.body;
          const buttons=[...root.querySelectorAll('button,a[href]')].map(b=>(b.getAttribute('aria-label')||b.getAttribute('data-testid')||'').slice(0,60)).filter(Boolean).slice(0,25);
          return {panel_found:!!panel,panel_tags:tags,panel_text_chars:(panel?.textContent||'').trim().length,images:imgs,viewer_buttons:buttons};
        }).catch(()=>null);
        if(!source)return {ok:false,kind:'file_preview_not_readable',error_stage:stage,viewer_opened:seen.header,image_only:seen.media&&!seen.frames,frames:seen.frames,zoom_tried:!!seen.zoomed,viewer,
          dom_probe:await domProbe(page,'','[role="dialog"],dialog[open]'),
          note:seen.media?'The preview shows images only (likely a scanned document); text extraction needs OCR.':'No preview text appeared.'};
        return previewPage(input,source.text,page.url().split('?')[0]);
      }
    }
    if(input.operation==='page_tool'){
      phase('page_tool');await settle(page,1500);
      const r=await callPageTool(page,String(input.tool||''),input.tool_args||{});
      return r.ok?{ok:true,result:r.result,url:page.url()}:{ok:false,kind:'page_tool_failed',error:r.error};
    }
    if(input.operation==='frame'){
      // 0.28.0 (B11): one Live frame while someone watches. Hidden sites (banks, payment, health portals) send a placeholder;
      // every other page is captured with password, card and one-time-code fields masked. Nothing is written to disk here.
      const r=input.view_rules||{},u=new URL(page.url()),host=u.hostname.toLowerCase().replace(/^www\./,'');
      const title=String(await page.title().catch(()=>'')).slice(0,120);
      const hidden=(r.hide_domains||[]).some(d=>host===d||host.endsWith('.'+d))||(r.hide_hints||[]).some(h=>host.includes(h));
      if(hidden&&!input.reveal)return {ok:true,placeholder:{domain:host,title,reason:'hidden from Live (bank, payment or health portal)'},url_host:host,view:'hide'};
      const masked=!input.reveal;
      const mask=masked?(r.mask_selectors||[]).map(sel=>page.locator(sel)):[];
      const signin=r.signin_path?new RegExp(r.signin_path,'i').test(u.pathname):false;
      const shot=await page.screenshot({type:'jpeg',quality:50,scale:'css',fullPage:false,timeout:5000,mask,maskColor:'#888'});
      return {ok:true,image_b64:shot.toString('base64'),url_host:host,view:hidden?'hide':signin?'mask':'show'};
    }
    if(input.operation==='screenshot'){
      phase('screenshot');await settle(page,1500);
      const shot=await page.screenshot({type:'jpeg',quality:60,fullPage:false,timeout:15000});
      return {ok:true,image_b64:shot.toString('base64'),url:page.url(),title:(await page.title()).slice(0,200)};
    }
    phase('snapshot_before');let before=input.operation==='act'?await snapshot(page):await read();
    if(input.operation!=='act')return {ok:true,snapshot:before};
    if(input.operation==='act') {
      // 0.20.7 (Nov 7 browser batteries): routine steps (typing, choosing, navigate/neutral clicks, navigation) were rejected as
      // "stale" on OpenTable, Walmart, Booking.com, USPS and Costco because the whole-page digest (text, scroll, every control)
      // changed between the broker's read and the act. The broker now says how strict to be: expected_mode 'none' (navigate
      // only: nothing on this page is pressed), 'page' (same website path; each control is still matched live below), or
      // 'full' (final/commit/protected steps and owner-approved reviews keep the exact-page check).
      const mode=input.expected_mode||'full';
      if(mode==='full'){if(digest(before)!==input.expected)return {ok:false,kind:'stale'};}
      else if(mode==='page'){if(pageKey(before.url)!==input.expected_page)return {ok:false,kind:'stale',moved_to:String(before.url||'').slice(0,600)};}
      phase('action');var fills=[],stepsDone=0,lastEffect=Date.now();trackPage(page);
      for(const step of input.steps) {
        if(step.kind==='navigate') {
          if(!inScope(input,step.url))throw new Error('Origin mismatch');
          effect();await page.goto(step.url,{waitUntil:'domcontentloaded',timeout:25000});
        } else if(step.kind==='click_xy') {
          // Phase 3: a click on a point the vision check found, only if the same element is still there.
          const now=await probePoint(page,step.x,step.y);
          if(!now.found||normText(now.label)!==normText(step.probe?.label)||now.tag!==step.probe?.tag)throw new Error('Control changed');
          effect();await page.mouse.click(step.x,step.y);
        } else if(step.kind==='upload') {
          // Phase 6: an owner-approved file, read from the broker inbox, attached under its own name.
          const file=inboxPath(step.file_path,'.bin');if(!file)throw new Error('Manual input required');
          const payload={name:String(step.file?.name||'document').slice(0,120),mimeType:String(step.file?.mime||'application/octet-stream').slice(0,100),buffer:require('node:fs').readFileSync(file)};
          const current=await snapshotSafe(page);const live=findLive(current,step.control,'click');if(!live)throw new Error('Control changed');
          const loc=await locatorFor(page,live);
          if(live.tag==='input'&&live.type==='file'){effect();await loc.setInputFiles(payload);}
          else{const chooser=page.waitForEvent('filechooser',{timeout:8000});effect();await loc.click({timeout:15000});await (await chooser).setFiles(payload);}
          fills.push({ref:live.ref,ok:true,method:'upload'});
        } else {
          const current=await snapshotSafe(page);const live=findLive(current,step.control,step.kind);
          if(!live)throw new Error('Control changed');
          if(live.type==='password' || live.type==='file' || live.disabled)throw new Error('Manual input required');
          const loc=await locatorFor(page,live);
          if(step.kind==='fill'){effect();const f=await fillReviewedField(loc,step.value,live);fills.push({ref:live.ref,ok:f.ok,method:f.method});}
          else if(step.kind==='select'){effect();const method=await chooseOption(page,loc,live,step.value);fills.push({ref:live.ref,ok:true,method});}
          else if(step.kind==='click'){
            // Trial runs Playwright's actionability checks without clicking: a
            // covered/hidden button fails here and is reported as not clicked.
            let target=loc;
            try{await loc.click({trial:true,timeout:6000});}
            catch(error){
              // Styled radios/checkboxes hide the real input behind its label (observed on
              // CVS time slots). Click that label, the way a person does.
              if(!(live.tag==='input'&&['radio','checkbox'].includes(live.type)))throw error;
              const handle=await loc.evaluateHandle(e=>(e.labels&&e.labels[0])||e.closest('label')||(e.id&&document.querySelector('label[for="'+CSS.escape(e.id)+'"]'))||null);
              target=handle.asElement();
              if(!target)throw new Error('Hidden choice has no clickable label');
              await target.click({trial:true,timeout:6000});
            }
            effect();await target.click({timeout:15000});
            if(live.tag==='input'&&live.type==='radio'&&!(await loc.isChecked().catch(()=>true)))throw new Error('Radio choice did not register');
          }
          else throw new Error('Unsupported action');
        }
        lastEffect=Date.now();if(step.kind!=='navigate')await quiet(page,lastEffect-1000);
        if(!originOK())throw new Error('Origin changed after action');
        stepsDone++;
      }
      progress({event:'action_dispatched',steps:input.steps.length});
    }
    phase('snapshot_after');
    if(input.operation==='act'){await quiet(page,lastEffect,{wait:200,limit:Math.max(1000,Math.min(8000,deadline-Date.now()-15000))});await settle(page,Math.min(3000,Math.max(500,deadline-Date.now()-15000)));}
    const after=await read();
    if(input.operation!=='act')return {ok:true,snapshot:after};
    // Validator signal: what observably changed because of this action.
    const ids=x=>new Set(x.controls.map(c=>c.stable_id));const b=ids(before),a=ids(after);
    const values=x=>JSON.stringify(x.controls.filter(c=>['input','textarea','select'].includes(c.tag)).map(c=>[c.stable_id,c.value,c.checked]));
    const changes={url_changed:before.url!==after.url,title_changed:before.title!==after.title,
      new_controls:[...a].filter(x=>!b.has(x)).length,removed_controls:[...b].filter(x=>!a.has(x)).length,
      values_changed:values(before)!==values(after),
      new_errors:after.page_errors.filter(x=>!before.page_errors.includes(x)),
      new_notices:(after.page_notices||[]).filter(x=>!(before.page_notices||[]).includes(x)),
      field_errors:after.controls.filter(c=>c.error).map(c=>({ref:c.ref,label:c.label.slice(0,80),error:c.error})).slice(0,8),
      text_changed:before.text!==after.text};
    changes.changed=changes.url_changed||changes.title_changed||changes.new_controls>0||changes.removed_controls>0||changes.values_changed||changes.text_changed||changes.new_errors.length>0;
    return {ok:true,snapshot:after,progress:changes,fills};
  } catch(error){if(process.env.PROCURATOR_DEBUG)console.error("DEBUG",error?.stack);
    const notDispatched=input.operation==='act'&&effects===0&&stage!=='snapshot_after';
    // Typing and dropdown choices (no clicks, no navigation) that stopped midway submitted nothing:
    // report which ran so the bot re-reads the page and redoes only what is missing, instead of
    // freezing the task as "uncertain" (Nov 4 MyLeaveco absence form).
    const fieldsOnly=input.operation==='act'&&Array.isArray(input.steps)&&input.steps.length>0&&input.steps.every(x=>x.kind==='fill'||x.kind==='select');
    const out={ok:false,kind:notDispatched?'not_dispatched':fieldsOnly&&effects>0&&stage==='action'?'fields_incomplete':'unconfirmed',error_stage:stage,error_type:error?.name==='TimeoutError'?'timeout':'driver_error'};
    if(out.kind==='fields_incomplete'){out.steps_done=typeof stepsDone==='number'?stepsDone:0;out.fills=typeof fills!=='undefined'?fills:[];}
    if(input.operation==='act'){
      const m=String(error?.message||'');
      out.reason=/Control changed/.test(m)?'page_changed':/ambiguous/i.test(m)?'ambiguous_control':/no clickable label|not visible|intercepts pointer|outside of the viewport|not stable|Timeout/i.test(m)?'control_not_clickable':/did not register/.test(m)?'choice_not_registered':/Manual input/.test(m)?'manual_input_required':'driver_error';
    }
    if((CHAT_OPS.includes(input.operation)||['project_file','claude_file'].includes(input.operation))&&page){out.error_detail=String(error?.message||'').split('\n')[0].replace(/https?:\/\/\S+/g,'[url]').slice(0,200);out.dom_probe=CHAT_OPS.includes(input.operation)?await domProbe(page,TURNS[input.operation.startsWith('claude')?'claude':'chatgpt'].all):await domProbe(page,'','[role="dialog"],dialog[open],[role="tabpanel"]');}
    return out;
  } finally {
    if(tracing){try{await within(tracing.tracing.stop({path:input.trace_path}),6000);}catch{}}
    if(browser)await browser.close();
  }
}
if(require.main===module){
 let raw='';process.stdin.on('data',c=>{raw+=c;if(raw.length>200000)process.exit(2)});
 process.stdin.on('end',async()=>{try{console.log(JSON.stringify(await run(JSON.parse(raw),event=>process.stdout.write(JSON.stringify(event)+'\n'))))}catch{console.log(JSON.stringify({ok:false,kind:'unconfirmed',error_stage:'driver_close',error_type:'driver_error'}));process.exitCode=1}});
}
module.exports={inScope,settle,sameValue,captureOriginal,kindOf,digest,pageKey,snapshot,readiness,readableSnapshot,locatorFor,fillReviewedField,run};
