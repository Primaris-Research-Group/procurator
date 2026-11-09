import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {DocReader,fileMeta,registerDocRead,DOC_TOOLS,displayName,listFiles,resolveFile,FILE_HINT,FILE_GUIDANCE} from './docread.mjs';

function fixture(meta,{cached=false,files={}}={}){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'inbox-'));const runs=[],calls=[],written=[];
 const run=async(args,o)=>{runs.push(args);if(args[0]==='drive'&&args[1]==='get')return JSON.stringify(meta);
  if(args[0]==='docs')return files.docs??'Full doc text';
  if(args[0]==='drive'&&args[1]==='download')return Buffer.from(files.download??'%PDF-1.4');
  if(args[0]==='gmail'){fs.writeFileSync(args[args.indexOf('--out')+1]+'.pdf','%PDF');return '';}
  throw new Error('unexpected');};
 const call=async(a,args)=>{calls.push([a,args]);if(a==='doc_page')return cached?{cached:true,status:'file_read',content:'page '+(args.offset||0)}:{cached:false};return {cached:true,status:'file_read',content:'first page'};};
 const r=new DocReader(call,run,{inbox:dir,write:(p,d)=>{written.push([path.basename(p),String(d)]);fs.writeFileSync(p,d);}});
 return {r,runs,calls,written,dir};
}

test('a Google Doc is read whole (no 64 KB cap) and handed to the broker as text',async()=>{
 const f=fixture({id:'1AbCdEfGhIjKl',name:'Master timeline',mimeType:'application/vnd.google-apps.document',modifiedTime:'2026-11-06T12:00:00.000Z'},{files:{docs:'x'.repeat(200000)}});
 const out=await f.r.drive({file_id:'1AbCdEfGhIjKl'});
 assert.equal(out.status,'file_read');assert.equal(out.file_id,'1AbCdEfGhIjKl');
 assert.deepEqual(f.runs[1],['docs','cat','1AbCdEfGhIjKl','--all-tabs','--max-bytes','0']);
 const ingest=f.calls.find(c=>c[0]==='doc_ingest')[1];
 assert.equal(ingest.kind,'text');assert.equal(ingest.key,'drive:1AbCdEfGhIjKl@2026-11-06T12:00:00.000Z');assert.match(ingest.file,/^[a-f0-9]{32}\.bin$/);
 assert.equal(f.written[0][1].length,200000);
});

test('cached pages are served without fetching again; PDFs and Sheets download bytes; trash and folders are refused',async()=>{
 const c=fixture({name:'x.pdf',mimeType:'application/pdf',modifiedTime:'v1',size:1000},{cached:true});
 const p=await c.r.drive({file_id:'1PdfScanOnlyX',offset:20000,expected_sha256:'a'.repeat(64)});
 assert.equal(p.content,'page 20000');assert.equal(c.runs.length,1,'only metadata was fetched');
 const pdf=fixture({name:'NJ.pdf',mimeType:'application/pdf',modifiedTime:'v1',size:1000});
 await pdf.r.drive({file_id:'1PdfScanOnlyX'});assert.deepEqual(pdf.runs[1],['drive','download','1PdfScanOnlyX','--out','-']);assert.equal(pdf.calls.at(-1)[1].kind,'auto');
 const sh=fixture({name:'Ledger',mimeType:'application/vnd.google-apps.spreadsheet',modifiedTime:'v2'},{files:{download:'a,b\n1,2'}});
 const r=await sh.r.drive({file_id:'1SheetLedgerX'});assert.deepEqual(sh.runs[1],['drive','download','1SheetLedgerX','--format','csv','--out','-']);assert.match(r.note,/first sheet/);
 await assert.rejects(fixture({mimeType:'application/pdf',trashed:true}).r.drive({file_id:'1PdfScanOnlyX'}),/trash/);
 await assert.rejects(fixture({mimeType:'application/vnd.google-apps.folder',name:'Legal Matter'}).r.drive({file_id:'1FolderXXXXX'}),/no readable text/);
 await assert.rejects(fixture({mimeType:'application/pdf',size:30*1024*1024}).r.drive({file_id:'1PdfScanOnlyX'}),/20 MB/);
 await assert.rejects(fixture({}).r.drive({file_id:'../../etc'}),/file_id must be/);
 const exp=fixture({mimeType:'application/pdf',modifiedTime:'v3'});
 assert.equal((await exp.r.drive({file_id:'1PdfScanOnlyX',offset:5,expected_sha256:'b'.repeat(64)})).status,'file_changed');
});

test('a Gmail attachment is downloaded into the inbox (renamed back if gog adds an extension)',async()=>{
 const f=fixture({});
 const out=await f.r.attachment({message_id:'18c2a9f0b1d2e3f4',attachment_id:'ANGjdJ8_long-id',filename:'letter.pdf'});
 assert.equal(out.status,'file_read');
 const ing=f.calls.find(c=>c[0]==='doc_ingest')[1];
 assert.match(ing.key,/^gmail:18c2a9f0b1d2e3f4:[a-f0-9]{24}$/);assert.ok(fs.existsSync(path.join(f.dir,ing.file)));
 await assert.rejects(f.r.attachment({message_id:'x;rm',attachment_id:'a'}),/message_id/);
});

test('fileMeta reads the common gog JSON shapes; tools are registered for the main agent and declared',async()=>{
 assert.deepEqual(fileMeta({file:{name:'A',mimeType:'text/plain',modifiedTime:'2026-11-06T12:00:00Z',size:'12'}}),{name:'A',mime:'text/plain',modified:'2026-11-06T12:00:00Z',size:12,trashed:false});
 assert.equal(fileMeta('{"name":"B","mimeType":"application/pdf"}').mime,'application/pdf');
 const tools=[];registerDocRead({registerTool:(f,o)=>tools.push([f,o])},async()=>({}),{drive:async()=>({ok:1}),attachment:async()=>({ok:2}),local:async()=>({ok:3}),roots:[],fsx:fs});
 assert.deepEqual(tools.map(t=>t[1].name),DOC_TOOLS);
 assert.equal(tools[0][0]({agentId:'phone'}),undefined,'never available to the phone agent');
 const declared=JSON.parse(fs.readFileSync(new URL('./openclaw.plugin.json',import.meta.url))).contracts.tools;
 for(const n of DOC_TOOLS)assert.ok(declared.includes(n),n+' must be declared in openclaw.plugin.json');
 assert.deepEqual(JSON.parse((await tools[0][0]({agentId:'main'}).execute('1',{file_id:'x'})).content[0].text),{ok:1});
});

// 0.32.3 (Nov 9): three GME .eml files sent in Telegram came through as a raw-MIME preview the bot could not open.
function media(){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'media-'));const inbound=path.join(root,'media','inbound');fs.mkdirSync(inbound,{recursive:true});
 const ws=path.join(root,'workspace');fs.mkdirSync(ws);
 const put=(name,data,ageMin=0)=>{const p=path.join(inbound,name);fs.writeFileSync(p,data);const t=new Date(Date.now()-ageMin*60000);fs.utimesSync(p,t,t);return p;};
 return {root,inbound,ws,put,roots:[path.join(root,'media')+'/',ws+'/']};
}
test('a file the owner sent in Telegram is read whole by path or by name, keyed by its content',async()=>{
 const m=media();
 const eml=m.put('Workflow_Request---3f2a1b2c-1111-2222-3333-444455556666.eml','From: a@example.org\r\nSubject: Workflow Request\r\n\r\nbody',2);
 m.put('Rotation_Sched_ICU_Make-Up_Time---aaaabbbb-1111-2222-3333-444455556666.eml','From: b@example.org\r\n\r\nicu',1);
 m.put('photo---cccc1111-1111-2222-3333-444455556666.jpg','\xff\xd8',0);
 assert.equal(displayName(eml),'Workflow Request.eml');
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'inbox-'));const calls=[];
 const call=async(a,args)=>{calls.push([a,args]);return a==='doc_page'?{cached:false}:{cached:true,status:'file_read',content:'decoded'};};
 const r=new DocReader(call,async()=>{throw new Error('no gog');},{inbox:dir,roots:m.roots});
 const byPath=await r.local({path:eml});
 assert.equal(byPath.status,'file_read');assert.equal(byPath.file,'Workflow Request.eml');
 const ing=calls.find(c=>c[0]==='doc_ingest')[1];
 assert.match(ing.key,/^file:[a-f0-9]{40}$/);assert.equal(ing.kind,'auto');assert.equal(ing.name,'Workflow Request.eml');assert.equal(ing.mime,'message/rfc822');
 assert.equal(fs.readFileSync(path.join(dir,ing.file),'utf8'),fs.readFileSync(eml,'utf8'),'the original bytes reach the broker');
 const byName=await r.local({name:'workflow request'});assert.equal(byName.path,fs.realpathSync(eml));
 assert.equal((await r.local({name:'Rotation Sched'})).file,'Rotation Sched ICU Make-Up Time.eml');
 assert.equal((await r.local({name:'photo'})).status,'image');
 await assert.rejects(r.local({name:'nothing like it'}),/owner_files_recent/);
 await assert.rejects(r.local({name:'eml'}),/Several files match/);
 const outside=path.join(m.root,'secret.txt');fs.writeFileSync(outside,'x');
 await assert.rejects(r.local({path:outside}),/Only files the owner sent/);
 const link=path.join(m.inbound,'link.txt');fs.symlinkSync(outside,link);
 await assert.rejects(r.local({path:link}),/Only files the owner sent/);
 await assert.rejects(r.local({path:'relative.eml'}),/full path/);
 assert.equal((await r.local({path:eml,offset:5,expected_sha256:'a'.repeat(64)})).status,'file_changed');
});
test('owner_files_recent lists newest first and skips old, empty and linked files; the hook steers file messages to the tool',async()=>{
 const m=media();
 m.put('old---aaaabbbb-1111-2222-3333-444455556666.pdf','%PDF',60*24*20);m.put('empty.txt','',0);
 m.put('a---aaaabbbb-1111-2222-3333-444455556666.eml','x',5);m.put('b---aaaabbbb-1111-2222-3333-444455556667.eml','y',1);
 fs.writeFileSync(path.join(m.ws,'draft.md'),'z');fs.symlinkSync('/etc/hostname',path.join(m.inbound,'evil.txt'));
 const files=listFiles({roots:m.roots,days:7});
 assert.deepEqual(files.map(f=>f.name),['draft.md','b.eml','a.eml']);
 assert.equal(files[1].where,'sent by the owner');assert.equal(files[0].where,'assistant workspace');
 const tools=[],hooks=[];const reader=new DocReader(async()=>({}),async()=>'',{roots:m.roots});
 registerDocRead({registerTool:(f,o)=>tools.push([f,o]),on:(n,fn,o)=>hooks.push([n,fn,o])},async()=>({}),reader);
 const recent=tools.find(t=>t[1].name==='owner_files_recent')[0]({agentId:'main'});
 const listed=JSON.parse((await recent.execute('1',{})).content[0].text);assert.equal(listed.files.length,3);
 const [name,hook]=hooks[0];assert.equal(name,'before_prompt_build');
 for(const t of ['Good, here are the emails from GME, I put them in this chat','Tell me if you are able to open these files','[media attached: /home/node/.openclaw/media/inbound/x.eml (message/rfc822)]'])
  assert.equal(hook({prompt:t},{agentId:'main'})?.appendContext,FILE_GUIDANCE,t);
 for(const t of ['What is on my calendar tomorrow?','Check my emails from GME','Draft emails and send them'])assert.equal(hook({prompt:t},{agentId:'main'}),undefined,t);
 assert.equal(hook({prompt:'here is the pdf'},{agentId:'phone'}),undefined);
 assert.ok(FILE_HINT.test('Workflow Request.eml'));
});
