// Clinical calculators (Nov 5 2026). Deterministic formulas implemented from the original publications, so the
// assistant never does clinical arithmetic in its head. Every required input must be given: a missing value is
// reported back, never assumed normal. Results are decision support for a physician, not a diagnosis.
// MDCalc content is not used (its terms forbid automated access); these are the published scores themselves.

const B=d=>({type:'bool',d}),I=(min,max,d)=>({type:'int',min,max,d}),N=(min,max,d)=>({type:'num',min,max,d}),E=(values,d)=>({type:'enum',values,d});
const r1=x=>Math.round(x*10)/10,r2=x=>Math.round(x*100)/100,r0=x=>Math.round(x);
const band=(x,list)=>list.find(([max])=>x<=max)[1];   // [[upper bound inclusive, label], ...]

export const CALCS={
 cha2ds2_vasc:{name:'CHA₂DS₂-VASc (stroke risk in atrial fibrillation)',ref:'Lip GYH et al. Chest 2010;137:263-272',
  inputs:{age:I(18,120,'years'),sex:E(['male','female']),chf:B('heart failure or LV dysfunction'),hypertension:B(),diabetes:B(),stroke_tia_thromboembolism:B('prior stroke, TIA or thromboembolism'),vascular_disease:B('prior MI, PAD or aortic plaque')},
  compute:v=>{const s=(v.chf?1:0)+(v.hypertension?1:0)+(v.age>=75?2:v.age>=65?1:0)+(v.diabetes?1:0)+(v.stroke_tia_thromboembolism?2:0)+(v.vascular_disease?1:0)+(v.sex==='female'?1:0);
   const nonSex=s-(v.sex==='female'?1:0);
   return {score:s,max:9,category:nonSex===0?'Low risk (sex category alone does not raise risk)':nonSex===1?'Intermediate: anticoagulation reasonable to consider':'High: oral anticoagulation recommended unless contraindicated',
    note:'2023 ACC/AHA/ACCP/HRS: anticoagulate at a score ≥2 in men or ≥3 in women; consider at 1 (men) / 2 (women).'};}},
 has_bled:{name:'HAS-BLED (major bleeding risk on anticoagulation)',ref:'Pisters R et al. Chest 2010;138:1093-1100',
  inputs:{hypertension_uncontrolled:B('systolic >160 mmHg'),renal_disease:B('dialysis, transplant, Cr ≥2.26 mg/dL'),liver_disease:B('cirrhosis or bilirubin >2× ULN with AST/ALT >3× ULN'),stroke_history:B(),bleeding_history:B('prior major bleed or predisposition'),labile_inr:B('time in therapeutic range <60%'),age_over_65:B(),antiplatelet_or_nsaid:B(),alcohol_8_or_more_per_week:B()},
  compute:v=>{const s=Object.values(v).filter(Boolean).length;return {score:s,max:9,category:s>=3?'High bleeding risk: address modifiable factors and review closely (not by itself a reason to withhold anticoagulation)':'Low to moderate bleeding risk'};}},
 heart_score:{name:'HEART score (major adverse cardiac events in chest pain)',ref:'Six AJ et al. Neth Heart J 2008;16:191-196; Backus BE et al. Int J Cardiol 2013;168:2153-2158',
  inputs:{history:I(0,2,'0 slightly, 1 moderately, 2 highly suspicious'),ecg:I(0,2,'0 normal, 1 nonspecific repolarization, 2 significant ST deviation'),age:I(18,120,'years'),risk_factor_count:I(0,10,'hypercholesterolemia, hypertension, diabetes, smoking, obesity, family history'),known_atherosclerotic_disease:B('prior MI, PCI/CABG, stroke/TIA or PAD'),troponin:I(0,2,'0 ≤ normal limit, 1 1–3×, 2 >3× normal limit')},
  compute:v=>{const age=v.age>=65?2:v.age>=45?1:0,rf=(v.known_atherosclerotic_disease||v.risk_factor_count>=3)?2:v.risk_factor_count>=1?1:0,s=v.history+v.ecg+age+rf+v.troponin;
   return {score:s,max:10,category:s<=3?'Low (6-week MACE about 1.7%)':s<=6?'Moderate (6-week MACE about 16.6%)':'High (6-week MACE about 50%)',components:{history:v.history,ecg:v.ecg,age,risk_factors:rf,troponin:v.troponin}};}},
 timi_ua_nstemi:{name:'TIMI risk score for UA/NSTEMI',ref:'Antman EM et al. JAMA 2000;284:835-842',
  inputs:{age_65_or_older:B(),three_or_more_cad_risk_factors:B(),known_cad_stenosis_50:B('known coronary stenosis ≥50%'),aspirin_last_7_days:B(),severe_angina_2_episodes_24h:B(),st_deviation_0_5mm:B(),positive_cardiac_marker:B()},
  compute:v=>{const s=Object.values(v).filter(Boolean).length,risk={0:4.7,1:4.7,2:8.3,3:13.2,4:19.9,5:26.2,6:40.9,7:40.9}[s];
   return {score:s,max:7,category:`14-day death, MI or urgent revascularization about ${risk}%`};}},
 wells_pe:{name:'Wells criteria for pulmonary embolism',ref:'Wells PS et al. Thromb Haemost 2000;83:416-420',
  inputs:{clinical_signs_dvt:B(),pe_most_likely_diagnosis:B(),heart_rate_over_100:B(),immobilization_or_surgery_4_weeks:B(),previous_pe_or_dvt:B(),hemoptysis:B(),malignancy:B('treatment within 6 months or palliative')},
  compute:v=>{const s=(v.clinical_signs_dvt?3:0)+(v.pe_most_likely_diagnosis?3:0)+(v.heart_rate_over_100?1.5:0)+(v.immobilization_or_surgery_4_weeks?1.5:0)+(v.previous_pe_or_dvt?1.5:0)+(v.hemoptysis?1:0)+(v.malignancy?1:0);
   return {score:s,max:12.5,category:(s<=4?'PE unlikely (≤4): D-dimer to rule out':'PE likely (>4): CT pulmonary angiography')+'; three-tier: '+(s<2?'low':s<=6?'moderate':'high')+' probability'};}},
 wells_dvt:{name:'Wells criteria for DVT',ref:'Wells PS et al. N Engl J Med 2003;349:1227-1235',
  inputs:{active_cancer:B(),paralysis_or_recent_cast:B(),bedridden_3_days_or_surgery_12_weeks:B(),localized_deep_vein_tenderness:B(),entire_leg_swollen:B(),calf_swelling_over_3cm:B(),pitting_edema_symptomatic_leg:B(),collateral_superficial_veins:B(),previous_dvt:B(),alternative_diagnosis_as_likely:B()},
  compute:v=>{const s=Object.entries(v).reduce((t,[k,x])=>t+(x?(k==='alternative_diagnosis_as_likely'?-2:1):0),0);return {score:s,category:s>=2?'DVT likely (≥2): ultrasound':'DVT unlikely (<2): D-dimer to rule out'};}},
 perc:{name:'PERC rule (pulmonary embolism rule-out)',ref:'Kline JA et al. J Thromb Haemost 2004;2:1247-1255',
  inputs:{age_50_or_older:B(),heart_rate_100_or_more:B(),oxygen_saturation_below_95:B(),unilateral_leg_swelling:B(),hemoptysis:B(),surgery_or_trauma_4_weeks:B(),prior_pe_or_dvt:B(),hormone_use:B('oral contraceptives, hormone replacement or estrogen')},
  compute:v=>{const n=Object.values(v).filter(Boolean).length;return {score:n,category:n===0?'PERC negative: PE can be ruled out without testing ONLY if pretest probability is already low (<15%)':'PERC positive: cannot rule out PE by PERC'};}},
 curb65:{name:'CURB-65 (pneumonia severity)',ref:'Lim WS et al. Thorax 2003;58:377-382',
  inputs:{confusion:B(),bun_mg_dl:N(0,300,'blood urea nitrogen, mg/dL (urea >7 mmol/L ≈ BUN >19)'),respiratory_rate:I(0,80),systolic_bp:I(0,300),diastolic_bp:I(0,200),age:I(18,120)},
  compute:v=>{const s=(v.confusion?1:0)+(v.bun_mg_dl>19?1:0)+(v.respiratory_rate>=30?1:0)+((v.systolic_bp<90||v.diastolic_bp<=60)?1:0)+(v.age>=65?1:0);
   return {score:s,max:5,category:s<=1?'Low severity: outpatient treatment usually appropriate':s===2?'Moderate: consider short admission or closely supervised outpatient care':'Severe: hospitalize; consider ICU at 4–5'};}},
 psi_port:{name:'Pneumonia Severity Index (PSI/PORT)',ref:'Fine MJ et al. N Engl J Med 1997;336:243-250',
  inputs:{age:I(18,120),sex:E(['male','female']),nursing_home_resident:B(),neoplastic_disease:B(),liver_disease:B(),heart_failure:B(),cerebrovascular_disease:B(),renal_disease:B(),altered_mental_status:B(),respiratory_rate:I(0,80),systolic_bp:I(0,300),temperature_c:N(25,45),pulse:I(0,300),arterial_ph:N(6.5,8,'use 7.4 only if measured'),bun_mg_dl:N(0,300),sodium:N(90,200),glucose_mg_dl:N(10,3000),hematocrit_pct:N(5,80),pao2_mmhg:N(10,700,'or give an SpO2 below 90 as pao2 59'),pleural_effusion:B()},
  compute:v=>{
   const exam=v.altered_mental_status||v.respiratory_rate>=30||v.systolic_bp<90||v.temperature_c<35||v.temperature_c>=40||v.pulse>=125;
   const comorb=v.neoplastic_disease||v.liver_disease||v.heart_failure||v.cerebrovascular_disease||v.renal_disease;
   let s=v.age-(v.sex==='female'?10:0)+(v.nursing_home_resident?10:0)+(v.neoplastic_disease?30:0)+(v.liver_disease?20:0)+(v.heart_failure?10:0)+(v.cerebrovascular_disease?10:0)+(v.renal_disease?10:0)
    +(v.altered_mental_status?20:0)+(v.respiratory_rate>=30?20:0)+(v.systolic_bp<90?20:0)+((v.temperature_c<35||v.temperature_c>=40)?15:0)+(v.pulse>=125?10:0)
    +(v.arterial_ph<7.35?30:0)+(v.bun_mg_dl>=30?20:0)+(v.sodium<130?20:0)+(v.glucose_mg_dl>=250?10:0)+(v.hematocrit_pct<30?10:0)+(v.pao2_mmhg<60?10:0)+(v.pleural_effusion?10:0);
   const cls=(v.age<=50&&!comorb&&!exam)?'I':s<=70?'II':s<=90?'III':s<=130?'IV':'V';
   const mort={I:'0.1–0.4%',II:'0.6–0.7%',III:'0.9–2.8%',IV:'8.2–9.3%',V:'27.0–31.1%'}[cls];
   return {score:s,risk_class:cls,category:`Class ${cls}, 30-day mortality ${mort}; ${['I','II'].includes(cls)?'outpatient':cls==='III'?'outpatient or brief observation':'inpatient'} care suggested`};}},
 ckd_epi_2021:{name:'eGFR, CKD-EPI 2021 creatinine (race-free)',ref:'Inker LA et al. N Engl J Med 2021;385:1737-1749',
  inputs:{creatinine_mg_dl:N(0.1,30),age:I(18,120),sex:E(['male','female'])},
  compute:v=>{const f=v.sex==='female',k=f?0.7:0.9,a=f?-0.241:-0.302,x=v.creatinine_mg_dl/k;
   const e=142*Math.min(x,1)**a*Math.max(x,1)**-1.2*0.9938**v.age*(f?1.012:1);
   return {score:r0(e),unit:'mL/min/1.73 m²',category:band(e,[[14.999,'G5 kidney failure'],[29.999,'G4 severely decreased'],[44.999,'G3b moderately to severely decreased'],[59.999,'G3a mildly to moderately decreased'],[89.999,'G2 mildly decreased'],[Infinity,'G1 normal or high']]),note:'Not valid in acute kidney injury (creatinine not at steady state).'};}},
 cockcroft_gault:{name:'Creatinine clearance (Cockcroft-Gault)',ref:'Cockcroft DW, Gault MH. Nephron 1976;16:31-41',
  inputs:{age:I(18,120),weight_kg:N(20,400,'state which weight: actual, ideal or adjusted'),creatinine_mg_dl:N(0.1,30),sex:E(['male','female'])},
  compute:v=>{const c=(140-v.age)*v.weight_kg/(72*v.creatinine_mg_dl)*(v.sex==='female'?0.85:1);return {score:r0(c),unit:'mL/min',note:'Used for many drug-dosing labels. In obesity, adjusted body weight is often used.'};}},
 meld_na:{name:'MELD-Na (UNOS 2016)',ref:'Kim WR et al. N Engl J Med 2008;359:1018-1026; OPTN policy 2016',
  inputs:{bilirubin_mg_dl:N(0.1,80),inr:N(0.5,20),creatinine_mg_dl:N(0.1,30),sodium:N(100,180),dialysis_twice_in_week:B('≥2 dialysis sessions or 24 h CVVHD in the past week')},
  compute:v=>{const cr=v.dialysis_twice_in_week?4:Math.min(4,Math.max(1,v.creatinine_mg_dl)),b=Math.max(1,v.bilirubin_mg_dl),i=Math.max(1,v.inr),na=Math.min(137,Math.max(125,v.sodium));
   const meld=Math.round((0.957*Math.log(cr)+0.378*Math.log(b)+1.120*Math.log(i)+0.643)*10);
   const s=meld>11?Math.round(meld+1.32*(137-na)-0.033*meld*(137-na)):meld;
   return {score:Math.min(40,s),meld:meld,note:'US transplant allocation switched to MELD 3.0 in 2023 (also available: meld_3).'};}},
 meld_3:{name:'MELD 3.0',ref:'Kim WR et al. Gastroenterology 2021;161:1887-1895',
  inputs:{sex:E(['male','female']),bilirubin_mg_dl:N(0.1,80),inr:N(0.5,20),creatinine_mg_dl:N(0.1,30),sodium:N(100,180),albumin_g_dl:N(0.5,7),dialysis_twice_in_week:B()},
  compute:v=>{const cr=v.dialysis_twice_in_week?3:Math.min(3,Math.max(1,v.creatinine_mg_dl)),b=Math.max(1,v.bilirubin_mg_dl),i=Math.max(1,v.inr),na=Math.min(137,Math.max(125,v.sodium)),al=Math.min(3.5,Math.max(1.5,v.albumin_g_dl));
   const s=1.33*(v.sex==='female'?1:0)+4.56*Math.log(b)+0.82*(137-na)-0.24*(137-na)*Math.log(b)+9.09*Math.log(i)+11.14*Math.log(cr)+1.85*(3.5-al)-1.83*(3.5-al)*Math.log(cr)+6;
   return {score:Math.min(40,Math.max(6,Math.round(s)))};}},
 child_pugh:{name:'Child-Pugh score (cirrhosis)',ref:'Pugh RN et al. Br J Surg 1973;60:646-649',
  inputs:{bilirubin_mg_dl:N(0.1,80),albumin_g_dl:N(0.5,7),inr:N(0.5,20),ascites:E(['none','mild','moderate_severe']),encephalopathy:E(['none','grade_1_2','grade_3_4'])},
  compute:v=>{const s=(v.bilirubin_mg_dl<2?1:v.bilirubin_mg_dl<=3?2:3)+(v.albumin_g_dl>3.5?1:v.albumin_g_dl>=2.8?2:3)+(v.inr<1.7?1:v.inr<=2.3?2:3)+{none:1,mild:2,moderate_severe:3}[v.ascites]+{none:1,grade_1_2:2,grade_3_4:3}[v.encephalopathy];
   return {score:s,max:15,category:s<=6?'Class A':s<=9?'Class B':'Class C'};}},
 maddrey:{name:"Maddrey discriminant function (alcohol-associated hepatitis)",ref:'Maddrey WC et al. Gastroenterology 1978;75:193-199 (modified 4.6 factor)',
  inputs:{pt_seconds:N(5,200),control_pt_seconds:N(5,50),bilirubin_mg_dl:N(0.1,80)},
  compute:v=>{const s=4.6*(v.pt_seconds-v.control_pt_seconds)+v.bilirubin_mg_dl;return {score:r1(s),category:s>=32?'Severe (≥32): corticosteroids may be considered if no contraindication':'Not severe (<32)'};}},
 qsofa:{name:'qSOFA',ref:'Seymour CW et al. JAMA 2016;315:762-774',
  inputs:{respiratory_rate:I(0,80),altered_mentation:B(),systolic_bp:I(0,300)},
  compute:v=>{const s=(v.respiratory_rate>=22?1:0)+(v.altered_mentation?1:0)+(v.systolic_bp<=100?1:0);return {score:s,max:3,category:s>=2?'≥2: higher risk of poor outcome; assess for sepsis and organ dysfunction':'<2: does not rule out sepsis'};}},
 sofa:{name:'SOFA score',ref:'Vincent JL et al. Intensive Care Med 1996;22:707-710',
  inputs:{pao2_fio2:N(10,700,'PaO2/FiO2 in mmHg'),mechanical_ventilation_or_cpap:B('respiratory support'),platelets_k:N(0,2000,'×10³/µL'),bilirubin_mg_dl:N(0.1,80),map_mmhg:N(10,200),vasopressor:E(['none','dopamine_le_5_or_dobutamine','dopamine_gt_5_or_epi_ne_le_0_1','dopamine_gt_15_or_epi_ne_gt_0_1'],'doses in µg/kg/min'),gcs:I(3,15),creatinine_mg_dl:N(0.1,30),urine_output_ml_day:N(0,20000,'optional')},
  optional:['urine_output_ml_day'],
  compute:v=>{
   let resp=v.pao2_fio2>=400?0:v.pao2_fio2>=300?1:v.pao2_fio2>=200?2:v.pao2_fio2>=100?3:4;if(resp>2&&!v.mechanical_ventilation_or_cpap)resp=2;
   const coag=v.platelets_k>=150?0:v.platelets_k>=100?1:v.platelets_k>=50?2:v.platelets_k>=20?3:4;
   const liver=v.bilirubin_mg_dl<1.2?0:v.bilirubin_mg_dl<2?1:v.bilirubin_mg_dl<6?2:v.bilirubin_mg_dl<12?3:4;
   const cv={none:v.map_mmhg>=70?0:1,dopamine_le_5_or_dobutamine:2,dopamine_gt_5_or_epi_ne_le_0_1:3,dopamine_gt_15_or_epi_ne_gt_0_1:4}[v.vasopressor];
   const cns=v.gcs===15?0:v.gcs>=13?1:v.gcs>=10?2:v.gcs>=6?3:4;
   let renal=v.creatinine_mg_dl<1.2?0:v.creatinine_mg_dl<2?1:v.creatinine_mg_dl<3.5?2:v.creatinine_mg_dl<5?3:4;
   if(v.urine_output_ml_day!==undefined)renal=Math.max(renal,v.urine_output_ml_day<200?4:v.urine_output_ml_day<500?3:0);
   const parts={respiratory:resp,coagulation:coag,liver,cardiovascular:cv,cns,renal};
   return {score:Object.values(parts).reduce((a,b)=>a+b,0),max:24,components:parts,note:'An acute rise of ≥2 points from baseline defines sepsis-related organ dysfunction (Sepsis-3).'};}},
 gcs:{name:'Glasgow Coma Scale',ref:'Teasdale G, Jennett B. Lancet 1974;2:81-84',
  inputs:{eye:I(1,4),verbal:I(1,5),motor:I(1,6)},
  compute:v=>{const s=v.eye+v.verbal+v.motor;return {score:s,max:15,category:s<=8?'Severe (≤8)':s<=12?'Moderate (9–12)':'Mild (13–15)'};}},
 anion_gap:{name:'Anion gap (with albumin correction)',ref:'Figge J et al. Crit Care Med 1998;26:1807-1810 (albumin correction)',
  inputs:{sodium:N(90,200),chloride:N(50,160),bicarbonate:N(1,60),albumin_g_dl:N(0.5,7,'optional')},optional:['albumin_g_dl'],
  compute:v=>{const ag=v.sodium-(v.chloride+v.bicarbonate);return {score:r1(ag),unit:'mEq/L',...v.albumin_g_dl!==undefined?{albumin_corrected:r1(ag+2.5*(4-v.albumin_g_dl))}:{},note:'Reference range depends on the lab (often about 4–12 mEq/L with current analyzers).'};}},
 corrected_sodium:{name:'Sodium corrected for hyperglycemia',ref:'Katz MA. N Engl J Med 1973;289:843-844; Hillier TA et al. Am J Med 1999;106:399-403',
  inputs:{sodium:N(90,200),glucose_mg_dl:N(10,3000)},
  compute:v=>{const ex=Math.max(0,v.glucose_mg_dl-100)/100;return {score:r1(v.sodium+1.6*ex),unit:'mEq/L (Katz, 1.6)',hillier_2_4:r1(v.sodium+2.4*ex)};}},
 corrected_calcium:{name:'Calcium corrected for albumin',ref:'Payne RB et al. Br Med J 1973;4:643-646',
  inputs:{calcium_mg_dl:N(1,25),albumin_g_dl:N(0.5,7)},
  compute:v=>({score:r1(v.calcium_mg_dl+0.8*(4-v.albumin_g_dl)),unit:'mg/dL',note:'Ionized calcium is preferred when available, especially in critical illness or CKD.'})},
 aa_gradient:{name:'Alveolar–arterial O₂ gradient',ref:'Alveolar gas equation (respiratory quotient 0.8)',
  inputs:{fio2:N(0.21,1,'fraction, e.g. 0.21'),pao2_mmhg:N(10,700),paco2_mmhg:N(5,200),age:I(1,120),atmospheric_mmhg:N(400,800,'optional, default 760 (sea level)')},optional:['atmospheric_mmhg'],
  compute:v=>{const pa=v.fio2*((v.atmospheric_mmhg??760)-47)-v.paco2_mmhg/0.8,g=pa-v.pao2_mmhg;return {score:r1(g),unit:'mmHg',alveolar_po2:r1(pa),expected_upper:r1(v.age/4+4),category:g>v.age/4+4?'Elevated for age':'Normal for age'};}},
 qtc:{name:'Corrected QT (Bazett, Fridericia)',ref:'Bazett HC. Heart 1920;7:353-370; Fridericia LS. Acta Med Scand 1920;53:469-486',
  inputs:{qt_ms:N(150,800),heart_rate:N(20,300)},
  compute:v=>{const rr=60/v.heart_rate;return {score:r0(v.qt_ms/Math.cbrt(rr)),unit:'ms (Fridericia)',bazett:r0(v.qt_ms/Math.sqrt(rr)),note:'Fridericia is preferred at fast or slow heart rates. Above 500 ms (or a rise >60 ms) carries higher torsades risk.'};}},
 free_water_deficit:{name:'Free water deficit (hypernatremia)',ref:'Adrogué HJ, Madias NE. N Engl J Med 2000;342:1493-1499',
  inputs:{sodium:N(140,220),weight_kg:N(2,400),age:I(0,120),sex:E(['male','female'])},
  compute:v=>{const f=v.age<18?0.6:v.age>=65?(v.sex==='female'?0.45:0.5):(v.sex==='female'?0.5:0.6),tbw=f*v.weight_kg,d=tbw*(v.sodium/140-1);
   return {score:r1(d),unit:'L',total_body_water_l:r1(tbw),note:'Correct chronic hypernatremia by no more than about 10–12 mEq/L in 24 h; add ongoing losses.'};}},
 fena:{name:'Fractional excretion of sodium (and urea)',ref:'Espinel CH. JAMA 1976;236:579-581; Carvounis CP et al. Kidney Int 2002;62:2223-2229',
  inputs:{urine_sodium:N(0,400),plasma_sodium:N(90,200),urine_creatinine_mg_dl:N(1,1000),plasma_creatinine_mg_dl:N(0.1,30),urine_urea_mg_dl:N(1,5000,'optional'),bun_mg_dl:N(1,300,'optional')},optional:['urine_urea_mg_dl','bun_mg_dl'],
  compute:v=>{const fe=v.urine_sodium*v.plasma_creatinine_mg_dl/(v.plasma_sodium*v.urine_creatinine_mg_dl)*100;
   const out={score:r2(fe),unit:'%',category:fe<1?'<1%: suggests prerenal':fe>2?'>2%: suggests intrinsic renal (ATN)':'1–2%: indeterminate',note:'Unreliable on diuretics: use FEUrea (<35% suggests prerenal).'};
   if(v.urine_urea_mg_dl!==undefined&&v.bun_mg_dl!==undefined)out.feurea_pct=r1(v.urine_urea_mg_dl*v.plasma_creatinine_mg_dl/(v.bun_mg_dl*v.urine_creatinine_mg_dl)*100);
   return out;}},
 map:{name:'Mean arterial pressure',ref:'Standard estimate (SBP + 2×DBP)/3',
  inputs:{systolic_bp:N(20,300),diastolic_bp:N(10,200)},compute:v=>({score:r0((v.systolic_bp+2*v.diastolic_bp)/3),unit:'mmHg'})},
 bmi_bsa:{name:'BMI and body surface area (Mosteller)',ref:'Mosteller RD. N Engl J Med 1987;317:1098',
  inputs:{weight_kg:N(1,400),height_cm:N(30,250)},
  compute:v=>{const bmi=v.weight_kg/(v.height_cm/100)**2;return {score:r1(bmi),unit:'kg/m²',bsa_m2:r2(Math.sqrt(v.height_cm*v.weight_kg/3600)),category:band(bmi,[[18.499,'Underweight'],[24.999,'Normal'],[29.999,'Overweight'],[34.999,'Obesity class 1'],[39.999,'Obesity class 2'],[Infinity,'Obesity class 3']])};}},
 abcd2:{name:'ABCD² (stroke risk after TIA)',ref:'Johnston SC et al. Lancet 2007;369:283-292',
  inputs:{age_60_or_older:B(),bp_140_90_or_higher:B('at first assessment'),clinical:E(['unilateral_weakness','speech_disturbance_without_weakness','other']),duration_minutes:I(0,10000),diabetes:B()},
  compute:v=>{const s=(v.age_60_or_older?1:0)+(v.bp_140_90_or_higher?1:0)+{unilateral_weakness:2,speech_disturbance_without_weakness:1,other:0}[v.clinical]+(v.duration_minutes>=60?2:v.duration_minutes>=10?1:0)+(v.diabetes?1:0);
   return {score:s,max:7,category:s<=3?'Low (2-day stroke risk about 1.0%)':s<=5?'Moderate (2-day stroke risk about 4.1%)':'High (2-day stroke risk about 8.1%)',note:'Current guidance favors urgent evaluation of every TIA regardless of score.'};}},
 spesi:{name:'Simplified PESI (pulmonary embolism)',ref:'Jiménez D et al. Arch Intern Med 2010;170:1383-1389',
  inputs:{age_over_80:B(),cancer:B(),chronic_cardiopulmonary_disease:B(),heart_rate_110_or_more:B(),systolic_bp_below_100:B(),oxygen_saturation_below_90:B()},
  compute:v=>{const s=Object.values(v).filter(Boolean).length;return {score:s,max:6,category:s===0?'Low risk (30-day mortality about 1.0%): consider outpatient treatment if otherwise suitable':'Not low risk (30-day mortality about 10.9%)'};}},
 rcri:{name:'Revised Cardiac Risk Index',ref:'Lee TH et al. Circulation 1999;100:1043-1049',
  inputs:{high_risk_surgery:B('intraperitoneal, intrathoracic or suprainguinal vascular'),ischemic_heart_disease:B(),heart_failure:B(),cerebrovascular_disease:B(),insulin_treated_diabetes:B(),creatinine_over_2:B('preoperative creatinine >2 mg/dL')},
  compute:v=>{const s=Object.values(v).filter(Boolean).length;return {score:s,max:6,category:'Class '+['I','II','III','IV'][Math.min(s,3)]+(s>=3?' (3 or more predictors)':''),note:'Risk percentages differ between the original and later validations; interpret with current perioperative guidelines.'};}},
 centor_mcisaac:{name:'Centor score, McIsaac modification (strep pharyngitis)',ref:'McIsaac WJ et al. CMAJ 1998;158:75-83',
  inputs:{temperature_over_38:B(),cough_absent:B(),tender_anterior_cervical_nodes:B(),tonsillar_swelling_or_exudate:B(),age:I(3,120)},
  compute:v=>{const s=(v.temperature_over_38?1:0)+(v.cough_absent?1:0)+(v.tender_anterior_cervical_nodes?1:0)+(v.tonsillar_swelling_or_exudate?1:0)+(v.age<=14?1:v.age>=45?-1:0);
   return {score:s,category:s<=1?'Low probability: no testing or antibiotics':'Test (rapid antigen test and/or culture); treat if positive',note:'IDSA advises testing rather than empiric antibiotics.'};}},
 ottawa_ankle:{name:'Ottawa ankle and foot rules',ref:'Stiell IG et al. JAMA 1993;269:1127-1132',
  inputs:{malleolar_zone_pain:B(),tender_posterior_lateral_malleolus:B('posterior edge or tip, distal 6 cm'),tender_posterior_medial_malleolus:B(),midfoot_zone_pain:B(),tender_base_fifth_metatarsal:B(),tender_navicular:B(),unable_to_bear_weight_4_steps:B('immediately and in the ED')},
  compute:v=>{const ankle=v.malleolar_zone_pain&&(v.tender_posterior_lateral_malleolus||v.tender_posterior_medial_malleolus||v.unable_to_bear_weight_4_steps);
   const foot=v.midfoot_zone_pain&&(v.tender_base_fifth_metatarsal||v.tender_navicular||v.unable_to_bear_weight_4_steps);
   return {score:(ankle?1:0)+(foot?1:0),category:[ankle?'Ankle X-ray indicated':'No ankle X-ray needed by the rule',foot?'foot X-ray indicated':'no foot X-ray needed by the rule'].join('; ')};}},
 stop_bang:{name:'STOP-BANG (obstructive sleep apnea)',ref:'Chung F et al. Anesthesiology 2008;108:812-821; Br J Anaesth 2012;108:768-775',
  inputs:{snoring:B(),tiredness:B(),observed_apnea:B(),high_blood_pressure:B(),bmi_over_35:B(),age_over_50:B(),neck_over_40cm:B(),male:B()},
  compute:v=>{const s=Object.values(v).filter(Boolean).length;return {score:s,max:8,category:s<=2?'Low risk':s<=4?'Intermediate risk':'High risk'};}},
 phq9:{name:'PHQ-9 (depression severity)',ref:'Kroenke K et al. J Gen Intern Med 2001;16:606-613',
  inputs:{items:{type:'items',count:9,min:0,max:3,d:'nine answers, each 0–3, in order'}},
  compute:v=>{const s=v.items.reduce((a,b)=>a+b,0);return {score:s,max:27,category:band(s,[[4,'Minimal'],[9,'Mild'],[14,'Moderate'],[19,'Moderately severe'],[27,'Severe']]),...v.items[8]>0?{safety_flag:'Item 9 (thoughts of self-harm) is positive: needs same-day clinical safety assessment.'}:{}};}},
 gad7:{name:'GAD-7 (anxiety severity)',ref:'Spitzer RL et al. Arch Intern Med 2006;166:1092-1097',
  inputs:{items:{type:'items',count:7,min:0,max:3,d:'seven answers, each 0–3, in order'}},
  compute:v=>{const s=v.items.reduce((a,b)=>a+b,0);return {score:s,max:21,category:band(s,[[4,'Minimal'],[9,'Mild'],[14,'Moderate'],[21,'Severe']])};}},
};

function check(name,spec,value){
 if(spec.type==='bool'){if(typeof value!=='boolean')throw new Error(`${name} must be true or false.`);return value;}
 if(spec.type==='enum'){if(!spec.values.includes(value))throw new Error(`${name} must be one of: ${spec.values.join(', ')}.`);return value;}
 if(spec.type==='items'){if(!Array.isArray(value)||value.length!==spec.count||value.some(x=>!Number.isInteger(x)||x<spec.min||x>spec.max))throw new Error(`${name} must be ${spec.count} whole numbers from ${spec.min} to ${spec.max}.`);return value;}
 const n=typeof value==='string'&&value.trim()!==''?Number(value):value;
 if(typeof n!=='number'||!Number.isFinite(n)||(spec.type==='int'&&!Number.isInteger(n)))throw new Error(`${name} must be a ${spec.type==='int'?'whole ':''}number.`);
 if(n<spec.min||n>spec.max)throw new Error(`${name} must be between ${spec.min} and ${spec.max}${spec.d?' ('+spec.d+')':''}.`);
 return n;
}

export function listCalculators(){return Object.entries(CALCS).map(([id,c])=>({id,name:c.name}));}
export function describe(id){
 const c=CALCS[id];if(!c)throw new Error('Unknown calculator. Call with action "list".');
 return {id,name:c.name,reference:c.ref,inputs:Object.fromEntries(Object.entries(c.inputs).map(([k,s])=>[k,{type:s.type,...s.values?{values:s.values}:{},...s.min!==undefined?{min:s.min,max:s.max}:{},...s.d?{about:s.d}:{},...(c.optional||[]).includes(k)?{optional:true}:{}}]))};
}
export function calculate(id,inputs={}){
 const c=CALCS[id];if(!c)throw new Error('Unknown calculator. Call with action "list".');
 if(!inputs||typeof inputs!=='object'||Array.isArray(inputs))throw new Error('inputs must be an object.');
 const extra=Object.keys(inputs).filter(k=>!(k in c.inputs));
 if(extra.length)throw new Error('Unknown inputs for '+id+': '+extra.join(', ')+'. Call with action "describe".');
 const missing=Object.keys(c.inputs).filter(k=>!(c.optional||[]).includes(k)&&(inputs[k]===undefined||inputs[k]===null||inputs[k]===''));
 if(missing.length)return {calculator:id,name:c.name,complete:false,missing,note:'Not calculated: these values are needed. Ask the owner or look them up; never assume a normal value.'};
 const v={};for(const k of Object.keys(c.inputs))if(inputs[k]!==undefined&&inputs[k]!==null&&inputs[k]!=='')v[k]=check(k,c.inputs[k],inputs[k]);
 return {calculator:id,name:c.name,complete:true,...c.compute(v),inputs_used:v,reference:c.ref,disclaimer:'Decision support from the published score; clinical judgment applies.'};
}

// 0.24.3 (B01 audit): the gateway closes an object with no listed properties, so the inputs object lists every input
// of every calculator (from CALCS); describe gives the ones a given calculator needs.
const JTYPE={bool:'boolean',int:'integer',num:'number',enum:'string'};
export const INPUTS_SCHEMA=(()=>{const props={};
 for(const c of Object.values(CALCS))for(const [k,spec] of Object.entries(c.inputs)){
  const t=JTYPE[spec.type],cur=props[k];
  if(!cur){props[k]={type:t,...(spec.type==='enum'?{enum:[...spec.values]}:{})};continue;}
  if(cur.type!==t){const types=new Set([].concat(cur.type,t));delete cur.enum;cur.type=types.size===2&&types.has('integer')&&types.has('number')?'number':[...types];}
  else if(spec.type==='enum')cur.enum=[...new Set([...cur.enum,...spec.values])];
 }
 return {type:'object',additionalProperties:false,description:'Named inputs for the chosen calculator (action describe lists them), e.g. {"age":72,"sex":"female","chf":false}.',properties:props};})();

export function registerCalculators(api){
 const text=x=>({content:[{type:'text',text:JSON.stringify(x)}]});
 api.registerTool(ctx=>ctx.agentId==='main'?{name:'clinical_calc',label:'clinical_calc',
  description:'Exact clinical scores and formulas from the original publications (use instead of mental arithmetic): '+Object.keys(CALCS).join(', ')+'. action list | describe (inputs for one calculator) | calculate (calculator + inputs). Every required input must be supplied; missing ones come back as "missing" — ask for them, never assume normal. Report the score, category and reference. For scores not listed, tell the owner and give them the MDCalc page to run themselves (never open mdcalc.com).',
  parameters:{type:'object',properties:{action:{type:'string',enum:['list','describe','calculate']},calculator:{type:'string',maxLength:40},inputs:INPUTS_SCHEMA},required:['action'],additionalProperties:false},
  async execute(_id,a){try{ctx.assertInvocationCurrent?.();
   if(a.action==='list')return text({calculators:listCalculators()});
   if(a.action==='describe')return text(describe(a.calculator));
   return text(calculate(a.calculator,a.inputs||{}));
  }catch(e){return {...text({error:String(e?.message||e)}),isError:true};}}
 }:undefined,{optional:true,name:'clinical_calc'});
}
