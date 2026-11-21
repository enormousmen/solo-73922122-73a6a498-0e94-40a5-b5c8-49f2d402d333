"""SQLite persistence and the lease state machine.

VLAN scoping
------------
A branch gateway can serve several VLANs.  Each VLAN has its own single
pool and its own lease namespace, so two VLANs may use the same IPv4 range
and the same client ids without colliding.  VLAN ids 1..4094 are external;
``LEGACY_VLAN`` (0) is the internal scope used by requests that omit a VLAN
id.  That scope keeps the pre-VLAN database and API behaviour: the original
pool and lease ids keep working and old rows are migrated to scope 0.

A scope is created by the first successful ``switch_pool`` for it.  Every
other operation requires the scope to exist and returns
``vlan_not_configured`` otherwise.

Concurrency model
-----------------
Every mutating operation runs inside ``BEGIN IMMEDIATE``, which takes
SQLite's database-wide write lock up front.  Writers therefore execute one
at a time, and because a pool switch is a single transaction, a switch
racing with acquire/renew/release always yields the state of some serial
execution order of the two.  Different VLANs share the same writer lock,
which preserves those guarantees at the cost of serialising writes across
VLANs (correctness over throughput).

Reads use a short deferred transaction so that multi-statement reports see
one consistent snapshot even while a switch commits.

Expiry
------
Leases are never deleted.  An unreleased lease whose ``expires_at`` has
passed is swept to ``released_at = <now>, release_reason = 'expired'`` at
the start of each write transaction.  That keeps the partial unique indexes
("one active lease per address", "one active lease per client", both scoped
by VLAN) truthful without needing a clock-dependent index predicate.
"""

from __future__ import annotations

import ipaddress
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import subnets
from .config import Settings
from .subnets import PoolError

# VLAN ids visible to clients are 1..4094.  Scope 0 is the internal
# "legacy" VLAN used by requests that omit a VLAN id; it is what pre-VLAN
# databases and clients keep using.
MIN_VLAN = 1
MAX_VLAN = 4094
LEGACY_VLAN = 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    vlan          INTEGER PRIMARY KEY,
    cidr          TEXT NOT NULL,
    configured_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id       TEXT PRIMARY KEY,
    vlan           INTEGER NOT NULL DEFAULT 0,
    client_id      TEXT NOT NULL,
    ip             TEXT NOT NULL,
    pool_cidr      TEXT NOT NULL,
    issued_at      REAL NOT NULL,
    expires_at     REAL NOT NULL,
    released_at    REAL,
    release_reason TEXT,
    CHECK (released_at IS NULL OR released_at >= issued_at),
    CHECK (expires_at > issued_at),
    CHECK (release_reason IS NULL OR release_reason IN ('released', 'expired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_leases_active_ip_vlan
    ON leases(vlan, ip) WHERE released_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_leases_active_client_vlan
    ON leases(vlan, client_id) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_leases_expires
    ON leases(expires_at) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS pool_changes (
    change_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    vlan           INTEGER NOT NULL DEFAULT 0,
    previous_cidr  TEXT,
    new_cidr       TEXT NOT NULL,
    changed_at     REAL NOT NULL
);
"""


class LeaseError(Exception):
    """Domain error carrying an HTTP status and a stable machine code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Lease:
    lease_id: str
    vlan: int
    client_id: str
    ip: str
    pool_cidr: str
    issued_at: float
    expires_at: float
    released_at: float | None
    release_reason: str | None

    def is_active(self, now: float) -> bool:
        return self.released_at is None and self.expires_at > now

    def status_at(self, now: float) -> str:
        # A lease past its deadline that has not been swept yet still reads as
        # expired, so a report never claims a dead lease is active.
        if self.released_at is not None:
            return "expired" if self.release_reason == "expired" else "released"
        return "active" if self.expires_at > now else "expired"

    def to_dict(self, now: float) -> dict[str, Any]:
        active = self.is_active(now)
        return {
            "lease_id": self.lease_id,
            "vlan": public_vlan(self.vlan),
            "client_id": self.client_id,
            "ip": self.ip,
            "pool_cidr": self.pool_cidr,
            "status": self.status_at(now),
            "issued_at": iso(self.issued_at),
            "expires_at": iso(self.expires_at),
            "released_at": iso(self.released_at) if self.released_at is not None else None,
            "release_reason": self.release_reason,
            "seconds_remaining": max(0, int(self.expires_at - now)) if active else 0,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Lease":
        return cls(
            lease_id=row["lease_id"],
            vlan=row["vlan"],
            client_id=row["client_id"],
            ip=row["ip"],
            pool_cidr=row["pool_cidr"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            released_at=row["released_at"],
            release_reason=row["release_reason"],
        )


def public_vlan(vlan: int) -> int | None:
    """Map the internal legacy scope to ``None`` for API responses."""
    return None if vlan == LEGACY_VLAN else vlan


def validate_vlan(vlan: int) -> int:
    """Validate an internal scope id (0 is allowed only for legacy calls)."""
    if not isinstance(vlan, int) or not LEGACY_VLAN <= vlan <= MAX_VLAN:
        raise LeaseError(
            422,
            "invalid_vlan",
            f"vlan must be between {MIN_VLAN} and {MAX_VLAN}",
        )
    return vlan


class Store:
    """All lease and pool operations, each one a single SQLite transaction."""

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.db_path = settings.db_path
        self._pool = subnets.parse_pool_cidr(settings.default_pool_cidr)

    # ------------------------------------------------------------------ setup

    def _connect(self) -> sqlite3.Connection:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def initialize(self) -> None:
        """Create/migrate the schema and restore or seed the default pool.

        A pre-VLAN database is upgraded in place: existing leases and pool
        changes are assigned to the legacy scope (VLAN 0), and the old
        ``settings.current_pool`` becomes VLAN 0's pool.  Lease ids, addresses
        and expiry times are left untouched.  The environment default only
        seeds a brand-new database.
        """
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            self._migrate_pre_vlan(conn)
            conn.executescript(SCHEMA)
            self._seed_or_restore_legacy_pool(conn)
        finally:
            conn.close()

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_pre_vlan(self, conn: sqlite3.Connection) -> None:
        """Add VLAN columns to a database created by the single-pool version.

        Each table is handled independently so a partially migrated file (for
        example a leases table that already has ``vlan``) stays usable.
        """
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        need_leases = "leases" in tables and "vlan" not in self._columns(conn, "leases")
        need_changes = (
            "pool_changes" in tables
            and "vlan" not in self._columns(conn, "pool_changes")
        )
        if not (need_leases or need_changes):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            if need_leases:
                conn.execute(
                    "ALTER TABLE leases ADD COLUMN vlan INTEGER NOT NULL DEFAULT 0"
                )
                # The pre-VLAN indexes were global; replace them with the
                # VLAN-scoped ones created by SCHEMA below.
                conn.execute("DROP INDEX IF EXISTS ux_leases_active_ip")
                conn.execute("DROP INDEX IF EXISTS ux_leases_active_client")
            if need_changes:
                conn.execute(
                    "ALTER TABLE pool_changes ADD COLUMN vlan INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _seed_or_restore_legacy_pool(self, conn: sqlite3.Connection) -> None:
        """Ensure the legacy scope exists, seeding it on a brand-new file.

        A file that has a legacy pool (including one migrated from the
        pre-VLAN layout) is restored as-is; the environment default is used
        only when no legacy pool has ever been configured.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT cidr FROM pools WHERE vlan = ?", (LEGACY_VLAN,)
            ).fetchone()
            if row is not None:
                self._pool = subnets.parse_pool_cidr(row["cidr"])
            else:
                legacy = conn.execute(
                    "SELECT value FROM settings WHERE key = 'current_pool'"
                ).fetchone()
                cidr = (
                    subnets.parse_pool_cidr(legacy["value"])
                    if legacy is not None
                    else self._pool
                )
                conn.execute(
                    "INSERT INTO pools(vlan, cidr, configured_at) VALUES (?, ?, ?)",
                    (LEGACY_VLAN, str(cidr), self.clock()),
                )
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES ('current_pool', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(cidr),),
                )
                self._pool = cidr
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            if conn.in_transaction:  # pragma: no cover - commit failed
                conn.execute("ROLLBACK")
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            if conn.in_transaction:  # pragma: no cover
                conn.execute("ROLLBACK")
            conn.close()

    # ------------------------------------------------------------- primitives

    def _current_pool(
        self, conn: sqlite3.Connection, vlan: int
    ) -> ipaddress.IPv4Network:
        """Return the pool of an existing VLAN, or fail with a domain error.

        Only ``switch_pool`` may create a scope; every other operation on an
        unconfigured VLAN raises ``vlan_not_configured`` rather than silently
        inventing a pool.
        """
        row = conn.execute(
            "SELECT cidr FROM pools WHERE vlan = ?", (vlan,)
        ).fetchone()
        if row is None:
            raise LeaseError(
                404,
                "vlan_not_configured",
                f"VLAN {public_vlan(vlan)} has no pool configured",
            )
        return subnets.parse_pool_cidr(row["cidr"])

    def _sweep_expired(self, conn: sqlite3.Connection, now: float) -> int:
        cur = conn.execute(
            """
            UPDATE leases
               SET released_at = ?, release_reason = 'expired'
             WHERE released_at IS NULL
               AND expires_at <= ?
            """,
            (now, now),
        )
        return cur.rowcount

    def _active_rows(
        self, conn: sqlite3.Connection, now: float, vlan: int
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM leases "
            "WHERE vlan = ? AND released_at IS NULL AND expires_at > ?",
            (vlan, now),
        ).fetchall()

    @staticmethod
    def _in_pool(pool: ipaddress.IPv4Network, ip_text: str) -> bool:
        try:
            address = ipaddress.IPv4Address(ip_text)
        except ValueError:
            return False
        first, last = subnets.usable_bounds(pool)
        return first <= int(address) <= last

    def _counts(self, pool: ipaddress.IPv4Network, active: list[sqlite3.Row]) -> dict[str, int]:
        first, last = subnets.usable_bounds(pool)
        in_pool = sum(
            1 for r in active if first <= int(ipaddress.IPv4Address(r["ip"])) <= last
        )
        total = subnets.usable_count(pool)
        return {
            "usable_addresses": total,
            "allocated_in_pool": in_pool,
            "free_in_pool": total - in_pool,
            "occupied_out_of_pool": len(active) - in_pool,
        }

    def _resolve_ttl(self, ttl_seconds: int | None) -> int:
        if ttl_seconds is None:
            return self.settings.default_ttl_seconds
        if not (
            self.settings.min_ttl_seconds
            <= ttl_seconds
            <= self.settings.max_ttl_seconds
        ):
            raise LeaseError(
                422,
                "invalid_ttl",
                f"ttl_seconds must be between {self.settings.min_ttl_seconds} "
                f"and {self.settings.max_ttl_seconds}",
            )
        return ttl_seconds

    # ---------------------------------------------------------------- queries

    def pool_info(self, vlan: int = LEGACY_VLAN) -> dict[str, Any]:
        vlan = validate_vlan(vlan)
        now = self.clock()
        with self._read() as conn:
            pool = self._current_pool(conn, vlan)
            active = self._active_rows(conn, now, vlan)
            counts = self._counts(pool, active)
        return {
            "vlan": public_vlan(vlan),
            "cidr": str(pool),
            "network_address": str(pool.network_address),
            "broadcast_address": str(pool.broadcast_address),
            "prefix_length": pool.prefixlen,
            **counts,
            "server_time": iso(now),
        }

    def configured_vlans(self) -> list[dict[str, Any]]:
        """Every scope that has a pool, with its basic usage counters."""
        now = self.clock()
        with self._read() as conn:
            rows = conn.execute("SELECT vlan, cidr FROM pools ORDER BY vlan").fetchall()
            result = []
            for row in rows:
                pool = subnets.parse_pool_cidr(row["cidr"])
                counts = self._counts(pool, self._active_rows(conn, now, row["vlan"]))
                result.append(
                    {"vlan": public_vlan(row["vlan"]), "cidr": str(pool), **counts}
                )
        return result

    def get_lease(self, lease_id: str, vlan: int = LEGACY_VLAN) -> dict[str, Any]:
        """Look a lease up inside ``vlan``; another VLAN's lease is not found."""
        vlan = validate_vlan(vlan)
        now = self.clock()
        with self._read() as conn:
            self._current_pool(conn, vlan)  # unconfigured VLAN is an error
            row = conn.execute(
                "SELECT * FROM leases WHERE lease_id = ? AND vlan = ?",
                (lease_id, vlan),
            ).fetchone()
        if row is None:
            raise LeaseError(404, "lease_not_found", "no lease with that id")
        return Lease.from_row(row).to_dict(now)

    def out_of_pool_leases(self, vlan: int = LEGACY_VLAN) -> list[dict[str, Any]]:
        """Active leases of ``vlan`` whose address is not inside its pool.

        Expired-but-not-yet-swept leases are excluded here the same way a
        write transaction would have swept them.
        """
        vlan = validate_vlan(vlan)
        now = self.clock()
        with self._read() as conn:
            pool = self._current_pool(conn, vlan)
            rows = [
                row
                for row in conn.execute(
                    "SELECT * FROM leases WHERE vlan = ? AND released_at IS NULL",
                    (vlan,),
                ).fetchall()
                if row["expires_at"] > now
            ]
        result = []
        for row in rows:
            if self._in_pool(pool, row["ip"]):
                continue
            lease = Lease.from_row(row)
            item = lease.to_dict(now)
            item["original_pool_cidr"] = lease.pool_cidr
            item["current_pool_cidr"] = str(pool)
            result.append(item)
        result.sort(key=lambda item: (item["expires_at"], item["ip"]))
        return result

    # --------------------------------------------------------------- commands

    def acquire(
        self,
        client_id: str,
        ttl_seconds: int | None = None,
        vlan: int = LEGACY_VLAN,
    ) -> tuple[dict[str, Any], bool]:
        """Allocate an address, or return the client's still-valid lease.

        Returns ``(lease, reused)``.  ``reused`` is true when an existing
        unexpired lease for the client was returned unchanged (even if its
        address has since fallen outside the pool).  Client ids and addresses
        are scoped to ``vlan``: the same client id in another VLAN is a
        different client, and the same address may be active there too.
        """
        vlan = validate_vlan(vlan)
        now = self.clock()
        ttl = self._resolve_ttl(ttl_seconds)
        with self._write() as conn:
            self._sweep_expired(conn, now)
            pool = self._current_pool(conn, vlan)
            existing = conn.execute(
                "SELECT * FROM leases "
                "WHERE vlan = ? AND client_id = ? AND released_at IS NULL",
                (vlan, client_id),
            ).fetchone()
            if existing is not None:
                return Lease.from_row(existing).to_dict(now), True

            held = {
                int(ipaddress.IPv4Address(row["ip"]))
                for row in conn.execute(
                    "SELECT ip FROM leases WHERE vlan = ? AND released_at IS NULL",
                    (vlan,),
                ).fetchall()
            }
            address = subnets.first_free_address(pool, held)
            if address is None:
                raise LeaseError(
                    409, "pool_exhausted", f"no free address left in pool {pool}"
                )
            lease_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO leases(lease_id, vlan, client_id, ip, pool_cidr,
                                   issued_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    vlan,
                    client_id,
                    str(ipaddress.IPv4Address(address)),
                    str(pool),
                    now,
                    now + ttl,
                ),
            )
            row = conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return Lease.from_row(row).to_dict(now), False

    def _scoped_lease(
        self, conn: sqlite3.Connection, lease_id: str, vlan: int
    ) -> Lease:
        row = conn.execute(
            "SELECT * FROM leases WHERE lease_id = ? AND vlan = ?",
            (lease_id, vlan),
        ).fetchone()
        if row is None:
            raise LeaseError(404, "lease_not_found", "no lease with that id")
        return Lease.from_row(row)

    def renew(
        self,
        lease_id: str,
        ttl_seconds: int | None = None,
        vlan: int = LEGACY_VLAN,
    ) -> dict[str, Any]:
        vlan = validate_vlan(vlan)
        now = self.clock()
        ttl = self._resolve_ttl(ttl_seconds)
        with self._write() as conn:
            self._sweep_expired(conn, now)
            pool = self._current_pool(conn, vlan)
            lease = self._scoped_lease(conn, lease_id, vlan)
            if lease.released_at is not None:
                if lease.release_reason == "expired":
                    raise LeaseError(409, "lease_expired", "lease has expired")
                raise LeaseError(409, "lease_released", "lease has already been released")
            if not self._in_pool(pool, lease.ip):
                raise LeaseError(
                    409,
                    "address_out_of_pool",
                    f"{lease.ip} is outside the current pool {pool}; "
                    "it may be released but not renewed",
                )
            conn.execute(
                "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
                (now + ttl, lease_id),
            )
            row = conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return Lease.from_row(row).to_dict(now)

    def release(
        self, lease_id: str, vlan: int = LEGACY_VLAN
    ) -> tuple[dict[str, Any], bool]:
        """Release an active lease.  Releasing an already released lease is a no-op."""
        vlan = validate_vlan(vlan)
        now = self.clock()
        with self._write() as conn:
            self._sweep_expired(conn, now)
            self._current_pool(conn, vlan)  # unconfigured VLAN is an error
            lease = self._scoped_lease(conn, lease_id, vlan)
            if lease.released_at is None:
                conn.execute(
                    """
                    UPDATE leases SET released_at = ?, release_reason = 'released'
                     WHERE lease_id = ?
                    """,
                    (now, lease_id),
                )
                row = conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
                ).fetchone()
                return Lease.from_row(row).to_dict(now), False
            if lease.release_reason == "expired":
                raise LeaseError(409, "lease_expired", "lease has expired")
            return Lease.from_row(
                conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
                ).fetchone()
            ).to_dict(now), True

    def switch_pool(self, cidr: str, vlan: int = LEGACY_VLAN) -> dict[str, Any]:
        """Move ``vlan`` to ``cidr``; the previous pool survives any failure.

        The first successful call for a VLAN configures it.  For an already
        configured VLAN this is a pool switch.  Everything else touches only
        that VLAN's pool and leases.
        """
        vlan = validate_vlan(vlan)
        try:
            target = subnets.parse_pool_cidr(cidr)
        except PoolError as exc:
            raise LeaseError(422, "invalid_pool", str(exc)) from exc
        now = self.clock()
        with self._write() as conn:
            self._sweep_expired(conn, now)
            row = conn.execute(
                "SELECT cidr FROM pools WHERE vlan = ?", (vlan,)
            ).fetchone()
            if row is not None and subnets.parse_pool_cidr(row["cidr"]) == target:
                return {
                    "vlan": public_vlan(vlan),
                    "cidr": str(target),
                    "previous_cidr": str(target),
                    "changed": False,
                    "configured": True,
                    "changed_at": None,
                    **self._counts(target, self._active_rows(conn, now, vlan)),
                }
            previous = subnets.parse_pool_cidr(row["cidr"]) if row is not None else None
            if previous is None:
                conn.execute(
                    "INSERT INTO pools(vlan, cidr, configured_at) VALUES (?, ?, ?)",
                    (vlan, str(target), now),
                )
            else:
                conn.execute(
                    "UPDATE pools SET cidr = ?, configured_at = ? WHERE vlan = ?",
                    (str(target), now, vlan),
                )
            conn.execute(
                """
                INSERT INTO pool_changes(vlan, previous_cidr, new_cidr, changed_at)
                VALUES (?, ?, ?, ?)
                """,
                (vlan, str(previous) if previous is not None else None, str(target), now),
            )
            # Keep the legacy settings row in step with VLAN 0's pool, so an
            # older build pointed at the same file still sees the right pool.
            if vlan == LEGACY_VLAN:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES ('current_pool', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(target),),
                )
            counts = self._counts(target, self._active_rows(conn, now, vlan))
        return {
            "vlan": public_vlan(vlan),
            "cidr": str(target),
            "previous_cidr": str(previous) if previous is not None else None,
            "changed": True,
            "configured": previous is None,
            "changed_at": iso(now),
            **counts,
        }
