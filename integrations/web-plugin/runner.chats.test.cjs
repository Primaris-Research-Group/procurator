const test=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {run}=require('./runner.cjs');

const GPT_CHAT='/g/g-p-fixture123/c/6900aaaa-1111-2222-3333-444455556666';
const GPT_CHAT2='/g/g-p-fixture123/c/6900bbbb-1111-2222-3333-444455556666';
const longAnswer='Deferred assistant paragraph about the fictional grievance timeline. '.repeat(20);
const composer='<form onsubmit="window.sent=true;return false"><textarea id="prompt-textarea" oninput="window.typed=true"></textarea><button>Send</button></form>';
const sidebar='<nav><a href="https://chatgpt.com/c/unrelated-1234-5678">Unrelated private chat</a><div role="button" data-app-action-sidebar-project-id="g-p-fixture123" data-app-action-sidebar-project-label="Legal Matter">Legal Matter</div></nav>';
const gptProject=`${sidebar}<main><h1>Legal Matter</h1><div role="tablist"><button role="tab" aria-selected="true">Chats</button><button role="tab" aria-selected="false">Sources</button></div>
<div role="tabpanel" aria-label="Chats"><ol>
<li><a href="${GPT_CHAT}"><div>Grievance draft v2</div><div>Here is the revised complaint…</div><div>Oct 30</div></a><button onclick="window.deleted=true">Delete</button></li>
<li><a href="${GPT_CHAT2}"><div>Evidence checklist</div><div>Exhibits A–F</div></a></li>
</ol></div>${composer}</main>`;
const gptChat=`<title>Grievance draft v2 | ChatGPT</title>${sidebar}<main>
<article><div data-message-author-role="user"><div>Draft my grievance against the program. See attached Exhibit_A.pdf</div></div></article>
<article><div data-message-author-role="assistant"><div><p>First answer paragraph.</p><button>Copy</button><span class="sr-only">ChatGPT said:</span></div></div></article>
<article style="content-visibility:hidden"><div data-message-author-role="assistant"><p></p></div></article>
</main>${composer}<script>document.querySelector('article:last-of-type p').textContent=${JSON.stringify(longAnswer)};</script>`;

async function gpt(t){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();
 await page.route('https://chatgpt.com/**',route=>{const p=new URL(route.request().url()).pathname;
  route.fulfill({contentType:'text/html',body:p.includes('/c/')?gptChat:p.endsWith('/project')?gptProject:`${sidebar}<main><h1>Home</h1>${composer}</main>`});});
 return {page,input:{connectUrl:'unused',origin:'https://chatgpt.com',read_url:'https://chatgpt.com/',project_scope:true,wait_seconds:0},connect:async()=>({contexts:()=>browser.contexts(),close:async()=>{}})};
}
const untouched=page=>page.evaluate(()=>window.sent||window.typed||window.deleted||window.uploaded);

test('ChatGPT chats lists only the project chats, with case-insensitive project names',async t=>{
 const {page,input,connect}=await gpt(t);
 const r=await run({...input,operation:'chatgpt_chats',project_name:'legal  matter'},()=>{},connect);
 assert.equal(r.ok,true);assert.equal(r.project_url,'https://chatgpt.com/g/g-p-fixture123/project');
 assert.deepEqual(r.chats.map(c=>c.title),['Grievance draft v2','Evidence checklist']);
 assert.equal(r.chats[0].url,'https://chatgpt.com'+GPT_CHAT);assert.match(r.chats[0].preview,/revised complaint/);
 assert(!JSON.stringify(r).includes('Unrelated'));assert.equal(await untouched(page),undefined);
});
test('ChatGPT read_chat by title and URL returns ordered turns, deferred text and pagination without typing',async t=>{
 const {page,input,connect}=await gpt(t);
 const r=await run({...input,operation:'chatgpt_chat',project_name:'Legal Matter',chat_title:'grievance draft v2',max_chars:20000},()=>{},connect);
 assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.chat.turns,3);assert.equal(r.chat.title,'grievance draft v2');
 const c=r.chat.content;assert(c.startsWith('[User]\nDraft my grievance'));assert.match(c,/Exhibit_A\.pdf/);
 assert.match(c,/\[Assistant\]\nFirst answer paragraph\./);assert.match(c,/Deferred assistant paragraph/);
 assert(!/Copy|ChatGPT said|Unrelated/.test(c));assert.equal(r.chat.chat_url,'https://chatgpt.com'+GPT_CHAT);
 const first=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat',max_chars:100},()=>{},connect);
 assert.equal(first.ok,true);assert.equal(first.chat.title,'Grievance draft v2');assert.equal(first.chat.next_offset,100);assert.equal(first.chat.sha256,r.chat.sha256);
 const next=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat',offset:100,max_chars:100,expected_sha256:first.chat.sha256},()=>{},connect);
 assert.equal(next.ok,true);assert.equal(next.chat.offset,100);
 const changed=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat',offset:100,expected_sha256:'0'.repeat(64)},()=>{},connect);
 assert.equal(changed.kind,'file_changed');
 const missing=await run({...input,operation:'chatgpt_chat',project_name:'Legal Matter',chat_title:'Delete'},()=>{},connect);
 assert.equal(missing.kind,'chat_not_found');assert.deepEqual(missing.available,['Grievance draft v2','Evidence checklist']);
 assert.equal(await untouched(page),undefined);
});

const CL_PROJECT='https://claude.ai/project/00000000-0000-4000-8000-000000000003';
const CL_CHAT='https://claude.ai/chat/11111111-2222-4333-8444-555555555555';
const clCatalog=`<main><a href="${CL_PROJECT}">Project Gryffindor</a></main>`;
const clProject=`<nav><a href="https://claude.ai/chat/99999999-2222-4333-8444-555555555555">Unrelated recent chat</a></nav><main><div data-testid="project-body"><h1>Project Gryffindor</h1>
<a href="${CL_CHAT}"><div>Rubric review</div><div>Last message 2 days ago</div></a>${composer}</div></main>`;
const clChat=`<title>Rubric review - Claude</title><main><div data-testid="user-message"><p>Review my rubric.</p></div>
<div class="font-claude-response" data-is-streaming="false"><div><p>Claude reply one.</p><p>Second paragraph.</p></div></div>
<div data-testid="user-message"><p>Thanks</p></div><div class="font-claude-response"><p>You're welcome.</p></div></main>${composer}`;
async function claude(t){
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true});t.after(()=>browser.close());
 const page=await browser.newPage();
 await page.route('https://claude.ai/**',route=>{const p=new URL(route.request().url()).pathname;
  route.fulfill({contentType:'text/html',body:p==='/projects'?clCatalog:p.startsWith('/chat/')?clChat:clProject});});
 return {page,input:{connectUrl:'unused',origin:'https://claude.ai',read_url:'https://claude.ai/projects',project_scope:true,wait_seconds:0},connect:async()=>({contexts:()=>browser.contexts(),close:async()=>{}})};
}
test('Claude chats and read_chat use project links and message containers only',async t=>{
 const {page,input,connect}=await claude(t);
 const list=await run({...input,operation:'claude_chats',project_name:'project gryffindor'},()=>{},connect);
 assert.equal(list.ok,true);assert.deepEqual(list.chats.map(c=>[c.title,c.url]),[['Rubric review',CL_CHAT]]);
 const r=await run({...input,read_url:CL_CHAT,operation:'claude_chat'},()=>{},connect);
 assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.chat.title,'Rubric review');assert.equal(r.chat.turns,4);
 assert.match(r.chat.content,/^\[User\]\nReview my rubric\.\n\n\[Assistant\]\nClaude reply one\.\n\nSecond paragraph\./);
 assert.match(r.chat.content,/\[Assistant\]\nYou're welcome\.$/);
 const byTitle=await run({...input,operation:'claude_chat',project_name:'Project Gryffindor',chat_title:'Rubric review'},()=>{},connect);
 assert.equal(byTitle.ok,true);assert.equal(byTitle.chat.sha256,r.chat.sha256);
 assert.equal(await untouched(page),undefined);
});

test('ChatGPT newer article[data-turn] markup reads with roles and drops "said:" headings',async t=>{
 const {page,input,connect}=await gpt(t);
 const html=`<title>Turn markup | ChatGPT</title><main>
<article data-testid="conversation-turn-1" data-turn="user"><h5 class="x">You said:</h5><div><p>Question about the fee packet.</p></div></article>
<article data-testid="conversation-turn-2" data-turn="assistant"><h6>ChatGPT said:</h6><div><p>Send five copies plus the original.</p></div><button>Copy</button></article></main>${composer}`;
 await page.unroute('https://chatgpt.com/**');await page.route('https://chatgpt.com/**',route=>route.fulfill({contentType:'text/html',body:html}));
 const r=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat'},()=>{},connect);
 assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.chat.turns,2);
 assert.equal(r.chat.content,'[User]\nQuestion about the fee packet.\n\n[Assistant]\nSend five copies plus the original.');
 assert.equal(await untouched(page),undefined);
});
test('unreadable chat returns a structure-only probe that never contains conversation text',async t=>{
 const {page,input,connect}=await gpt(t);
 const html=`<main><section data-testid="thread-x123"><div data-new-markup="1" data-testid="msg-7"><p>SECRET CONVERSATION TEXT</p></div></section></main>${composer}`;
 await page.unroute('https://chatgpt.com/**');await page.route('https://chatgpt.com/**',route=>route.fulfill({contentType:'text/html',body:html}));
 const r=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat',wait_seconds:0},()=>{},connect);
 assert.equal(r.ok,false);assert.equal(r.kind,'chat_not_readable');
 assert.equal(r.dom_probe.has_main,true);assert.deepEqual(r.dom_probe.markers,[]);assert(r.dom_probe.main_text_chars>0);assert.equal(r.dom_probe.data_attrs['data-new-markup'],1);assert.equal(r.dom_probe.data_testids['msg-#'],1);
 assert(!JSON.stringify(r).includes('SECRET'));assert.equal(await untouched(page),undefined);
});

test('ChatGPT Nov-2026 data-conversation-role markup (observed live) reads all turns with roles',async t=>{
 const {page,input,connect}=await gpt(t);
 const turn=(role,text)=>`<div data-conversation-role="${role}" data-chatgpt-search-message-ids="m1" data-content-search-unit-key="k"><div data-markdown-copy="1"><p>${text}</p></div><span data-state="closed"><button>Copy</button></span></div>`;
 const html=`<title>USPS Submission Update | ChatGPT</title><main><section>${turn('user','Here is the tracking info.')}${turn('assistant','NJ typically reviews within weeks.')}${turn('user','And the refund?')}${turn('assistant','Timing depends on the committee.')}</section></main>${composer}`;
 await page.unroute('https://chatgpt.com/**');await page.route('https://chatgpt.com/**',route=>route.fulfill({contentType:'text/html',body:html}));
 const r=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat'},()=>{},connect);
 assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.chat.turns,4);assert.equal(r.chat.title,'USPS Submission Update');
 assert.equal(r.chat.content,'[User]\nHere is the tracking info.\n\n[Assistant]\nNJ typically reviews within weeks.\n\n[User]\nAnd the refund?\n\n[Assistant]\nTiming depends on the committee.');
 assert.equal(await untouched(page),undefined);
});

test('empty role markers beside the message body read the enclosing turn block',async t=>{
 const {page,input,connect}=await gpt(t);
 const turn=(role,text)=>`<div class="turn"><span data-conversation-role="${role}" data-content-search-unit-key="k"></span><div><div data-markdown-copy="1"><p>${text}</p></div><button>Copy</button></div></div>`;
 const html=`<title>Sibling markers | ChatGPT</title><main><header>Header text</header><section>${turn('user','Tracking number attached.')}${turn('assistant','Review usually takes weeks.')}</section></main>${composer}`;
 await page.unroute('https://chatgpt.com/**');await page.route('https://chatgpt.com/**',route=>route.fulfill({contentType:'text/html',body:html}));
 const r=await run({...input,read_url:'https://chatgpt.com'+GPT_CHAT,operation:'chatgpt_chat'},()=>{},connect);
 assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.chat.content,'[User]\nTracking number attached.\n\n[Assistant]\nReview usually takes weeks.');
});
