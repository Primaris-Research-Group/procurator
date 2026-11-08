const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot,digest,run}=require('./runner.cjs');

// Nov 4 CVS patient-info page: "State" is required but the bot could not see a usable control.
// These are the common ways modern sites build a State dropdown.
async function open(t,html){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();await page.setContent(html);return {browser,page};
}
const act=(browser,before,steps)=>run({connectUrl:'unused',practice:true,operation:'act',expected:digest(before),steps},()=>{},async()=>({contexts:()=>browser.contexts(),close:async()=>{}}));
const STATES=['AL','AK','AS','AZ','AR','AA','AE','AP','CA','CO','CT','DE','DC','FM','FL','GA','GU','HI','ID','IL','IN','IA','KS','KY','LA','ME','MH','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','MP','OH','OK','OR','PW','PA','PR','RI','SC','SD','TN','TX','UT','VT','VI','VA','WA','WV','WI','WY'];
const NAMES={GA:'Georgia',WY:'Wyoming',TX:'Texas'};
const opts='<option value="">Select a state</option>'+STATES.map(s=>`<option value="${s}">${NAMES[s]||s}</option>`).join('');

// 1) Real <select> hidden (opacity 0 + aria-hidden) behind a styled button; its change event drives the display.
const STYLED=`<form><label>City <input name="city"></label>
 <div class="ps-select"><label for="state">State</label>
  <select id="state" name="state" aria-hidden="true" tabindex="-1" required style="position:absolute;opacity:0;width:1px;height:1px;pointer-events:none">${opts}</select>
  <button type="button" class="trigger" aria-haspopup="listbox" id="shown">Select a state</button></div>
 <div style="display:none"><label>Old state <select name="legacy">${opts}</select></label></div>
 <button type="submit">Continue</button></form>
 <script>document.getElementById('state').addEventListener('change',e=>{document.getElementById('shown').textContent=e.target.selectedOptions[0].text;window.changed=(window.changed||0)+1;});</script>`;

test('a hidden native State select behind a styled button is exposed and selectable, beyond 40 options',async t=>{
 const {browser,page}=await open(t,STYLED);
 const before=await snapshot(page);
 const state=before.controls.find(c=>c.name==='state');
 assert.ok(state,'State select must be in the snapshot');
 assert.equal(state.label,'State');assert.equal(state.hidden_native,true);assert.equal(state.required,true);
 assert.ok(state.options.some(o=>o.value==='WY'&&o.label==='Wyoming'),'all states are listed');
 assert.equal(before.controls.find(c=>c.name==='legacy'),undefined,'selects inside hidden sections stay out');
 const r=await act(browser,before,[{kind:'select',control:state,value:'Wyoming'}]);   // option label works too
 assert.equal(r.ok,true,JSON.stringify(r).slice(0,300));
 assert.equal(await page.inputValue('#state'),'WY');
 assert.equal(await page.textContent('#shown'),'Wyoming');assert.equal(await page.evaluate(()=>window.changed),1);
});

test('display:none select inside a visible field wrapper is exposed and selectable',async t=>{
 const {browser,page}=await open(t,`<div class="field"><label for="st">State</label><select id="st" name="st" style="display:none">${opts}</select><div class="fake">Select a state</div></div>`);
 const before=await snapshot(page);const st=before.controls.find(c=>c.name==='st');
 assert.ok(st);assert.equal(st.hidden_native,true);
 const r=await act(browser,before,[{kind:'select',control:st,value:'GA'}]);
 assert.equal(r.ok,true,JSON.stringify(r).slice(0,300));assert.equal(await page.inputValue('#st'),'GA');
});

test('large site headers never push form fields out of the snapshot',async t=>{
 const nav='<header>'+Array.from({length:140},(_,i)=>`<button>Menu ${i}</button>`).join('')+'</header>';
 const {page}=await open(t,nav+`<main><label>State <select name="state">${opts}</select></label><button>Continue</button></main>`);
 const s=await snapshot(page);
 assert.ok(s.controls.find(c=>c.name==='state'),'State select kept');
 assert.ok(s.controls.find(c=>c.label==='Continue'),'form button kept');
 assert.equal(s.controls.length,120);assert.equal(s.controls_truncated,true);
});

// 2) Custom listbox combobox (no native select at all).
const LISTBOX=`<label id="lbl">State</label><div role="combobox" aria-labelledby="lbl" aria-haspopup="listbox" aria-expanded="false" tabindex="0" id="cb">Select a state</div>
<ul role="listbox" id="lb" hidden>${['Georgia','Georgia (GA) - Atlanta area','Texas','Wyoming'].map(s=>`<li role="option">${s}</li>`).join('')}</ul>
<script>const cb=document.getElementById('cb'),lb=document.getElementById('lb');
cb.addEventListener('click',()=>{lb.hidden=false;cb.setAttribute('aria-expanded','true');});
lb.addEventListener('click',e=>{if(e.target.getAttribute('role')==='option'){cb.textContent=e.target.textContent;lb.hidden=true;cb.setAttribute('aria-expanded','false');}});</script>`;

test('a custom listbox combobox is chosen by opening it and clicking the exact option',async t=>{
 const {browser,page}=await open(t,LISTBOX);
 const before=await snapshot(page);const cb=before.controls.find(c=>c.role==='combobox');
 assert.equal(cb.label,'State');assert.equal(cb.haspopup,'listbox');
 const r=await act(browser,before,[{kind:'select',control:cb,value:'Georgia'}]);
 assert.equal(r.ok,true,JSON.stringify(r).slice(0,300));
 assert.equal(await page.textContent('#cb'),'Georgia');
 assert.equal(r.fills[0].method,'listbox');
});

test('a custom combobox reports choice_not_registered when the option does not exist',async t=>{
 const {browser,page}=await open(t,LISTBOX);
 const before=await snapshot(page);const cb=before.controls.find(c=>c.role==='combobox');
 const r=await act(browser,before,[{kind:'select',control:cb,value:'Atlantis'}]);
 assert.equal(r.ok,false);assert.equal(r.reason,'choice_not_registered');
});

// 3) Form-associated web component with a CLOSED shadow root wrapping a native select.
const CLOSED=`<label for="ws">State</label><x-state-select id="ws" name="state"></x-state-select><button>Continue</button>
<script>customElements.define('x-state-select',class extends HTMLElement{
 static formAssociated=true;
 constructor(){super();this.i=this.attachInternals();const r=this.attachShadow({mode:'closed',delegatesFocus:true});
  r.innerHTML='<select>${opts.replace(/'/g,"\\'")}</select>';this.s=r.querySelector('select');
  this.s.addEventListener('change',()=>{this.i.setFormValue(this.s.value);});}
 get value(){return this.s.value;}
});</script>`;

test('a form-associated component with a closed shadow root is exposed and chosen by keyboard',async t=>{
 const {browser,page}=await open(t,CLOSED);
 const before=await snapshot(page);const ws=before.controls.find(c=>c.tag==='x-state-select');
 assert.ok(ws,'custom field present');assert.equal(ws.custom_field,true);assert.equal(ws.label,'State');
 const r=await act(browser,before,[{kind:'select',control:ws,value:'Georgia'}]);
 assert.equal(r.ok,true,JSON.stringify(r).slice(0,300));
 assert.equal(await page.evaluate(()=>document.getElementById('ws').value),'GA');
});

test('labels whose field the bot cannot use are reported as form_gaps',async t=>{
 const {page}=await open(t,`<label for="county">County</label><div id="county" class="fancy"></div><label>Name <input name="n"></label>
  <div role="alert"><a href="#county">Select a county</a></div>`);
 const s=await snapshot(page);
 assert.deepEqual(s.form_gaps.map(g=>[g.label,g.target]),[['County','div']]);
 const none=await snapshot(await (async()=>{await page.setContent(STYLED);return page;})());
 assert.deepEqual(none.form_gaps,[]);
});

test('a closed component that lands on a different option is not reported as success',async t=>{
 const {browser,page}=await open(t,CLOSED);
 const before=await snapshot(page);const ws=before.controls.find(c=>c.tag==='x-state-select');
 const r=await act(browser,before,[{kind:'select',control:ws,value:'Atlantis'}]);
 assert.equal(r.ok,false);assert.equal(r.reason,'choice_not_registered');
});
