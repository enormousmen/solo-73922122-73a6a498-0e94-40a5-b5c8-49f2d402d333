# Branch Gateway — VLAN-Scoped IPv4 Lease API

FastAPI + SQLite lease broker for a branch gateway. Each **VLAN** (1–4094)
has its own single active pool and its own lease namespace. Office and guest
VLANs can therefore share one gateway even when they use the *same* IPv4
range and the same client/device ids: every address is allocated and counted
independently per VLAN.

Requests that omit a VLAN id keep the original single-pool behaviour under
an internal legacy scope; that scope is also where a pre-VLAN database is
migrated to on first start.

## Run it

```bash
docker compose up -d --build
curl -s localhost:8000/health
./scripts/demo.sh          # two VLANs with overlapping CIDRs, end to end
```

Interactive docs: <http://localhost:8000/docs>.

Local (no Docker):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload
```

Configuration (env vars, see `docker-compose.yml`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DB_PATH` | `./data/leases.db` | SQLite file (compose: `/data/leases.db` on a volume) |
| `DEFAULT_POOL_CIDR` | `10.20.0.0/24` | Seeds VLAN 0 (no-VLAN requests) on a **fresh** database only |
| `DEFAULT_TTL_SECONDS` | `3600` | Lease lifetime when the request omits one |
| `MIN_TTL_SECONDS` / `MAX_TTL_SECONDS` | `60` / `604800` | Accepted `ttl_seconds` range |
| `OPERATOR_TOKEN` | empty | When set, operator endpoints require `X-Operator-Token` |

## API

VLAN ids are accepted as an optional query parameter (`?vlan=10`) or, for
bodies that have one, as an optional `"vlan"` field. Passing both is allowed
only when they agree. Omitting the VLAN uses the legacy scope.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness, reports the legacy pool |
| `GET` | `/v1/pools` | Every configured VLAN with its pool and counters |
| `GET` | `/v1/pool?vlan=10` | A VLAN's pool, capacity and usage counters |
| `PUT` | `/v1/pool?vlan=10` | Configure (first time) or switch a VLAN's pool, body `{"cidr": "10.30.0.0/27"}` (operator) |
| `POST` | `/v1/leases?vlan=10` | Acquire, body `{"client_id": "...", "ttl_seconds": 3600?}` |
| `GET` | `/v1/leases/{lease_id}?vlan=10` | Inspect a lease in its VLAN |
| `POST` | `/v1/leases/{lease_id}/renew?vlan=10` | Renew an active in-pool lease of that VLAN |
| `POST` | `/v1/leases/{lease_id}/release?vlan=10` | Release an active lease of that VLAN |
| `GET` | `/v1/admin/out-of-pool?vlan=10` | Still-occupied addresses outside that VLAN's pool (operator) |

Lease responses include `"vlan": 10`, or `"vlan": null` for the legacy
scope. Errors are structured: `{"error": {"code": "...", "message": "..."}}`
with codes such as `pool_exhausted`, `lease_not_found`, `lease_released`,
`lease_expired`, `address_out_of_pool`, `invalid_pool`, `invalid_ttl`,
`invalid_vlan`, `vlan_not_configured`, `unauthorized`.

## Semantics

**VLAN configuration.** A VLAN is created by its first successful
`PUT /v1/pool?vlan=N` (operator-authenticated exactly like any other pool
write). Every other operation on a VLAN without a pool fails with
`404 vlan_not_configured`; nothing is implicitly provisioned. VLAN ids
outside 1–4094 are rejected with `422 invalid_vlan` (query/body range
violations surface as `422 invalid_request` from schema validation).

**Isolation.** Each VLAN has exactly one pool. Its leases, `client_id`
uniqueness, address occupancy, usage counters, out-of-pool report and pool
history are all scoped to that VLAN. The same IPv4 address and the same
`client_id` may be active in two VLANs at once. A lease id only resolves
inside the VLAN stored on the lease; lookups, renewals and releases from
any other VLAN (including the no-VLAN scope) return `lease_not_found`.

**Acquire** allocates the lowest free address of the selected VLAN's current
pool (network and broadcast excluded; a /31 has two usable addresses).
Re-acquiring with the same `client_id` in the same VLAN returns that
client's unexpired lease untouched with HTTP 200 (new leases are 201 +
`Location`), even if the address is currently outside the pool.

**Renew / release** are addressed by the current lease id plus its VLAN.
Release works for in-pool and out-of-pool addresses; renew is refused for an
address outside the VLAN's current pool (`409 address_out_of_pool`).
Repeating a release is a successful no-op (`already_released: true`).

**Pool switch** is a single transaction scoped to one VLAN. Invalid CIDRs
are rejected with 422 and the previous pool keeps serving; any failure
inside the transaction rolls back, so the old pool and all leases stay
authoritative. Switching to the same pool is a no-op (`changed: false`).
Existing leases are *not* deleted by a switch: addresses that fell out stay
occupied until they expire or are released. Configuring a brand-new VLAN
reports `configured: true`; later switches report `configured: false` and
the `previous_cidr`.

**Expiry** is global (a clock event, not a pool event): leases whose
deadline has passed are swept at the start of every write transaction and
read as `expired` even before the sweep. Expired rows stay for auditing.

**Restart** restores every VLAN's pool, all leases (with VLAN, lease id and
expiry unchanged) and the per-VLAN switch history.

### Upgrading an existing database

On startup a pre-VLAN database is migrated in place: the `leases` and
`pool_changes` tables gain a `vlan` column defaulting to 0 (the legacy
scope), the old global unique indexes are replaced by VLAN-scoped ones, and
`settings.current_pool` is copied into the `pools` table as VLAN 0's pool.
Lease ids, addresses, issue/expiry timestamps and released rows are not
touched. Requests without a VLAN id continue to see the old pool and the old
leases. The migration is idempotent and only affects files lacking the
`vlan` column.

## Concurrency

Every mutating operation runs in `BEGIN IMMEDIATE`, i.e. under SQLite's
single write lock; reads run against WAL snapshots. A pool switch (for one
VLAN) is one transaction, so a switch racing with acquire/renew/release in
that VLAN has the same result as one of the two serial orders. VLANs share
the SQLite writer lock, so writes to different VLANs are serialised rather
than interleaved; this preserves all invariants at the cost of write
throughput. `tests/test_concurrency.py` asserts the serial-order equivalence
and address uniqueness.

## Tests

```bash
python3 -m pytest -q
```

- `tests/test_subnets.py` — CIDR parsing, usable ranges, lowest-free search
- `tests/test_store.py` — allocation, idempotency, renew/release rules, expiry,
  pool moves, restart recovery
- `tests/test_vlans.py` — overlapping VLANs, per-VLAN pools/leases/counters,
  cross-VLAN lease-id rejection, unconfigured-VLAN errors, first-config vs
  switch, failed-switch rollback, restart, v1-database upgrade
- `tests/test_api.py` — HTTP status codes, structured errors, operator auth
- `tests/test_concurrency.py` — switch-vs-operation races and address uniqueness
