#!/usr/bin/env bash
# Browser regression set (web 0.17.0). Run on the Mac from the repo folder.
#   bash bin/web-regression.sh            offline: re-serialize every page fixture in a local headless Chrome (no network)
#   bash bin/web-regression.sh --recorded offline without Chrome (checks the saved snapshots)
#   bash bin/web-regression.sh --live     the weekly live checklist (runs from Telegram with every gate on)
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:---offline}"
if [[ "$MODE" == "--live" ]]; then exec python3 integrations/web/regression.py live; fi
if [[ "$MODE" == "--recorded" ]]; then exec python3 integrations/web/regression.py offline --recorded; fi
if [[ -z "${PROCURATOR_TEST_CHROME:-}" ]]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" /usr/bin/google-chrome /usr/bin/chromium; do [[ -x "$c" ]] && export PROCURATOR_TEST_CHROME="$c" && break; done
fi
if [[ -z "${PROCURATOR_PLAYWRIGHT_PATH:-}" ]]; then
  if [[ -d integrations/web-plugin/node_modules/playwright-core ]]; then export PROCURATOR_PLAYWRIGHT_PATH="$PWD/integrations/web-plugin/node_modules/playwright-core"
  else
    PW="$HOME/.procurator/playwright"
    if [[ ! -d "$PW/node_modules/playwright-core" ]] && command -v npm >/dev/null; then
      echo "Installing playwright-core (driver library only, no browsers) into $PW"; mkdir -p "$PW"; (cd "$PW" && npm init -y >/dev/null && npm i --silent playwright-core@1 >/dev/null)
    fi
    [[ -d "$PW/node_modules/playwright-core" ]] && export PROCURATOR_PLAYWRIGHT_PATH="$PW/node_modules/playwright-core"
  fi
fi
if [[ -z "${PROCURATOR_TEST_CHROME:-}" || -z "${PROCURATOR_PLAYWRIGHT_PATH:-}" ]] || ! command -v node >/dev/null; then
  echo "No local Chrome/Node/playwright-core here; checking the saved snapshots instead."
  exec python3 integrations/web/regression.py offline --recorded
fi
exec python3 integrations/web/regression.py offline
