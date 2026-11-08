#!/usr/bin/env bash
# Store the credential of a health connection on the SERVER only (web 0.21.0, consumer health module). Run on the Mac:
#   bash bin/set-health-token.sh CONNECTION_ID            (paste the token JSON or a bearer token when asked; hidden; nothing stays on this Mac)
#   bash bin/set-health-token.sh --status CONNECTION_ID   (connected / pending, token present yes/no; never the token)
#   bash bin/set-health-token.sh --revoke CONNECTION_ID   (deletes the token file and revokes the connection's grants)
#   bash bin/set-health-token.sh --fdc                    (0.23.0: USDA FoodData Central API key for food search; hidden; server only)
# Tokens live in /var/lib/procurator-web/health-tokens/<ref>.json (mode 600). The assistant never sees them: tool results carry a
# token *reference* only. Connection IDs come from /healthconnect in Telegram.
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
PY=/opt/procurator/integrations/web/health_cli.py
case "${1:-}" in
  ""|-h|--help) sed -n 2,8p "$0"; exit 0;;
  --fdc) read -r -s -p "FoodData Central API key (from fdc.nal.usda.gov/api-key-signup; hidden): " KEY; echo
         [[ "$KEY" =~ ^[A-Za-z0-9]{20,64}$ ]] || { echo "That does not look like a FoodData Central key; nothing saved."; exit 1; }
         printf '%s\n' "$KEY" | ssh "$HOST" 'D=/var/lib/procurator-web/health-tokens; umask 077; cat > $D/fdc-api-key && chown --reference=$D $D/fdc-api-key && chmod 600 $D/fdc-api-key && systemctl restart procurator-web; echo "Saved on the server; the broker restarted to pick it up."'
         unset KEY; exit 0;;
  --status) [[ -n "${2:-}" ]] || { echo "Use --status CONNECTION_ID"; exit 1; }; ssh "$HOST" "python3 $PY status $(printf %q "$2")"; exit 0;;
  --revoke) [[ -n "${2:-}" ]] || { echo "Use --revoke CONNECTION_ID"; exit 1; }; ssh "$HOST" "python3 $PY revoke $(printf %q "$2")"; exit 0;;
esac
CID="$1"
[[ "$CID" =~ ^[a-f0-9]{16}$ ]] || { echo "Connection IDs are 16 hex characters (see /healthconnect list)."; exit 1; }
read -r -s -p "Token for connection $CID (JSON like {\"access_token\":...,\"refresh_token\":...} or a bare token; hidden): " VALUE; echo
[[ -n "$VALUE" ]] || { echo "Nothing typed; nothing saved."; exit 1; }
printf '%s\n' "$VALUE" | ssh "$HOST" "python3 $PY set $(printf %q "$CID")"
unset VALUE
