/* Builds a seed fixture's page.mhtml + snapshot.json from its source.html, served at the fixture's real URL.
   node make-seed.cjs DIR URL   (needs PROCURATOR_PLAYWRIGHT_PATH and a Chrome; run from integrations/web/fixtures) */
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PROCURATOR_PLAYWRIGHT_PATH||'/app/node_modules/playwright-core');
const {snapshot}=require('../../web-plugin/runner.cjs');
(async()=>{
  const [dir,url]=process.argv.slice(2);const html=fs.readFileSync(path.join(dir,'source.html'),'utf8');
  const browser=await chromium.launch({headless:true,...(process.env.PROCURATOR_TEST_CHROME?{executablePath:process.env.PROCURATOR_TEST_CHROME}:{})});
  const context=await browser.newContext();await context.route('**/*',r=>r.request().url()===url?r.fulfill({contentType:'text/html',body:html}):r.abort());
  const page=await context.newPage();await page.goto(url);
  const cdp=await context.newCDPSession(page);const {data}=await cdp.send('Page.captureSnapshot',{format:'mhtml'});
  fs.writeFileSync(path.join(dir,'page.mhtml'),data);
  const snap=await snapshot(page);for(const c of snap.controls)delete c.selector;
  fs.writeFileSync(path.join(dir,'snapshot.json'),JSON.stringify(snap,null,1));
  await browser.close();console.log(dir,data.length,'bytes',snap.controls.length,'controls');
})();
