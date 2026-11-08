#!/usr/bin/env bash
# Save an OAuth client (id + secret) for one provider on the box, without either value passing through chat (web 0.29.0).
# Usage (from the repo folder on the Mac):  bash bin/set-oauth-client.sh google
#   google — a "Web application" OAuth client from https://console.cloud.google.com/auth/clients with the redirect URI
#            https://<your domain>/assistant/dashboard/oauth/callback ; publish the consent screen to PRODUCTION (Testing mode
#            expires refresh tokens after 7 days). Scopes PROCURATOR asks for are sensitive only (calendar, contacts.readonly,
#            tasks, gmail.send, drive.file) — no restricted Gmail scope, so no CASA audit; until Google verifies the client the
#            consent screen shows an "unverified app" warning and the client is capped at 100 users.
#   When PROCURATOR is distributed, this is where the shared PROCURATOR client goes; a user who prefers their own client pastes theirs.
# Both values are typed here (hidden), sent over SSH and saved readable only by the broker. Run again to replace them.
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
SECRETS="${PROCURATOR_SECRETS_REMOTE:-/var/lib/procurator-web/secrets}"
NAME="${1:-}"
case "$NAME" in google|microsoft|linkedin|meta|tiktok|slack) ;; *) echo "Use: bash bin/set-oauth-client.sh google|microsoft|linkedin|meta|tiktok|slack"; exit 1;; esac
read -r -p "Client ID for $NAME: " CID
[[ "$CID" =~ ^[A-Za-z0-9_.:@/-]{8,300}$ ]] || { echo "That does not look like a client id; nothing saved."; exit 1; }
read -r -s -p "Client secret for $NAME (hidden), then Enter: " CSEC; echo
[[ "$CSEC" =~ ^[A-Za-z0-9_.~-]{8,300}$ ]] || { echo "That does not look like a client secret; nothing saved."; exit 1; }
python3 -c 'import json,sys;print(json.dumps({"client_id":sys.argv[1],"client_secret":sys.argv[2]}))' "$CID" "$CSEC" | ssh "$HOST" "set -euo pipefail; D=$SECRETS/oauth; mkdir -p \$D; chmod 700 $SECRETS \$D;
  umask 077; cat > \$D/$NAME-client.json.tmp; chmod 600 \$D/$NAME-client.json.tmp; mv \$D/$NAME-client.json.tmp \$D/$NAME-client.json;
  echo 'Saved. Settings > Connections now shows $NAME as ready (no restart needed).'"
unset CID CSEC
