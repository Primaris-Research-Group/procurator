#!/usr/bin/env bash
# Store a data-API key for the assistant on the droplet, without it ever passing through chat (Nov 5).
# Usage (from the repo folder):  bash bin/set-api-key.sh finnhub   or   bash bin/set-api-key.sh youtube
#   finnhub — free key from https://finnhub.io (backup market quotes when Yahoo rate-limits)
#   youtube — free YouTube Data API v3 key from https://console.cloud.google.com (youtube_search)
# The key is typed here (hidden), sent over SSH and saved readable only by the assistant. Run again to replace it.
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
NAME="${1:-}"
case "$NAME" in finnhub|youtube) ;; *) echo "Use: bash bin/set-api-key.sh finnhub|youtube"; exit 1;; esac
read -r -s -p "Paste the $NAME API key (hidden), then press Enter: " KEY; echo
[[ "$KEY" =~ ^[A-Za-z0-9_.-]{8,200}$ ]] || { echo "That does not look like an API key; nothing saved."; exit 1; }
printf '%s' "$KEY" | ssh "$HOST" "set -euo pipefail; D=/opt/procurator/state/procurator-admin/keys; mkdir -p \$D; chown 1000:1000 \$D; chmod 700 \$D;
  umask 077; cat > \$D/$NAME.key.tmp; chown 1000:1000 \$D/$NAME.key.tmp; chmod 600 \$D/$NAME.key.tmp; mv \$D/$NAME.key.tmp \$D/$NAME.key;
  docker exec procurator-openclaw-gateway-1 test -r /home/node/.openclaw/procurator-admin/keys/$NAME.key && echo 'Saved; the assistant can read it (no restart needed).' || echo 'Saved, but the assistant cannot read it yet; send this output to Claude.'"
unset KEY
