#!/usr/bin/env bash
# Keys for the browser service's read-only adapters (web 0.18.0). Run on the Mac from the repo folder:
#   bash bin/set-web-key.sh duffel      paste a Duffel access token (flight search, read-only offers)
#   bash bin/set-web-key.sh travelpayouts  paste a free Travelpayouts API token (fare calendar: cheapest months/days, nonstops)
#   bash bin/set-web-key.sh simplefin   paste a SimpleFIN Bridge SETUP token; it is claimed once on the server and only
#                                       the resulting read-only access URL is kept
#   bash bin/set-web-key.sh openai      paste an OpenAI API key for the browser broker (0.24.1: Packet Composer on GPT
#                                       models, page-vision fallback); the gateway's own key is separate
#   bash bin/set-web-key.sh places      paste a Google Places API (New) key (0.25.0: places_search listings with phone and
#                                       hours; without it places_search uses OpenStreetMap)
#   bash bin/set-web-key.sh status      which keys are set
# Stored on the server in /var/lib/procurator-web/keys/ (mode 600). Nothing is kept on this Mac.
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
case "${1:-}" in
  status) ssh "$HOST" 'for k in duffel travelpayouts simplefin openai places; do printf "%s: " $k; test -s /var/lib/procurator-web/keys/$k.key && echo set || echo "not set"; done'; exit 0;;
  duffel|simplefin|travelpayouts|openai|places) ;;
  *) sed -n 2,10p "$0"; exit 1;;
esac
read -r -s -p "Paste the $1 ${1/simplefin/setup} token (hidden): " SECRET; echo
[[ -n "$SECRET" ]] || { echo "Nothing pasted."; exit 1; }
printf '%s\n' "$SECRET" | ssh "$HOST" "NAME=$1 python3 -c '
import base64,os,sys,urllib.request
name=os.environ[\"NAME\"];v=sys.stdin.readline().strip()
if name==\"openai\" and not v.startswith(\"sk-\"):sys.exit(\"That does not look like an OpenAI API key (sk-...). Nothing saved.\")
if name==\"simplefin\":
    claim=base64.b64decode(v).decode().strip()
    if not claim.startswith(\"https://\"):sys.exit(\"That is not a SimpleFIN setup token.\")
    with urllib.request.urlopen(urllib.request.Request(claim,data=b\"\",method=\"POST\"),timeout=30) as r:v=r.read().decode().strip()
    if not v.startswith(\"https://\"):sys.exit(\"SimpleFIN did not return an access URL.\")
d=\"/var/lib/procurator-web/keys\";os.makedirs(d,mode=0o700,exist_ok=True)
p=os.path.join(d,name+\".key\");fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
os.write(fd,(v+\"\\n\").encode());os.close(fd);print(name+\" key saved on the server (hidden).\")
'"
unset SECRET
