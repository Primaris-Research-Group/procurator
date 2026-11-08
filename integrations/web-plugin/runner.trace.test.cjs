// web 0.17.0: Playwright trace of an act and the read-only MHTML capture, both written only under the broker inbox.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const inbox=fs.mkdtempSync(path.join(os.tmpdir(),'inbox-'))+'/';
process.env.PROCURATOR_INBOX_PREFIX=inbox;
const {run}=require('./runner.cjs');
test('act is traced into the inbox and capture archives the page without acting',async()=>{
 const port=9300+Math.floor(Math.random()*500);
 const browser=await chromium.launch({executablePath:process.env.PROCURATOR_TEST_CHROME,headless:true,args:['--remote-debugging-port='+port]});
 try{
  const page=await browser.newPage();await page.setContent('<form><label>State<select><option>GA</option></select></label><button>Continue</button></form>');
  const base={connectUrl:'http://127.0.0.1:'+port,practice:true,url:'about:blank',origin:'null'};
  const trace=inbox+'trace-abc.zip';
  const out=await run({...base,operation:'act',expected:'not-the-page',steps:[],trace_path:trace});
  assert.equal(out.kind,'stale');assert(fs.statSync(trace).size>0);
  const outside=path.join(os.tmpdir(),'outside-'+port+'.zip');
  await run({...base,operation:'act',expected:'x',steps:[],trace_path:outside});assert(!fs.existsSync(outside));
  const target=inbox+'capture-1.mhtml';
  const cap=await run({...base,operation:'capture',capture_path:target});
  assert.equal(cap.ok,true);assert(fs.readFileSync(target,'utf8').includes('MIME-Version'));assert(cap.snapshot.controls.some(c=>c.label==='State'));
  assert.equal((await run({...base,operation:'capture',capture_path:'/etc/x.mhtml'})).kind,'capture_refused');
 }finally{await browser.close();}
});
