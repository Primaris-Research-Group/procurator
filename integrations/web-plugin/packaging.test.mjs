// 0.21.0 (public-release readiness, build-plans/health/README.md): the plugin manifest must list every module the entry point
// imports — the 0.20.x manifest listed 7 files while index.mjs imported ~25, so a clean install from the archive would fail.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here=fileURLToPath(new URL('.',import.meta.url));   // fileURLToPath: works when the checkout path has spaces
function imports(file,seen=new Set()){
 if(seen.has(file)||!fs.existsSync(path.join(here,file)))return seen;
 seen.add(file);
 const src=fs.readFileSync(path.join(here,file),'utf8');
 for(const m of src.matchAll(/from\s+'\.\/([^']+)'|import\('\.\/([^']+)'\)|require\('\.\/([^']+)'\)/g))imports(m[1]||m[2]||m[3],seen);
 return seen;
}
test('package.json lists every module index.mjs imports, the driver and the manifest; versions agree',()=>{
 const pkg=JSON.parse(fs.readFileSync(path.join(here,'package.json'),'utf8'));
 const manifest=JSON.parse(fs.readFileSync(path.join(here,'openclaw.plugin.json'),'utf8'));
 const need=new Set([...imports('index.mjs'),'runner.cjs','openclaw.plugin.json','package.json']);
 const missing=[...need].filter(f=>!pkg.files.includes(f));
 assert.deepEqual(missing,[],'add to package.json files: '+missing.join(', '));
 for(const f of pkg.files)assert.ok(fs.existsSync(path.join(here,f)),f+' listed but missing');
 assert.ok(!pkg.files.some(f=>/\.test\./.test(f)),'tests are not shipped');
 assert.equal(pkg.version,manifest.version);
});
