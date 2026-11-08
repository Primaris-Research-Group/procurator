#!/usr/bin/env bash
# Phase 0 check for the dashboard (Nov 6 2026): is the gateway reporting model usage to the plugin's llm_output hook?
# Run from the procurator-deploy folder on the Mac:  bash bin/check-usage-hook.sh
# Prints what usage.json holds, what the gateway itself reports for the main session, and the difference.
# If usage.json stays empty while the gateway reports tokens, the hook is not firing on this OpenClaw build and the
# meter must fall back to polling the gateway (sessions.usage) — tell Claude the output.
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
ssh "$HOST" bash -s <<'REMOTE_SCRIPT'
set -uo pipefail
cd /opt/procurator
echo "== usage.json (what the plugin's llm_output hook has recorded) =="
python3 - <<'PY'
import json,os,datetime
p='state/procurator-admin/usage.json'
if not os.path.exists(p):
    print('  usage.json does not exist yet: the hook has never recorded a reply (or the plugin has not been reloaded since the meter was added)')
else:
    d=json.load(open(p));days=d.get('days',{})
    print('  days recorded:',len(days))
    for k in sorted(days)[-3:]:
        t=days[k]['total'];print('  %s: %d replies, %d in, %d cached, %d out, $%.4f, by %s%s'%(k,t['calls'],t['input'],t['cacheRead'],t['output'],t['cost'],','.join(days[k].get('by',{}).keys()),(' · packets '+','.join(days[k]['by_mission'].keys())) if days[k].get('by_mission') else ''))
PY
echo "== gateway's own view of the main session (sessions.usage RPC) =="
docker exec -i procurator-openclaw-gateway-1 node --input-type=module <<'JS' 2>&1 | head -40 || true
try{
 const {callGatewayFromCli}=await import('openclaw/plugin-sdk/gateway-runtime');
 const g=(m,p)=>callGatewayFromCli(m,{json:true,timeout:'20000'},p,{progress:false});
 let out=null;
 for(const method of ['sessions.usage','sessions.describe']){
  try{out=await g(method,{key:'agent:main:main'});console.log('  '+method+':',JSON.stringify(out).slice(0,1200));break;}catch(e){console.log('  '+method+' failed:',String(e?.message||e).slice(0,200));}
 }
}catch(e){console.log('  gateway RPC not reachable from here:',String(e?.message||e).slice(0,200));}
JS
echo "== plugin hook registration (gateway log, last 2 days) =="
docker logs --since 48h procurator-openclaw-gateway-1 2>&1 | grep -iE "llm_output|usage-meter|procurator-usage" | tail -5 || echo "  no llm_output lines in the log (normal if the hook simply works silently)"
echo "== which hooks this OpenClaw build offers to plugins =="
docker exec -i procurator-openclaw-gateway-1 sh -c 'grep -rhoE "\"(llm_output|llm_input|agent_end|before_prompt_build|message_received)\"" /app/dist 2>/dev/null | sort | uniq -c' 2>/dev/null || echo "  could not inspect the gateway bundle"
echo
echo "Reading: if 'days recorded' grows after you chat with the bot, the hook works and the dashboard's cost numbers are real."
echo "If it stays at 0 while the gateway RPC shows tokens, send this output to Claude: the meter needs the polling fallback."
REMOTE_SCRIPT
