#!/usr/bin/env bash
# Read-only check of the Google Calendar / Tasks connection on the droplet (Nov 4: the bot's
# calendar_create_event returned an error after the CVS booking). Prints only ok/error and counts,
# never event titles or email contents. Usage: bash bin/check-google.sh
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
ssh "$HOST" python3 - <<'PY'
import json,socket
def call(action,args={}):
    s=socket.socket(socket.AF_UNIX);s.settimeout(60);s.connect('/run/procurator-mail/drafts.sock')
    s.sendall((json.dumps({'action':action,'args':args})+'\n').encode());d=b''
    while b'\n' not in d:
        c=s.recv(65536)
        if not c:break
        d+=c
    s.close();return json.loads(d.split(b'\n')[0])
st=call('status')
r=st.get('result') or {}
print('status:', 'ok' if st.get('ok') else 'ERROR '+str(st.get('error'))[:300])
for k in ('gmail_send_authorized','gmail_modify_authorized','google_calendar_tasks_authorized'):print(' ',k,'=',r.get(k))
for action,args,key in (('cal_calendars',{},'calendars'),('cal_events',{'days':3},'events'),('tasks_lists',{},'lists')):
    x=call(action,args)
    if x.get('ok'):
        v=x.get('result') or {};n=next((len(val) for val in v.values() if isinstance(val,list)),'?')
        print(action+': ok,',n,'item(s)')
    else:print(action+': ERROR',str(x.get('error'))[:400])
PY
echo "--- mail broker errors, last 3 hours ---"
ssh "$HOST" "journalctl -u procurator-mail --since '-3h' --no-pager 2>/dev/null | grep -iE 'error|traceback|exception|gog' | tail -20" || true
echo "--- gateway calendar tool errors, last 3 hours ---"
ssh "$HOST" "docker logs --since 3h procurator-openclaw-gateway-1 2>&1 | grep -iE 'calendar|cal_create|procurator-mail' | grep -iE 'error|fail|denied|invalid' | tail -20" || true
