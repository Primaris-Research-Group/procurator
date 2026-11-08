import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {DocReader,fileMeta,registerDocRead,DOC_TOOLS} from './docread.mjs';

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
 const tools=[];registerDocRead({registerTool:(f,o)=>tools.push([f,o])},async()=>({}),{drive:async()=>({ok:1}),attachment:async()=>({ok:2})});
 assert.deepEqual(tools.map(t=>t[1].name),DOC_TOOLS);
 assert.equal(tools[0][0]({agentId:'phone'}),undefined,'never available to the phone agent');
 const declared=JSON.parse(fs.readFileSync(new URL('./openclaw.plugin.json',import.meta.url))).contracts.tools;
 for(const n of DOC_TOOLS)assert.ok(declared.includes(n),n+' must be declared in openclaw.plugin.json');
 assert.deepEqual(JSON.parse((await tools[0][0]({agentId:'main'}).execute('1',{file_id:'x'})).content[0].text),{ok:1});
});
