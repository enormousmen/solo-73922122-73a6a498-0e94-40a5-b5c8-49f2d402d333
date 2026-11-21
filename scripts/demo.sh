#!/usr/bin/env bash
# End-to-end demo against a running lease API.
#
#   docker compose up -d --build
#   ./scripts/demo.sh                  # defaults to http://127.0.0.1:8000
#   BASE=http://host:8000 ./scripts/demo.sh
#
# Shows two VLANs (office=10, guest=20) using the *same* IPv4 pool
# (10.20.0.0/24) with overlapping client ids and addresses.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
TOKEN="${OPERATOR_TOKEN:-}"
AUTH=()
if [[ -n "$TOKEN" ]]; then
  AUTH=(-H "X-Operator-Token: $TOKEN")
fi

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
api() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' "${AUTH[@]}" -d "$body"
  else
    curl -sS -X "$method" "$BASE$path" "${AUTH[@]}"
  fi
}
pretty() { python3 -m json.tool; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin)[\"$1\"])"; }

say "Health (legacy/no-VLAN scope)"
api GET /health | pretty

say "Configure VLAN 10 (office) with 10.20.0.0/24"
api PUT '/v1/pool?vlan=10' '{"cidr":"10.20.0.0/24"}' | pretty

say "Configure VLAN 20 (guest) with the SAME 10.20.0.0/24"
api PUT '/v1/pool?vlan=20' '{"cidr":"10.20.0.0/24"}' | pretty

say "Both VLANs now hold the same overlapping pool"
api GET /v1/pools | pretty

say "Same device id 'laptop-01' acquires in both VLANs"
OFFICE=$(api POST '/v1/leases?vlan=10' '{"client_id":"laptop-01"}')
echo "$OFFICE" | pretty
GUEST=$(api POST '/v1/leases?vlan=20' '{"client_id":"laptop-01"}')
echo "$GUEST" | pretty
OFFICE_ID=$(field lease_id <<<"$OFFICE")
GUEST_ID=$(field lease_id <<<"$GUEST")

say "They got the same address independently; usage is counted per VLAN"
api GET '/v1/pool?vlan=10' | pretty
api GET '/v1/pool?vlan=20' | pretty

say "Another office client takes 10.20.0.1 -> 10.20.0.2; guest is unaffected"
api POST '/v1/leases?vlan=10' '{"client_id":"printer-02"}' | pretty
api POST '/v1/leases?vlan=20' '{"client_id":"phone-03"}' | pretty

say "Office renews its lease"
api POST "/v1/leases/$OFFICE_ID/renew?vlan=10" '{"ttl_seconds":7200}' | pretty

say "Cross-VLAN access is rejected: guest cannot see the office lease id"
api GET "/v1/leases/$OFFICE_ID?vlan=20" | pretty
api POST "/v1/leases/$OFFICE_ID/renew?vlan=20" '{}' | pretty

say "An unconfigured VLAN is an error (30 has no pool)"
api GET '/v1/pool?vlan=30' | pretty
api POST '/v1/leases?vlan=30' '{"client_id":"nobody"}' | pretty

say "Office switches its pool to 10.30.0.0/27; guest keeps 10.20.0.0/24"
api PUT '/v1/pool?vlan=10' '{"cidr":"10.30.0.0/27"}' | pretty
api GET '/v1/pool?vlan=10' | pretty
api GET '/v1/pool?vlan=20' | pretty

say "Office out-of-pool leases (with expiry); guest has none"
api GET '/v1/admin/out-of-pool?vlan=10' | pretty
api GET '/v1/admin/out-of-pool?vlan=20' | pretty

say "A new office client gets a fresh address from the new pool"
api POST '/v1/leases?vlan=10' '{"client_id":"phone-04"}' | pretty

say "Office releases its out-of-pool address -> no longer occupied there"
api POST "/v1/leases/$OFFICE_ID/release?vlan=10" | pretty
api GET '/v1/admin/out-of-pool?vlan=10' | pretty

say "Invalid pool is rejected; that VLAN keeps serving its old pool"
api PUT '/v1/pool?vlan=10' '{"cidr":"10.30.0.0/40"}' | pretty
api GET '/v1/pool?vlan=10' | pretty

say "Guest lease from the very first step is still active on its own pool"
api GET "/v1/leases/$GUEST_ID?vlan=20" | pretty

say "VLAN 0 (no-VLAN requests) still has its own pool"
api GET /v1/pool | pretty

say "Done. All VLAN pools and leases persist in the 'lease-data' volume."
