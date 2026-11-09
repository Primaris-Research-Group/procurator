#!/usr/bin/env bash
# PROCURATOR post-deploy test battery (web 0.19.x). Run from the procurator-deploy folder on the Mac,
# after bash bin/deploy-procurator-web.sh:
#   bash bin/test-deployment.sh            full: ~4-6 min; opens 3 short browsers (practice + example.com in the cloud,
#                                          example.com on the PC); no Telegram messages, nothing typed on real sites
#   bash bin/test-deployment.sh --quick    ~40 s: no browsers, no server test suite
#   bash bin/test-deployment.sh --vision   full + page-vision checks (2 calls, a few cents)
#   bash bin/test-deployment.sh --only BD  just some sections (A platform, B security, C cloud, D home PC, E learning)
# Report: ~/LocalLLMFolder/procurator-test-reports/deploy-test-<time>.txt (outside the repo). Send it to Claude.
# The hands-on half (Telegram approvals, sign-ins, real sites) is the doc "PROCURATOR 0.19.0 — Hands-on test plan".
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
EXPECT=0.32.0
cd "$(dirname "$0")/.."
MODE=full; VISION=0; ONLY=ABCDE
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) MODE=quick ;;
    --vision) VISION=1 ;;
    --only) ONLY="${2:?--only needs letters, e.g. BD}"; shift ;;
    *) echo "Unknown option $1 (use --quick, --vision, --only ABCDE)"; exit 2 ;;
  esac; shift
done
REPORT_DIR="${PROCURATOR_TEST_REPORTS:-$HOME/LocalLLMFolder/procurator-test-reports}"
mkdir -p "$REPORT_DIR"
REPORT="$REPORT_DIR/deploy-test-$(date +%Y%m%d-%H%M%S).txt"

run() {
echo "PROCURATOR deployment test · $(date '+%Y-%m-%d %H:%M %Z') · Mac repo $(git rev-parse --short HEAD 2>/dev/null || echo 'no git')"
echo
echo "M. This Mac"
FILES=$(sed -n '/^FILES=(/,/^)/p' bin/deploy-procurator-web.sh | sed '1d;$d' | awk '{print $1}')
missing=0; for f in $FILES; do [[ -f "$f" ]] || { echo "  FAIL M01  missing from the Mac repo: $f"; missing=1; }; done
[[ $missing == 0 ]] && echo "  PASS M01  All $(echo "$FILES" | wc -l | tr -d ' ') deploy files present"
if python3 - <<'PY'
import base64,re,sys
t=open('integrations/browser-worker/dist/install-procurator-browser-wsl.sh').read()
def blob(tag):return base64.b64decode(''.join(re.search(r"<<'%s'\n(.*?)\n%s\n"%(tag,tag),t,re.S).group(1).split()))
ok=blob('WORKER_B64')==open('integrations/browser-worker/browser_worker.py','rb').read() and blob('RUNNER_B64')==open('integrations/web-plugin/runner.cjs','rb').read()
v=re.search(r"VERSION = '([^']+)'",open('integrations/browser-worker/browser_worker.py').read()).group(1)
print(('  PASS M02  PC installer carries the current worker %s and browser driver' if ok else '  FAIL M02  PC installer is out of date (python3 integrations/browser-worker/bundle.py), worker %s')%v)
sys.exit(0 if ok else 1)
PY
then :; fi
if launchctl print "gui/$(id -u)/org.primaris.procurator-builder" >/dev/null 2>&1; then
  st=$(launchctl print "gui/$(id -u)/org.primaris.procurator-builder" | awk -F'= ' '/^\tstate/{print $2; exit}')
  if [[ "$st" == running ]]; then echo "  PASS M03  Mac builder running"; else echo "  WARN M03  Mac builder loaded but $st (bash bin/setup-builder.sh)"; fi
else echo "  WARN M03  Mac builder not loaded (bash bin/setup-builder.sh)"; fi
if ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" true 2>/dev/null; then echo "  PASS M04  Droplet reachable over SSH"; else echo "  FAIL M04  Cannot reach $HOST over SSH; nothing else can run"; return 1; fi

# sha256 of every deploy file (and the relay, which lives in bin/ on the server) for test A05
MANIFEST=$(python3 - $FILES <<'PY'
import base64,hashlib,json,sys
m={f:hashlib.sha256(open(f,'rb').read()).hexdigest() for f in sys.argv[1:]}
m['bin/build-relay.py']=hashlib.sha256(open('integrations/builder/build-relay.py','rb').read()).hexdigest()
print(base64.b64encode(json.dumps(m).encode()).decode())
PY
)
ssh "$HOST" "TEST_MODE=$MODE TEST_VISION=$VISION TEST_SECTIONS=$ONLY EXPECT_VERSION=$EXPECT MANIFEST_B64=$MANIFEST python3 -" < bin/deploy-tests.py
}

set +e
run 2>&1 | tee "$REPORT"
code=${PIPESTATUS[0]}
set -e
echo
echo "Report saved: $REPORT"
if grep -q "RELEASE BLOCKERS" "$REPORT"; then
  echo "RELEASE BLOCKER found. Roll back with the backup named in A12 if the assistant misbehaves, and send the report to Claude."
  exit 1
fi
echo "Next: the hands-on Telegram checks in the doc 'PROCURATOR 0.19.0 — Hands-on test plan'."
exit $code
