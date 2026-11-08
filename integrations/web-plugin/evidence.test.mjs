import {test} from 'node:test';
import assert from 'node:assert/strict';
import {evidenceSearch,pubmedTerm,parseAbstracts,registerEvidence} from './evidence.mjs';
function fake(routes){const seen=[];const f=async(url,opts)=>{seen.push({url,ua:opts?.headers?.['user-agent']});for(const [re,body] of routes)if(re.test(url))return {ok:true,status:200,json:async()=>body,text:async()=>body};return {ok:false,status:500,json:async()=>({}),text:async()=>''};};f.seen=seen;return f;}
test('pubmed: filters build a precise query; summaries and abstracts become citable results',async()=>{
 assert.equal(pubmedTerm({query:'apixaban dialysis',type:'systematic_review',since_year:2020}),'(apixaban dialysis) AND (systematic review[pt] OR meta-analysis[pt]) AND 2020:3000[dp]');
 assert.throws(()=>pubmedTerm({query:'x'}),/2–300/);assert.throws(()=>pubmedTerm({query:'ok query',type:'blog'}),/type must/);
 const xml='<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID Version="1">111</PMID><Article><Abstract><AbstractText Label="BACKGROUND">Bleeding &amp; stroke.</AbstractText><AbstractText Label="RESULTS">HR 0.8.</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>';
 assert.deepEqual(parseAbstracts(xml),{111:'BACKGROUND: Bleeding & stroke. RESULTS: HR 0.8.'});
 const f=fake([[/esearch/,{esearchresult:{count:'42',idlist:['111','222']}}],[/esummary/,{result:{uids:['111','222'],111:{title:'Apixaban in dialysis: a <i>meta-analysis</i>',fulljournalname:'Kidney Int',pubdate:'2024 Mar',authors:[{name:'Ali A'},{name:'Bo B'},{name:'Cy C'},{name:'Di D'}],pubtype:['Meta-Analysis'],articleids:[{idtype:'doi',value:'10.1/abc'}]},222:{title:'Second',source:'JAMA',pubdate:'2023',authors:[]}}}],[/efetch/,xml]]);
 const r=await evidenceSearch({source:'pubmed',query:'apixaban dialysis',type:'systematic_review',abstracts:true,limit:2},{fetch:f});
 assert.equal(r.total,42);assert.equal(r.results[0].title,'Apixaban in dialysis: a meta-analysis');assert.equal(r.results[0].authors,'Ali A, Bo B, Cy C et al.');
 assert.equal(r.results[0].url,'https://pubmed.ncbi.nlm.nih.gov/111/');assert.equal(r.results[0].doi,'https://doi.org/10.1/abc');assert.match(r.results[0].abstract,/HR 0.8/);
 assert.match(r.note,/not instructions/);assert.ok(f.seen.every(x=>/^https:\/\/eutils\.ncbi\.nlm\.nih\.gov\//.test(x.url)&&!/@/.test(x.url)),'no email or personal data sent');
});
test('europe pmc, trials and drug labels',async()=>{
 const e=await evidenceSearch({source:'europepmc',query:'SGLT2 heart failure',since_year:2022},{fetch:fake([[/europepmc/,{hitCount:3,resultList:{result:[{id:'PPR1',source:'PPR',title:'Preprint',pubYear:'2025',authorString:'X',isOpenAccess:'Y'},{id:'9',pmid:'9',source:'MED',title:'T',pubYear:'2024',doi:'10.2/x',journalInfo:{journal:{title:'NEJM'}}}]}}]])});
 assert.equal(e.results[0].note,'Preprint (not peer reviewed)');assert.equal(e.results[1].url,'https://pubmed.ncbi.nlm.nih.gov/9/');assert.match(e.query,/PUB_YEAR:\[2022 TO 3000\]/);
 const tf=fake([[/clinicaltrials\.gov\/api\/v2\/studies/,{totalCount:1,studies:[{protocolSection:{identificationModule:{nctId:'NCT01234567',briefTitle:'Dapagliflozin in HFpEF'},statusModule:{overallStatus:'RECRUITING',startDateStruct:{date:'2025-01'}},designModule:{phases:['PHASE3'],studyType:'INTERVENTIONAL',enrollmentInfo:{count:500}},conditionsModule:{conditions:['Heart Failure']},contactsLocationsModule:{locations:[{facility:'Tampa General',city:'Tampa',state:'Florida',country:'United States'},{facility:'X',city:'Boston',state:'Massachusetts'}]}}}]}]]);
 const t=await evidenceSearch({source:'trials',query:'dapagliflozin',condition:'heart failure',location:'Tampa, FL',recruiting_only:true},{fetch:tf});
 assert.equal(t.results[0].url,'https://clinicaltrials.gov/study/NCT01234567');assert.deepEqual(t.results[0].nearby_sites,['Tampa General, Tampa, Florida']);assert.equal(t.results[0].phase,'PHASE3');
 assert.match(tf.seen[0].url,/filter\.overallStatus=RECRUITING/);assert.match(tf.seen[0].url,/query\.locn=Tampa/);
 const lf=fake([[/api\.fda\.gov\/drug\/label/,{meta:{results:{total:3}},results:[{set_id:'abc-123',effective_time:'20250101',boxed_warning:['WARNING: PREMATURE DISCONTINUATION INCREASES RISK'],dosage_and_administration:['5 mg twice daily.'],openfda:{brand_name:['ELIQUIS'],generic_name:['APIXABAN'],manufacturer_name:['BMS']}}]}]]);
 const l=await evidenceSearch({source:'drug_label',query:'apixaban',sections:['boxed_warning','dosage','pregnancy']},{fetch:lf});
 assert.equal(l.url,'https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=abc-123');assert.equal(l.sections.dosage,'5 mg twice daily.');assert.equal(l.sections.pregnancy,'(section not in this label)');assert.equal(l.other_labels,2);
 assert.match(decodeURIComponent(lf.seen[0].url),/openfda\.generic_name:"apixaban" OR openfda\.brand_name:"apixaban"/);
 await assert.rejects(evidenceSearch({source:'drug_label',query:'x"; drop'},{fetch:lf}),/drug name/);
 await assert.rejects(evidenceSearch({source:'drug_label',query:'apixaban',sections:['secrets']},{fetch:lf}),/Unknown label section/);
 await assert.rejects(evidenceSearch({source:'openevidence',query:'anything here'},{fetch:lf}),/source must be/);
});
test('a failing source is reported, not filled in from memory',async()=>{
 const tools=[];registerEvidence({registerTool:(f,o)=>tools.push([f,o])},{fetch:async()=>({ok:false,status:503})});
 assert.equal(tools[0][1].name,'evidence_search');assert.equal(tools[0][0]({agentId:'phone'}),undefined);
 const r=await tools[0][0]({agentId:'main'}).execute('1',{source:'pubmed',query:'sepsis fluids'});
 assert.equal(r.isError,true);assert.match(r.content[0].text,/HTTP 503/);assert.match(r.content[0].text,/do not answer from memory/);
});
