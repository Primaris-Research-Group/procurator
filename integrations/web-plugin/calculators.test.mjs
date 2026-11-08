import {test} from 'node:test';
import assert from 'node:assert/strict';
import {calculate,describe,listCalculators,registerCalculators,CALCS} from './calculators.mjs';
const c=(id,inputs)=>calculate(id,inputs);
test('missing values are reported, never assumed; bad values and unknown inputs are refused',()=>{
 const r=c('cha2ds2_vasc',{age:70,sex:'female'});assert.equal(r.complete,false);assert.deepEqual(r.missing,['chf','hypertension','diabetes','stroke_tia_thromboembolism','vascular_disease']);assert.equal(r.score,undefined);
 assert.throws(()=>c('cha2ds2_vasc',{age:70,sex:'f',chf:false,hypertension:false,diabetes:false,stroke_tia_thromboembolism:false,vascular_disease:false}),/one of/);
 assert.throws(()=>c('gcs',{eye:5,verbal:5,motor:6}),/between 1 and 4/);
 assert.throws(()=>c('gcs',{eye:4,verbal:5,motor:6,pupils:3}),/Unknown inputs/);
 assert.throws(()=>c('nope',{}),/Unknown calculator/);
 assert.ok(listCalculators().length>=30);assert.equal(describe('sofa').inputs.urine_output_ml_day.optional,true);
 for(const [id,x] of Object.entries(CALCS))assert.ok(x.ref&&x.name&&Object.keys(x.inputs).length,id);
});
test('cardiology scores match worked examples',()=>{
 const af={chf:true,hypertension:true,diabetes:false,stroke_tia_thromboembolism:true,vascular_disease:false};
 assert.equal(c('cha2ds2_vasc',{age:76,sex:'female',...af}).score,7);          // 1+1+2+2+1
 assert.equal(c('cha2ds2_vasc',{age:66,sex:'female',chf:false,hypertension:false,diabetes:false,stroke_tia_thromboembolism:false,vascular_disease:false}).score,2);
 assert.match(c('cha2ds2_vasc',{age:40,sex:'female',chf:false,hypertension:false,diabetes:false,stroke_tia_thromboembolism:false,vascular_disease:false}).category,/Low/);
 const h=c('heart_score',{history:1,ecg:1,age:58,risk_factor_count:2,known_atherosclerotic_disease:false,troponin:0});assert.equal(h.score,4);assert.match(h.category,/Moderate/);
 assert.equal(c('heart_score',{history:2,ecg:2,age:70,risk_factor_count:0,known_atherosclerotic_disease:true,troponin:2}).score,10);
 const t=c('timi_ua_nstemi',{age_65_or_older:true,three_or_more_cad_risk_factors:true,known_cad_stenosis_50:false,aspirin_last_7_days:true,severe_angina_2_episodes_24h:false,st_deviation_0_5mm:false,positive_cardiac_marker:false});assert.equal(t.score,3);assert.match(t.category,/13.2%/);
 assert.equal(c('has_bled',{hypertension_uncontrolled:true,renal_disease:false,liver_disease:false,stroke_history:true,bleeding_history:false,labile_inr:false,age_over_65:true,antiplatelet_or_nsaid:false,alcohol_8_or_more_per_week:false}).score,3);
 const q=c('qtc',{qt_ms:400,heart_rate:60});assert.equal(q.score,400);assert.equal(q.bazett,400);
 assert.equal(c('qtc',{qt_ms:360,heart_rate:100}).bazett,465);           // RR 0.6 s → 360/√0.6
 assert.equal(c('rcri',{high_risk_surgery:true,ischemic_heart_disease:true,heart_failure:false,cerebrovascular_disease:false,insulin_treated_diabetes:false,creatinine_over_2:false}).category,'Class III');
});
test('VTE and pulmonary scores',()=>{
 const w=c('wells_pe',{clinical_signs_dvt:false,pe_most_likely_diagnosis:true,heart_rate_over_100:true,immobilization_or_surgery_4_weeks:false,previous_pe_or_dvt:false,hemoptysis:false,malignancy:false});assert.equal(w.score,4.5);assert.match(w.category,/PE likely.*moderate/);
 assert.equal(c('wells_dvt',{active_cancer:true,paralysis_or_recent_cast:false,bedridden_3_days_or_surgery_12_weeks:false,localized_deep_vein_tenderness:true,entire_leg_swollen:false,calf_swelling_over_3cm:false,pitting_edema_symptomatic_leg:false,collateral_superficial_veins:false,previous_dvt:false,alternative_diagnosis_as_likely:true}).score,0);
 assert.match(c('perc',{age_50_or_older:false,heart_rate_100_or_more:false,oxygen_saturation_below_95:false,unilateral_leg_swelling:false,hemoptysis:false,surgery_or_trauma_4_weeks:false,prior_pe_or_dvt:false,hormone_use:false}).category,/ONLY if pretest probability is already low/);
 assert.equal(c('curb65',{confusion:false,bun_mg_dl:25,respiratory_rate:32,systolic_bp:100,diastolic_bp:60,age:70}).score,4);
 const psi={age:72,sex:'female',nursing_home_resident:false,neoplastic_disease:false,liver_disease:false,heart_failure:true,cerebrovascular_disease:false,renal_disease:false,altered_mental_status:false,respiratory_rate:24,systolic_bp:118,temperature_c:38.2,pulse:104,arterial_ph:7.40,bun_mg_dl:32,sodium:134,glucose_mg_dl:140,hematocrit_pct:36,pao2_mmhg:64,pleural_effusion:true};
 const p=c('psi_port',psi);assert.equal(p.score,72-10+10+20+10);assert.equal(p.risk_class,'IV');
 assert.equal(c('psi_port',{...psi,age:40,heart_failure:false,bun_mg_dl:10,pleural_effusion:false}).risk_class,'I');
 assert.match(c('spesi',{age_over_80:false,cancer:false,chronic_cardiopulmonary_disease:false,heart_rate_110_or_more:false,systolic_bp_below_100:false,oxygen_saturation_below_90:false}).category,/Low risk/);
 const aa=c('aa_gradient',{fio2:0.21,pao2_mmhg:90,paco2_mmhg:40,age:40});assert.equal(aa.alveolar_po2,99.7);assert.equal(aa.score,9.7);
});
test('renal, liver and electrolyte formulas',()=>{
 assert.equal(c('ckd_epi_2021',{creatinine_mg_dl:1.0,age:60,sex:'female'}).score,64);
 assert.equal(c('ckd_epi_2021',{creatinine_mg_dl:1.0,age:60,sex:'male'}).score,86);
 assert.equal(c('cockcroft_gault',{age:80,weight_kg:60,creatinine_mg_dl:1.2,sex:'female'}).score,35);  // (60*60)/(86.4)*0.85
 const m=c('meld_na',{bilirubin_mg_dl:2,inr:1.5,creatinine_mg_dl:1.5,sodium:130,dialysis_twice_in_week:false});
 assert.equal(m.meld,17);assert.equal(m.score,Math.round(17+1.32*7-0.033*17*7));
 assert.equal(c('meld_na',{bilirubin_mg_dl:0.5,inr:0.9,creatinine_mg_dl:0.6,sodium:140,dialysis_twice_in_week:false}).score,6);
 assert.equal(c('meld_3',{sex:'male',bilirubin_mg_dl:1,inr:1,creatinine_mg_dl:1,sodium:137,albumin_g_dl:3.5,dialysis_twice_in_week:false}).score,6);
 assert.equal(c('child_pugh',{bilirubin_mg_dl:2.5,albumin_g_dl:3.0,inr:1.8,ascites:'mild',encephalopathy:'none'}).score,9);
 assert.equal(c('maddrey',{pt_seconds:20,control_pt_seconds:12,bilirubin_mg_dl:8}).score,44.8);
 const ag=c('anion_gap',{sodium:140,chloride:100,bicarbonate:24,albumin_g_dl:2});assert.equal(ag.score,16);assert.equal(ag.albumin_corrected,21);
 const na=c('corrected_sodium',{sodium:130,glucose_mg_dl:600});assert.equal(na.score,138);assert.equal(na.hillier_2_4,142);
 assert.equal(c('corrected_calcium',{calcium_mg_dl:8,albumin_g_dl:2}).score,9.6);
 const fw=c('free_water_deficit',{sodium:154,weight_kg:70,age:40,sex:'male'});assert.equal(fw.score,4.2);
 const fe=c('fena',{urine_sodium:10,plasma_sodium:140,urine_creatinine_mg_dl:100,plasma_creatinine_mg_dl:2});assert.equal(fe.score,0.14);assert.match(fe.category,/prerenal/);
});
test('critical care, neuro and screening',()=>{
 const s=c('sofa',{pao2_fio2:250,mechanical_ventilation_or_cpap:false,platelets_k:90,bilirubin_mg_dl:2.5,map_mmhg:65,vasopressor:'none',gcs:14,creatinine_mg_dl:2.1});
 assert.deepEqual(s.components,{respiratory:2,coagulation:2,liver:2,cardiovascular:1,cns:1,renal:2});assert.equal(s.score,10);
 assert.equal(c('sofa',{pao2_fio2:150,mechanical_ventilation_or_cpap:false,platelets_k:200,bilirubin_mg_dl:0.5,map_mmhg:80,vasopressor:'none',gcs:15,creatinine_mg_dl:0.8}).components.respiratory,2,'no support caps at 2');
 assert.equal(c('sofa',{pao2_fio2:150,mechanical_ventilation_or_cpap:true,platelets_k:200,bilirubin_mg_dl:0.5,map_mmhg:80,vasopressor:'dopamine_gt_15_or_epi_ne_gt_0_1',gcs:15,creatinine_mg_dl:0.8,urine_output_ml_day:150}).score,3+4+4);
 assert.equal(c('qsofa',{respiratory_rate:22,altered_mentation:false,systolic_bp:100}).score,2);
 assert.equal(c('gcs',{eye:2,verbal:2,motor:4}).category,'Severe (≤8)');
 assert.match(c('abcd2',{age_60_or_older:true,bp_140_90_or_higher:true,clinical:'unilateral_weakness',duration_minutes:45,diabetes:false}).category,/Moderate/);
 assert.equal(c('centor_mcisaac',{temperature_over_38:true,cough_absent:true,tender_anterior_cervical_nodes:false,tonsillar_swelling_or_exudate:true,age:50}).score,2);
 assert.match(c('ottawa_ankle',{malleolar_zone_pain:true,tender_posterior_lateral_malleolus:false,tender_posterior_medial_malleolus:false,midfoot_zone_pain:false,tender_base_fifth_metatarsal:false,tender_navicular:false,unable_to_bear_weight_4_steps:false}).category,/No ankle X-ray/);
 const p=c('phq9',{items:[1,2,1,2,1,1,2,1,1]});assert.equal(p.score,12);assert.equal(p.category,'Moderate');assert.match(p.safety_flag,/same-day/);
 assert.equal(c('gad7',{items:[3,3,3,3,3,3,3]}).category,'Severe');
 assert.throws(()=>c('phq9',{items:[1,2]}),/9 whole numbers/);
 const b=c('bmi_bsa',{weight_kg:80,height_cm:180});assert.equal(b.score,24.7);assert.equal(b.bsa_m2,2);
 assert.equal(c('map',{systolic_bp:120,diastolic_bp:80}).score,93);
});
test('tool: main agent only, errors come back as errors',async()=>{
 const tools=[];registerCalculators({registerTool:(f,o)=>tools.push([f,o])});
 assert.equal(tools[0][1].name,'clinical_calc');assert.equal(tools[0][0]({agentId:'phone'}),undefined);
 const t=tools[0][0]({agentId:'main'});
 const ok=JSON.parse((await t.execute('1',{action:'calculate',calculator:'gcs',inputs:{eye:4,verbal:5,motor:6}})).content[0].text);assert.equal(ok.score,15);
 const bad=await t.execute('2',{action:'calculate',calculator:'gcs',inputs:{eye:9,verbal:5,motor:6}});assert.equal(bad.isError,true);
});
