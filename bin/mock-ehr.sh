#!/usr/bin/env bash
# Drive the synthetic mock EHR and pharmacy on the droplet (web 0.23.0). Run on the Mac from the repo folder.
#   bash bin/mock-ehr.sh status                          service state and FHIR bases
#   bash bin/mock-ehr.sh fault riverside '{"outage":true}'   set faults (rate_limit N, error_500 N, outage, hostile_next, partial, wrong_patient,
#                                                          book_commit_then_504, book_commit_then_drop, slot_race, refresh_lost, no_patient_context ...)
#   bash bin/mock-ehr.sh clear riverside                 clear faults
#   bash bin/mock-ehr.sh confirm northgate ap-1001       staff confirm a requested appointment
#   bash bin/mock-ehr.sh appointments riverside          list appointments (synthetic)
#   bash bin/mock-ehr.sh revoke riverside p1             patient revokes consent in the portal
#   bash bin/mock-ehr.sh refills                         synthetic pharmacy: list refill requests
#   bash bin/mock-ehr.sh advance rf-5001 ready           pharmacy staff move a refill (processing|ready|picked_up|delivered|rejected|out_of_stock|renewal_requested);
#                                                          the signed webhook goes to PROCURATOR like a real pharmacy's would
#   bash bin/mock-ehr.sh rx '{"id":"rx-1002","last_fill_date":"2026-09-01"}'   change a synthetic prescription (refills_remaining, status ...)
#   bash bin/mock-ehr.sh restart                         fresh synthetic data (dates relative to today)
# Test sign-ins: riverside owner / riverside-test (p1), maria / riverside-test (p2); northgate jordan / northgate-test (p1);
#   pharmacy corner owner / corner-test (c1). Pharmacy connection: /healthconnect mock_pharmacy "Cornerstone Pharmacy (synthetic)"
#   https://${DOMAIN}/assistant/dashboard/mock-ehr/corner/rx/v1 fake
set -euo pipefail
HOST="${PROCURATOR_HOST:?set PROCURATOR_HOST to root@<your box>}"
ADMIN='TOKEN=$(cat /var/lib/private/procurator-mock-ehr/admin-token 2>/dev/null || cat /var/lib/procurator-mock-ehr/admin-token)'
post() { ssh "$HOST" "$ADMIN; curl -s -X POST -H \"X-Admin-Token: \$TOKEN\" -H 'Content-Type: application/json' --data $(printf %q "$2") http://127.0.0.1:3351/_admin/$1; echo"; }
case "${1:-}" in
  status) ssh "$HOST" "systemctl is-active procurator-mock-ehr; journalctl -u procurator-mock-ehr -n 3 --no-pager | tail -1";;
  fault) post "$2/fault" "$3";;
  clear) post "$2/clear" '{}';;
  confirm) post "$2/confirm" "{\"appointment\":\"$3\"}";;
  appointments) post "$2/appointments" '{}';;
  revoke) post "$2/revoke_consent" "{\"patient\":\"$3\"}";;
  refills) post corner/refills '{}';;
  advance) post corner/advance "{\"refill\":\"$2\",\"status\":\"$3\"}";;
  rx) post corner/prescription "$2";;
  restart) ssh "$HOST" "systemctl restart procurator-mock-ehr && systemctl is-active procurator-mock-ehr";;
  *) sed -n 2,19p "$0";;
esac
