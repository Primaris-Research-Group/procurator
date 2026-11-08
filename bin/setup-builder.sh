#!/usr/bin/env bash
# PROCURATOR code builders (Nov 5 2026). Run on the Mac, from the procurator-deploy folder.
#   bash bin/setup-builder.sh                          set up (or update) THIS Mac as builder "mac"
#   bash bin/setup-builder.sh --enroll windows 'ssh-ed25519 AAAA…'
#                                                      register another machine's builder key (the WSL installer on the
#                                                      Windows PC prints this exact command); waits for its first check-in
#   bash bin/setup-builder.sh --uninstall [NAME]       remove a builder's droplet key (default: mac; also stops the Mac's)
#   bash bin/setup-builder.sh --enroll-browser home 'ssh-ed25519 AAAA…'
#                                                      register the home browser on the PC (web 0.19.0; the browser installer
#                                                      prints this exact command); its key can only fetch browser jobs
#   bash bin/setup-builder.sh --uninstall-browser [NAME]   remove the home browser's droplet key (default: home)
# Each builder key can ONLY run the build relay on the droplet, as that builder (no shell, no forwarding). Builders pull
# approved work; the droplet never runs anything on them. Sign-ins (Claude, ChatGPT, GitHub) stay on each machine.
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
cd "$(dirname "$0")/.."
LABEL=org.primaris.procurator-builder
BASE="$HOME/Procurator/builder"
KEY="$HOME/.ssh/procurator_builder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
[[ -f integrations/builder/worker.py && -f integrations/builder/build-relay.py ]] || { echo "Run from the procurator-deploy folder."; exit 1; }

# Install the relay and the restricted key line for builder $1 with public key $2 (uses your normal root SSH once).
install_relay() {
  local NAME="$1" PUB="$2" MODE="${3:-builder}"
  [[ "$NAME" =~ ^[a-z0-9-]{1,20}$ ]] || { echo "Builder name must be lowercase letters, digits or -"; exit 1; }
  [[ "$PUB" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]{60,80}$ ]] || { echo "That is not an ed25519 public key line (expected: ssh-ed25519 AAAA…)."; exit 1; }
  scp -q integrations/builder/build-relay.py "$HOST:/root/build-relay.py"
  ssh "$HOST" "PUB='$PUB' NAME='$NAME' MODE='$MODE' bash -s" <<'REMOTE'
set -euo pipefail
install -o root -g root -m 755 /root/build-relay.py /opt/procurator/bin/build-relay.py
rm -f /root/build-relay.py
T=$(mktemp -d); mkdir -p "$T/state"
R=/opt/procurator/bin/build-relay.py
if [ "$MODE" = browser ]; then
  if echo '{}' | PROCURATOR_LANES_DIR="$T/lanes" SSH_ORIGINAL_COMMAND=bhello python3 $R --browser "$NAME" | grep -q '"ok":true' \
     && ! SSH_ORIGINAL_COMMAND='id' PROCURATOR_LANES_DIR="$T/lanes" python3 $R --browser "$NAME" </dev/null >/dev/null \
     && ! SSH_ORIGINAL_COMMAND='pull' PROCURATOR_LANES_DIR="$T/lanes" python3 $R --browser "$NAME" </dev/null >/dev/null; then echo "  relay self-test (browser mode) OK"; else echo "  relay self-test FAILED"; rm -rf "$T"; exit 1; fi
  install -d -o root -g root -m 700 /var/lib/procurator-web/lanes
elif echo '{"repos":["a/b"]}' | PROCURATOR_BUILD_DIR="$T/state/procurator-build" SSH_ORIGINAL_COMMAND=hello python3 $R --builder "$NAME" | grep -q '"ok":true' \
   && ! SSH_ORIGINAL_COMMAND='id' PROCURATOR_BUILD_DIR="$T/state/procurator-build" python3 $R --builder "$NAME" </dev/null >/dev/null; then echo "  relay self-test OK"; else echo "  relay self-test FAILED"; rm -rf "$T"; exit 1; fi
rm -rf "$T"
# Build folder shared with the gateway container: owned like the assistant's other state folders.
S=/opt/procurator/state
REF=$S/procurator-voice; [ -d "$REF" ] || REF=$S
U=$(stat -c %u "$REF"); G=$(stat -c %g "$REF")
for d in "" /queue /cancel /claims /reports /builders; do install -d -o "$U" -g "$G" -m 700 "$S/procurator-build$d"; done
python3 - <<'PY'
import os,shutil,time
pub,name,mode=os.environ['PUB'].strip(),os.environ['NAME'],os.environ.get('MODE','builder')
tag=' procurator-'+mode+'-'+name
p='/root/.ssh/authorized_keys';lines=open(p).read().splitlines()
others=[l for l in lines if not l.rstrip().endswith(tag)]
real=[l for l in others if l.strip() and not l.lstrip().startswith('#') and 'procurator-builder' not in l and 'procurator-browser' not in l]
if not real: raise SystemExit('authorized_keys has no normal login key; refusing to edit it (you could be locked out). Nothing changed.')
line='restrict,command="/usr/bin/python3 /opt/procurator/bin/build-relay.py --%s %s" %s%s'%(mode,name,pub,tag)
if line in lines and len(lines)-len(others)==1: print('  key for %s "%s" already installed'%(mode,name)); raise SystemExit(0)
shutil.copy2(p,p+'.before-builder-'+time.strftime('%Y%m%d-%H%M%S'))
st=os.stat(p);tmp=p+'.tmp';open(tmp,'w').write('\n'.join(others+[line])+'\n');os.chmod(tmp,0o600);os.chown(tmp,st.st_uid,st.st_gid);os.replace(tmp,p)
print('  key for %s "%s" installed: it can only run the relay in %s mode (your %d login key(s) unchanged)'%(mode,name,mode,len(real)))
PY
REMOTE
}

if [[ "${1:-}" == "--uninstall" ]]; then
  NAME="${2:-mac}"
  [[ "$NAME" =~ ^[a-z0-9-]{1,20}$ ]] || { echo "Bad builder name"; exit 1; }
  if [[ "$NAME" == mac && "$(uname)" == Darwin ]]; then launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "Mac builder stopped."; fi
  ssh "$HOST" "NAME='$NAME' python3 -" <<'PY' || echo "Could not reach the droplet; remove the line ending in procurator-builder-$NAME from /root/.ssh/authorized_keys yourself."
import os,shutil,time
tag=' procurator-builder-'+os.environ['NAME']
p='/root/.ssh/authorized_keys';lines=open(p).read().splitlines()
keep=[l for l in lines if not l.rstrip().endswith(tag)]
if len(keep)!=len(lines):
    shutil.copy2(p,p+'.before-builder-removal-'+time.strftime('%Y%m%d-%H%M%S'))
    st=os.stat(p);tmp=p+'.tmp';open(tmp,'w').write('\n'.join(keep)+'\n');os.chmod(tmp,0o600);os.chown(tmp,st.st_uid,st.st_gid);os.replace(tmp,p)
f='/opt/procurator/state/procurator-build/builders/%s.json'%os.environ['NAME']
if os.path.exists(f): os.remove(f)
print('droplet: builder key removed' if len(keep)!=len(lines) else 'droplet: no key for that builder')
PY
  exit 0
fi

if [[ "${1:-}" == "--uninstall-browser" ]]; then
  NAME="${2:-home}"
  [[ "$NAME" =~ ^[a-z0-9-]{1,20}$ ]] || { echo "Bad machine name"; exit 1; }
  ssh "$HOST" "NAME='$NAME' python3 -" <<'PY'
import os,shutil,time
tag=' procurator-browser-'+os.environ['NAME']
p='/root/.ssh/authorized_keys';lines=open(p).read().splitlines()
keep=[l for l in lines if not l.rstrip().endswith(tag)]
if len(keep)!=len(lines):
    shutil.copy2(p,p+'.before-browser-removal-'+time.strftime('%Y%m%d-%H%M%S'))
    st=os.stat(p);tmp=p+'.tmp';open(tmp,'w').write('\n'.join(keep)+'\n');os.chmod(tmp,0o600);os.chown(tmp,st.st_uid,st.st_gid);os.replace(tmp,p)
f='/var/lib/procurator-web/lanes/%s/hello.json'%os.environ['NAME']
if os.path.exists(f): os.remove(f)
print('droplet: home browser key removed' if len(keep)!=len(lines) else 'droplet: no key for that machine')
PY
  exit 0
fi

if [[ "${1:-}" == "--enroll-browser" ]]; then
  NAME="${2:-}"; PUB=$(echo "${3:-}" | awk '{print $1" "$2}')
  [[ -n "$NAME" && -n "$PUB" ]] || { echo "Usage: bash bin/setup-builder.sh --enroll-browser home 'ssh-ed25519 AAAA…'   (the home browser installer prints this line)"; exit 1; }
  echo "Registering home browser \"$NAME\" on the droplet"
  install_relay "$NAME" "$PUB" browser
  echo "Waiting for $NAME to check in (up to 2 minutes)…"
  for i in $(seq 1 12); do
    OUT=$(ssh "$HOST" "python3 -c \"import json,time;b=json.load(open('/var/lib/procurator-web/lanes/$NAME/hello.json'));print('%s checked in %d s ago · %s · profiles: %s'%(b.get('label'),(time.time()*1000-b['seen_at'])/1000,b.get('chrome') or 'Chrome ?',', '.join(p['name'] for p in b['profiles']) or 'none yet'))\" 2>/dev/null" || true)
    if [[ -n "$OUT" ]]; then echo "  $OUT"; echo "Done. In Telegram: /weblane to see the lanes; /homesignin SITE then /homeready SITE to sign a site in at the PC."; exit 0; fi
    sleep 10
  done
  echo "  No check-in yet. On the PC (Ubuntu window) run: python3 ~/Procurator/browser/browser_worker.py check"
  exit 0
fi

if [[ "${1:-}" == "--enroll" ]]; then
  NAME="${2:-}"; PUB=$(echo "${3:-}" | awk '{print $1" "$2}')
  [[ -n "$NAME" && -n "$PUB" && "$NAME" != mac ]] || { echo "Usage: bash bin/setup-builder.sh --enroll windows 'ssh-ed25519 AAAA…'   (the WSL installer prints this line)"; exit 1; }
  echo "Registering builder \"$NAME\" on the droplet"
  install_relay "$NAME" "$PUB"
  echo "Waiting for $NAME to check in (it retries every 30 s; up to 2 minutes)…"
  for i in $(seq 1 12); do
    OUT=$(ssh "$HOST" "python3 -c \"import json,time;b=json.load(open('/opt/procurator/state/procurator-build/builders/$NAME.json'));print('%s checked in %d s ago · repos: %s · agents: %s'%(b.get('label'),(time.time()*1000-b['seen_at'])/1000,', '.join(b['repos']) or 'none yet',', '.join(k for k,v in b['agents'].items() if v) or 'none'))\" 2>/dev/null" || true)
    if [[ -n "$OUT" ]]; then echo "  $OUT"; echo "Done. Allow repos on that machine with: python3 ~/Procurator/builder/worker.py add-repo OWNER/NAME --test \"npm test\""; exit 0; fi
    sleep 10
  done
  echo "  No check-in yet. On the Windows PC (Ubuntu window) run: python3 ~/Procurator/builder/worker.py check"
  exit 0
fi

[[ "$(uname)" == Darwin ]] || { echo "Run this on your Mac (Windows PC: use integrations/builder/dist/install-procurator-builder-wsl.sh inside Ubuntu)."; exit 1; }
echo "1/5 Checking this Mac"
PY=$(command -v python3 || true)
[[ -n "$PY" ]] && "$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)' || { echo "python3 3.8+ not found. Install the Command Line Tools: xcode-select --install"; exit 1; }
for t in git gh; do command -v "$t" >/dev/null || { echo "$t is not installed. Install it (Homebrew: brew install $t), then run this again."; exit 1; }; done
gh auth status >/dev/null 2>&1 || { echo "GitHub CLI is not signed in. Run: gh auth login   (choose GitHub.com, HTTPS, and sign in in the browser), then run this again."; exit 1; }
CLAUDE=$(command -v claude || true); CODEX=$(command -v codex || true)
[[ -n "$CLAUDE$CODEX" ]] || { echo "Neither Claude Code (claude) nor Codex (codex) is installed. Install at least one and sign in with your subscription, then run this again."; exit 1; }
git config --global user.email >/dev/null || { echo "Set your git identity first:  git config --global user.name \"Your Name\"  and  git config --global user.email you@example.com"; exit 1; }
echo "  python3 $("$PY" -c 'import platform;print(platform.python_version())') · git · gh (signed in) · Claude Code: ${CLAUDE:-not installed} · Codex: ${CODEX:-not installed}"
[[ -n "$CLAUDE" ]] && { "$CLAUDE" --version >/dev/null 2>&1 && echo "  Claude Code runs (sign-in is checked on the first build)" || echo "  WARNING: claude is installed but did not run; open a terminal, run claude once and sign in."; }
[[ -n "$CODEX" ]] && { "$CODEX" --version >/dev/null 2>&1 && echo "  Codex runs (sign-in is checked on the first build)" || echo "  WARNING: codex is installed but did not run; run codex once and sign in with ChatGPT."; }

echo "2/5 Builder key (it can only talk to the build relay on the droplet)"
mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
[[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N '' -C procurator-builder-mac -f "$KEY"
chmod 600 "$KEY"
PUB=$(awk '{print $1" "$2}' "$KEY.pub")

echo "3/5 Installing the build relay on the droplet (uses your normal SSH access once)"
install_relay mac "$PUB"
SSHOPTS=(-F /dev/null -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15)
OUT=$(echo '{"label":"Mac"}' | ssh "${SSHOPTS[@]}" "$HOST" hello 2>&1 || true)
[[ "$OUT" == *'"ok":true'* ]] && echo "  builder key works" || { echo "  builder key test FAILED: $OUT"; exit 1; }
OUT=$(ssh "${SSHOPTS[@]}" "$HOST" 'id' </dev/null 2>&1 || true)
[[ "$OUT" == *'unknown command'* ]] && echo "  builder key is restricted (a shell command was refused)" || { echo "  WARNING: restriction check unexpected: $OUT"; exit 1; }

echo "4/5 Installing the worker in $BASE"
mkdir -p "$BASE/logs" "$HOME/Procurator/builds"; chmod 700 "$HOME/Procurator" "$BASE"
install -m 700 integrations/builder/worker.py "$BASE/worker.py"
BUILDER_PATH="$PATH" HOSTARG="$HOST" KEYARG="$KEY" CLAUDE="$CLAUDE" CODEX="$CODEX" "$PY" - "$BASE/config.json" <<'PY'
import json,os,shutil,sys
p=sys.argv[1]
c=json.load(open(p)) if os.path.exists(p) else {}
c['relay']=['ssh','-F','/dev/null','-i',os.environ['KEYARG'],'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','StrictHostKeyChecking=yes',os.environ['HOSTARG']]
c['path']=os.environ['BUILDER_PATH'];c['name']='mac';c.setdefault('label','Mac')
c['tools']={'git':shutil.which('git'),'gh':shutil.which('gh'),'claude':os.environ['CLAUDE'] or 'claude','codex':os.environ['CODEX'] or 'codex'}
c.setdefault('default_agent','claude' if os.environ['CLAUDE'] else 'codex')
c.setdefault('repos',{})
tmp=p+'.tmp';json.dump(c,open(tmp,'w'),indent=2);os.chmod(tmp,0o600);os.replace(tmp,p)
print('  config: %s (allowed repos: %s)'%(p,', '.join(c['repos']) or 'none yet'))
PY

echo "5/5 Starting the builder (LaunchAgent $LABEL)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$BASE/worker.py</string><string>run</string></array>
  <key>WorkingDirectory</key><string>$BASE</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$BASE/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$BASE/logs/launchd.log</string>
</dict></plist>
PLIST
# Nov 6: bootout is asynchronous; bootstrapping before the old agent is gone fails with "5: Input/output error".
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for i in $(seq 1 20); do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
STARTED=0
for attempt in 1 2 3; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then STARTED=1; break; fi
  sleep 2
done
if [[ $STARTED == 0 ]]; then
  # Older launchd path; works when bootstrap keeps refusing an agent that was loaded the old way.
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST" && STARTED=1 || true
fi
[[ $STARTED == 1 ]] && echo "  LaunchAgent loaded" || { echo "  Could not load the LaunchAgent. Run: launchctl bootout gui/$(id -u)/$LABEL; then bash bin/setup-builder.sh again (or log out and back in)."; }
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 5
"$PY" "$BASE/worker.py" check || true
echo
echo "Done. Next:"
echo "  1. Allow a repo on this Mac:  python3 ~/Procurator/builder/worker.py add-repo OWNER/NAME --test \"npm test\""
echo "  2. Windows PC: run integrations/builder/dist/install-procurator-builder-wsl.sh inside Ubuntu (WSL); it prints the --enroll command to paste here."
echo "  3. Deploy the gateway side once: PROCURATOR_SKIP_CALL_CHECK=1 bash bin/deploy-procurator-web.sh 2>&1 | tee deploy-web.txt"
echo "Logs: ~/Procurator/builder/logs/worker.log · Status: python3 ~/Procurator/builder/worker.py check · Remove: bash bin/setup-builder.sh --uninstall"
