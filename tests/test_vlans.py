"""Multi-VLAN behaviour: isolated pools, scoped leases, upgrade compatibility."""

from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.db import LEGACY_VLAN, LeaseError, Store

OFFICE = 10
GUEST = 20


def configure(store: Store, vlan: int, cidr: str) -> None:
    store.switch_pool(cidr, vlan)


# ---------------------------------------------------------------- isolation


def test_two_vlans_share_the_same_cidr_and_addresses(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.20.0.0/29")

    office, reused = store.acquire("laptop", vlan=OFFICE)
    guest, reused2 = store.acquire("laptop", vlan=GUEST)

    assert reused is False and reused2 is False
    assert office["ip"] == guest["ip"] == "10.20.0.1"
    assert office["vlan"] == OFFICE and guest["vlan"] == GUEST
    assert office["lease_id"] != guest["lease_id"]

    # Each VLAN's second client gets the next address independently.
    office2, _ = store.acquire("printer", vlan=OFFICE)
    guest2, _ = store.acquire("phone", vlan=GUEST)
    assert office2["ip"] == guest2["ip"] == "10.20.0.2"


def test_usage_is_counted_per_vlan(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.20.0.0/29")
    store.acquire("a", vlan=OFFICE)
    store.acquire("b", vlan=OFFICE)
    store.acquire("c", vlan=GUEST)

    office = store.pool_info(OFFICE)
    guest = store.pool_info(GUEST)
    assert (office["allocated_in_pool"], office["free_in_pool"]) == (2, 4)
    assert (guest["allocated_in_pool"], guest["free_in_pool"]) == (1, 5)


def test_pool_switch_is_per_vlan(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.20.0.0/29")
    store.acquire("a", vlan=OFFICE)

    result = store.switch_pool("10.99.0.0/24", GUEST)
    assert result["changed"] is True
    assert store.pool_info(OFFICE)["cidr"] == "10.20.0.0/29"
    assert store.pool_info(GUEST)["cidr"] == "10.99.0.0/24"

    # Office still allocates from its own range; guest starts afresh.
    office_lease, _ = store.acquire("b", vlan=OFFICE)
    guest_lease, _ = store.acquire("b", vlan=GUEST)
    assert office_lease["ip"] == "10.20.0.2"
    assert guest_lease["ip"] == "10.99.0.1"


def test_out_of_pool_report_is_per_vlan(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.20.0.0/29")
    office_lease, _ = store.acquire("a", vlan=OFFICE)
    store.acquire("b", vlan=GUEST)

    store.switch_pool("10.99.0.0/24", OFFICE)
    assert [item["lease_id"] for item in store.out_of_pool_leases(OFFICE)] == [
        office_lease["lease_id"]
    ]
    assert store.out_of_pool_leases(GUEST) == []


def test_renew_and_release_are_scoped_by_vlan(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.20.0.0/29")
    lease, _ = store.acquire("a", vlan=OFFICE)

    with pytest.raises(LeaseError) as exc:
        store.get_lease(lease["lease_id"], GUEST)
    assert exc.value.code == "lease_not_found"
    with pytest.raises(LeaseError):
        store.renew(lease["lease_id"], vlan=GUEST)
    with pytest.raises(LeaseError):
        store.release(lease["lease_id"], vlan=GUEST)

    # Still untouched in its own VLAN.
    assert store.get_lease(lease["lease_id"], OFFICE)["status"] == "active"


def test_same_client_id_is_independent_per_vlan(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.20.0.0/29")
    store.acquire("laptop", vlan=OFFICE)
    again, reused = store.acquire("laptop", vlan=GUEST)
    assert reused is False
    assert again["ip"] == "10.20.0.1"


# ------------------------------------------------------------ configuration


def test_unconfigured_vlan_is_an_error_for_non_config_operations(
    store: Store,
) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    with pytest.raises(LeaseError) as exc:
        store.pool_info(GUEST)
    assert exc.value.status_code == 404
    assert exc.value.code == "vlan_not_configured"
    with pytest.raises(LeaseError) as exc:
        store.acquire("a", vlan=GUEST)
    assert exc.value.code == "vlan_not_configured"
    with pytest.raises(LeaseError) as exc:
        store.renew("anything", vlan=GUEST)
    assert exc.value.code == "vlan_not_configured"
    with pytest.raises(LeaseError):
        store.release("anything", vlan=GUEST)
    with pytest.raises(LeaseError):
        store.out_of_pool_leases(GUEST)


def test_first_pool_set_configures_vlan(store: Store) -> None:
    result = store.switch_pool("10.20.0.0/29", GUEST)
    assert result["vlan"] == GUEST
    assert result["configured"] is True
    assert result["previous_cidr"] is None
    assert store.pool_info(GUEST)["cidr"] == "10.20.0.0/29"


def test_second_pool_set_is_a_switch(store: Store) -> None:
    configure(store, GUEST, "10.20.0.0/29")
    result = store.switch_pool("10.30.0.0/29", GUEST)
    assert result["configured"] is False
    assert result["previous_cidr"] == "10.20.0.0/29"


def test_vlan_range_is_validated(store: Store) -> None:
    for bad in (-1, 4095):
        with pytest.raises(LeaseError) as exc:
            store.pool_info(bad)
        assert exc.value.code == "invalid_vlan"
        with pytest.raises(LeaseError):
            store.switch_pool("10.20.0.0/29", bad)


def test_invalid_pool_does_not_configure_vlan(store: Store) -> None:
    with pytest.raises(LeaseError) as exc:
        store.switch_pool("10.0.0.0/99", GUEST)
    assert exc.value.code == "invalid_pool"
    with pytest.raises(LeaseError) as exc:
        store.pool_info(GUEST)
    assert exc.value.code == "vlan_not_configured"


def test_failed_switch_keeps_previous_pool_and_leases(store: Store) -> None:
    configure(store, GUEST, "10.20.0.0/29")
    lease, _ = store.acquire("a", vlan=GUEST)
    conn = store._connect()
    try:
        conn.executescript(
            """
            CREATE TRIGGER fail_guest_change
            BEFORE INSERT ON pool_changes
            WHEN NEW.vlan = 20
            BEGIN
                SELECT RAISE(ABORT, 'simulated disk failure');
            END;
            """
        )
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        store.switch_pool("10.99.0.0/24", GUEST)

    assert store.pool_info(GUEST)["cidr"] == "10.20.0.0/29"
    assert store.renew(lease["lease_id"], vlan=GUEST)["status"] == "active"


def test_one_vlan_does_not_leak_into_no_vlan_scope(store: Store) -> None:
    configure(store, GUEST, "10.20.0.0/29")
    assert store.pool_info()["cidr"] == "10.20.0.0/29"  # legacy default fixture
    lease, _ = store.acquire("a", vlan=GUEST)
    with pytest.raises(LeaseError) as exc:
        store.get_lease(lease["lease_id"])
    assert exc.value.code == "lease_not_found"


# ---------------------------------------------------------------- restart


def test_restart_restores_all_vlan_pools_and_leases(
    settings: Settings, clock
) -> None:
    first = Store(settings, clock=clock)
    first.initialize()
    first.switch_pool("10.20.0.0/29", OFFICE)
    first.switch_pool("10.20.0.0/29", GUEST)
    office, _ = first.acquire("a", vlan=OFFICE)
    guest, _ = first.acquire("a", vlan=GUEST)
    first.switch_pool("10.30.0.0/29", GUEST)
    guest_after_switch, _ = first.acquire("b", vlan=GUEST)

    reopened = Store(settings, clock=clock)
    reopened.initialize()
    assert reopened.pool_info(OFFICE)["cidr"] == "10.20.0.0/29"
    assert reopened.pool_info(GUEST)["cidr"] == "10.30.0.0/29"
    assert reopened.get_lease(office["lease_id"], OFFICE)["ip"] == office["ip"]
    assert reopened.get_lease(guest["lease_id"], GUEST)["status"] == "active"
    assert (
        reopened.get_lease(guest_after_switch["lease_id"], GUEST)["ip"]
        == guest_after_switch["ip"]
    )
    # Legacy scope is untouched by the VLAN work.
    assert reopened.pool_info()["cidr"] == "10.20.0.0/29"


def test_configured_vlans_lists_every_scope(store: Store) -> None:
    configure(store, OFFICE, "10.20.0.0/29")
    configure(store, GUEST, "10.30.0.0/29")
    listed = {item["vlan"]: item["cidr"] for item in store.configured_vlans()}
    assert listed[OFFICE] == "10.20.0.0/29"
    assert listed[GUEST] == "10.30.0.0/29"


# ------------------------------------------------------- upgrade from v1 DB

V1_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE leases (
    lease_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, ip TEXT NOT NULL,
    pool_cidr TEXT NOT NULL, issued_at REAL NOT NULL, expires_at REAL NOT NULL,
    released_at REAL, release_reason TEXT,
    CHECK (released_at IS NULL OR released_at >= issued_at),
    CHECK (expires_at > issued_at),
    CHECK (release_reason IS NULL OR release_reason IN ('released', 'expired'))
);
CREATE UNIQUE INDEX ux_leases_active_ip ON leases(ip) WHERE released_at IS NULL;
CREATE UNIQUE INDEX ux_leases_active_client
    ON leases(client_id) WHERE released_at IS NULL;
CREATE INDEX ix_leases_expires ON leases(expires_at) WHERE released_at IS NULL;
CREATE TABLE pool_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    previous_cidr TEXT, new_cidr TEXT NOT NULL, changed_at REAL NOT NULL
);
"""


def make_v1_database(path: str, cidr: str = "10.20.0.0/24") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('current_pool', ?)", (cidr,)
        )
        conn.execute(
            "INSERT INTO leases(lease_id, client_id, ip, pool_cidr, issued_at,"
            " expires_at) VALUES ('legacy-1', 'old-laptop', '10.20.0.5', ?,"
            " 1799999000.0, 1800009000.0)",
            (cidr,),
        )
        conn.execute(
            "INSERT INTO leases(lease_id, client_id, ip, pool_cidr, issued_at,"
            " expires_at, released_at, release_reason)"
            " VALUES ('legacy-2', 'gone', '10.20.0.6', ?, 1799998000.0,"
            " 1800008000.0, 1799998500.0, 'released')",
            (cidr,),
        )
        conn.execute(
            "INSERT INTO pool_changes(previous_cidr, new_cidr, changed_at)"
            " VALUES ('10.10.0.0/24', ?, 1799997000.0)",
            (cidr,),
        )
        conn.commit()
    finally:
        conn.close()


def test_upgrade_preserves_legacy_pool_and_lease_identity(
    tmp_path, clock
) -> None:
    db = str(tmp_path / "v1.db")
    make_v1_database(db)
    settings = Settings(
        db_path=db,
        default_pool_cidr="10.99.0.0/24",  # must be ignored, as before
    )
    store = Store(settings, clock=clock)
    store.initialize()

    # The legacy scope keeps the old pool, not the env default.
    assert store.pool_info()["cidr"] == "10.20.0.0/24"
    # Lease ids, addresses and expiry times are unchanged.
    lease = store.get_lease("legacy-1")
    assert lease["lease_id"] == "legacy-1"
    assert lease["ip"] == "10.20.0.5"
    assert lease["expires_at"] == "2027-01-15T10:30:00Z"  # 1800009000
    assert lease["vlan"] is None
    assert store.get_lease("legacy-2")["status"] == "released"

    # Old client still gets its original lease back via a no-VLAN request.
    again, reused = store.acquire("old-laptop")
    assert reused is True and again["lease_id"] == "legacy-1"
    # A new client gets the next free address after the old one.
    fresh, _ = store.acquire("new-laptop")
    assert fresh["ip"] == "10.20.0.1"

    # And a VLAN can be configured on the upgraded database with the same CIDR.
    store.switch_pool("10.20.0.0/24", GUEST)
    guest, _ = store.acquire("old-laptop", vlan=GUEST)
    assert guest["ip"] == "10.20.0.1"
    assert guest["lease_id"] != "legacy-1"


def test_upgrade_migrates_pool_changes_to_legacy_scope(tmp_path, clock) -> None:
    db = str(tmp_path / "v1.db")
    make_v1_database(db)
    store = Store(Settings(db_path=db), clock=clock)
    store.initialize()
    store.switch_pool("10.20.5.0/24")  # legacy scope switch still works

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT vlan, new_cidr FROM pool_changes ORDER BY change_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(LEGACY_VLAN, "10.20.0.0/24"), (LEGACY_VLAN, "10.20.5.0/24")]


def test_upgrade_is_idempotent(tmp_path, clock) -> None:
    db = str(tmp_path / "v1.db")
    make_v1_database(db)
    for _ in range(2):
        Store(Settings(db_path=db), clock=clock).initialize()
    store = Store(Settings(db_path=db), clock=clock)
    store.initialize()
    assert store.get_lease("legacy-1")["ip"] == "10.20.0.5"


# ---------------------------------------------------------------- HTTP API


def test_api_two_vlans_can_use_overlapping_pools(client) -> None:
    for vlan in (OFFICE, GUEST):
        response = client.put(
            "/v1/pool", params={"vlan": vlan}, json={"cidr": "10.20.0.0/29"}
        )
        assert response.status_code == 200
        assert response.json()["vlan"] == vlan
        assert response.json()["configured"] is True

    office = client.post(
        "/v1/leases", params={"vlan": OFFICE}, json={"client_id": "laptop"}
    )
    guest = client.post(
        "/v1/leases", params={"vlan": GUEST}, json={"client_id": "laptop"}
    )
    assert office.status_code == guest.status_code == 201
    assert office.json()["ip"] == guest.json()["ip"] == "10.20.0.1"
    assert office.json()["vlan"] == OFFICE
    assert guest.json()["vlan"] == GUEST
    assert office.headers["location"].endswith("?vlan=10")

    # Usage counters are independent.
    assert client.get("/v1/pool", params={"vlan": OFFICE}).json()["allocated_in_pool"] == 1
    assert client.get("/v1/pool", params={"vlan": GUEST}).json()["allocated_in_pool"] == 1

    listed = client.get("/v1/pools").json()
    assert {item["vlan"] for item in listed["pools"]} >= {OFFICE, GUEST}


def test_api_unconfigured_vlan_is_404(client) -> None:
    assert client.get("/v1/pool", params={"vlan": 777}).status_code == 404
    assert (
        client.get("/v1/pool", params={"vlan": 777}).json()["error"]["code"]
        == "vlan_not_configured"
    )
    acquire = client.post("/v1/leases", params={"vlan": 777}, json={"client_id": "a"})
    assert acquire.status_code == 404
    assert acquire.json()["error"]["code"] == "vlan_not_configured"


def test_api_lease_id_is_invalid_in_another_vlan(client) -> None:
    client.put("/v1/pool", params={"vlan": OFFICE}, json={"cidr": "10.20.0.0/29"})
    lease = client.post(
        "/v1/leases", params={"vlan": OFFICE}, json={"client_id": "a"}
    ).json()
    lease_id = lease["lease_id"]

    assert client.get(f"/v1/leases/{lease_id}").status_code == 404
    assert client.get(f"/v1/leases/{lease_id}", params={"vlan": GUEST}).status_code == 404
    assert (
        client.post(f"/v1/leases/{lease_id}/renew", params={"vlan": GUEST}).status_code
        == 404
    )
    assert (
        client.post(f"/v1/leases/{lease_id}/release", params={"vlan": GUEST}).status_code
        == 404
    )
    # It still works in its own VLAN.
    assert (
        client.post(f"/v1/leases/{lease_id}/renew", params={"vlan": OFFICE}).status_code
        == 200
    )


def test_api_vlan_from_body_is_accepted(client) -> None:
    configured = client.put("/v1/pool", json={"vlan": GUEST, "cidr": "10.20.0.0/29"})
    assert configured.status_code == 200
    lease = client.post("/v1/leases", json={"client_id": "a", "vlan": GUEST})
    assert lease.status_code == 201
    assert lease.json()["vlan"] == GUEST
    renewed = client.post(
        f"/v1/leases/{lease.json()['lease_id']}/renew",
        json={"vlan": GUEST},
    )
    assert renewed.status_code == 200


def test_api_conflicting_vlan_locations_are_rejected(client) -> None:
    response = client.post(
        "/v1/leases",
        params={"vlan": OFFICE},
        json={"client_id": "a", "vlan": GUEST},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_vlan"
    response = client.put(
        "/v1/pool",
        params={"vlan": OFFICE},
        json={"vlan": GUEST, "cidr": "10.20.0.0/29"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("bad", [0, 4095, -1, "abc"])
def test_api_vlan_range_is_validated(client, bad) -> None:
    response = client.get("/v1/pool", params={"vlan": bad})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_api_vlan_configuration_requires_operator(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.db import Store
    from app.main import create_app

    settings = Settings(
        db_path=str(tmp_path / "leases.db"),
        default_pool_cidr="10.20.0.0/29",
        operator_token="s3cret",
    )
    with TestClient(create_app(settings, store=Store(settings))) as locked:
        denied = locked.put(
            "/v1/pool", params={"vlan": GUEST}, json={"cidr": "10.20.0.0/29"}
        )
        assert denied.status_code == 401
        allowed = locked.put(
            "/v1/pool",
            params={"vlan": GUEST},
            json={"cidr": "10.20.0.0/29"},
            headers={"X-Operator-Token": "s3cret"},
        )
        assert allowed.status_code == 200
        # Tenant endpoints remain open for that VLAN.
        assert (
            locked.post(
                "/v1/leases", params={"vlan": GUEST}, json={"client_id": "a"}
            ).status_code
            == 201
        )


def test_api_switch_and_out_of_pool_are_per_vlan(client) -> None:
    for vlan in (OFFICE, GUEST):
        client.put("/v1/pool", params={"vlan": vlan}, json={"cidr": "10.20.0.0/29"})
    office = client.post(
        "/v1/leases", params={"vlan": OFFICE}, json={"client_id": "a"}
    ).json()
    client.put("/v1/pool", params={"vlan": OFFICE}, json={"cidr": "10.30.0.0/29"})

    office_report = client.get(
        "/v1/admin/out-of-pool", params={"vlan": OFFICE}
    ).json()
    guest_report = client.get(
        "/v1/admin/out-of-pool", params={"vlan": GUEST}
    ).json()
    assert office_report["count"] == 1
    assert office_report["leases"][0]["lease_id"] == office["lease_id"]
    assert guest_report["count"] == 0
    # Guest's pool did not move.
    assert client.get("/v1/pool", params={"vlan": GUEST}).json()["cidr"] == "10.20.0.0/29"


# --------------------------------------------------- failure & concurrency


def test_failed_first_configuration_leaves_vlan_unconfigured(store: Store) -> None:
    conn = store._connect()
    try:
        conn.executescript(
            """
            CREATE TRIGGER fail_pool_insert
            BEFORE INSERT ON pools
            WHEN NEW.vlan = 20
            BEGIN
                SELECT RAISE(ABORT, 'simulated disk failure');
            END;
            """
        )
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        store.switch_pool("10.20.0.0/29", GUEST)

    with pytest.raises(LeaseError) as exc:
        store.pool_info(GUEST)
    assert exc.value.code == "vlan_not_configured"


def test_concurrent_acquires_in_overlapping_vlans_are_isolated(
    settings: Settings,
) -> None:
    import os
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    from app.db import LeaseError

    directory = tempfile.mkdtemp(prefix="lease-vlan-race-")
    store = Store(settings.with_(db_path=os.path.join(directory, "db.sqlite")))
    store.initialize()
    store.switch_pool("10.20.0.0/29", OFFICE)
    store.switch_pool("10.20.0.0/29", GUEST)

    def acquire(args: tuple[int, int]) -> tuple[int, str | None]:
        vlan, index = args
        try:
            return vlan, store.acquire(f"client-{index}", vlan=vlan)[0]["ip"]
        except LeaseError as exc:
            assert exc.code == "pool_exhausted"
            return vlan, None

    jobs = [(vlan, index) for vlan in (OFFICE, GUEST) for index in range(8)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(acquire, jobs))

    for vlan in (OFFICE, GUEST):
        addresses = [ip for v, ip in results if v == vlan and ip is not None]
        assert len(addresses) == 6  # /29 capacity per VLAN
        assert len(set(addresses)) == 6


def test_switch_in_one_vlan_does_not_disturb_another_under_race(
    settings: Settings,
) -> None:
    import os
    import threading
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    directory = tempfile.mkdtemp(prefix="lease-vlan-race-")
    store = Store(settings.with_(db_path=os.path.join(directory, "db.sqlite")))
    store.initialize()
    store.switch_pool("10.20.0.0/29", OFFICE)
    store.switch_pool("10.20.0.0/29", GUEST)
    guest_lease, _ = store.acquire("guest-laptop", vlan=GUEST)

    barrier = threading.Barrier(2)

    def acquire_office():
        barrier.wait()
        return store.acquire("office-laptop", vlan=OFFICE)[0]

    def switch_office():
        barrier.wait()
        return store.switch_pool("10.40.0.0/29", OFFICE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        acquire_future = pool.submit(acquire_office)
        switch_future = pool.submit(switch_office)
        office_lease = acquire_future.result()
        switch_future.result()

    # The guest VLAN never moved and its lease is untouched.
    assert store.pool_info(GUEST)["cidr"] == "10.20.0.0/29"
    assert store.get_lease(guest_lease["lease_id"], GUEST)["status"] == "active"
    assert office_lease["ip"] in {"10.20.0.1", "10.40.0.1"}
    assert store.pool_info(OFFICE)["cidr"] == "10.40.0.0/29"


def test_legacy_scope_switch_keeps_old_settings_row_in_step(
    settings: Settings, clock
) -> None:
    store = Store(settings, clock=clock)
    store.initialize()
    store.switch_pool("10.44.0.0/24")
    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'current_pool'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("10.44.0.0/24",)
    # And an old build reading only settings still sees the same pool.
    reopened = Store(settings, clock=clock)
    reopened.initialize()
    assert reopened.pool_info()["cidr"] == "10.44.0.0/24"
