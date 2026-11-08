/* Offline fixture loader (web 0.17.0, Phase 2). Opens a saved page archive (MHTML, or plain HTML for synthetic seeds)
   in a local headless Chrome with no network, and prints the snapshot the live driver would have produced.
   Usage: node runner.fixtures.cjs FILE [FILE...]  ->  one JSON line per file: {file, ok, snapshot|error} */
const path=require('node:path');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot}=require('./runner.cjs');
async function load(browser,file){
  const context=await browser.newContext({offline:true,javaScriptEnabled:true});
  try{
    // Archives must not reach the network: block every request that is not the file itself.
    await context.route('**/*',r=>r.request().url().startsWith('file:')?r.continue():r.abort());
    const page=await context.newPage();
    await page.goto('file://'+path.resolve(file),{waitUntil:'load',timeout:20000});
    await page.waitForTimeout(300);
    const snap=await snapshot(page);
    return {file,ok:true,snapshot:snap};
  }catch(e){return {file,ok:false,error:String(e?.message||e).split('\n')[0].slice(0,300)};}
  finally{await context.close();}
}
if(require.main===module){(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.PROCURATOR_TEST_CHROME?{executablePath:process.env.PROCURATOR_TEST_CHROME}:{})});
  try{for(const f of process.argv.slice(2))console.log(JSON.stringify(await load(browser,f)));}
  finally{await browser.close();}
})().catch(e=>{console.log(JSON.stringify({ok:false,error:String(e?.message||e).slice(0,300)}));process.exitCode=1;});}
module.exports={load};
