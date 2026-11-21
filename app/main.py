"""FastAPI application: VLAN-scoped IPv4 lease broker."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .db import LEGACY_VLAN, MAX_VLAN, MIN_VLAN, LeaseError, Store

DESCRIPTION = """
VLAN-scoped IPv4 DHCP-style lease API.

* Each VLAN (1–4094) has its own single pool and its own lease namespace.
  Two VLANs may use the same CIDR and the same `client_id` independently.
* **Acquire** (`POST /v1/leases`) hands out the lowest free address of the
  selected VLAN's current pool.  Re-acquiring with the same `client_id` in
  that VLAN returns the client's unexpired lease unchanged (HTTP 200,
  `reused: true`).
* **Renew / release** (`POST /v1/leases/{lease_id}/renew|release`) require
  the current lease id **and** the VLAN that owns it; a lease id is not
  valid in another VLAN.
* **Pool** (`GET`/`PUT /v1/pool`) takes an optional `vlan`; omitting it uses
  the legacy default pool.  The first successful `PUT` configures a VLAN;
  any other access to an unconfigured VLAN is an error.  A switch is atomic:
  it either fully commits or the previous pool keeps serving.  Pools and
  leases survive restarts.
* **Operators** can list still-occupied addresses outside a VLAN's pool.
"""


def scope_of(vlan: int | None) -> int:
    """Map the optional API VLAN id to the internal scope id."""
    return LEGACY_VLAN if vlan is None else vlan


def resolve_vlan(query_vlan: int | None, body_vlan: int | None) -> int | None:
    """Merge the query-string and body VLAN ids, rejecting contradictions."""
    if query_vlan is not None and body_vlan is not None and query_vlan != body_vlan:
        raise LeaseError(
            422, "invalid_vlan", "vlan in the body and query string must match"
        )
    return body_vlan if body_vlan is not None else query_vlan


VlanParam = Annotated[
    int | None,
    Query(
        ge=MIN_VLAN,
        le=MAX_VLAN,
        description="VLAN id (1–4094); omit for the legacy default pool",
    ),
]


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=128)
    ttl_seconds: int | None = Field(default=None, ge=1)
    vlan: int | None = Field(default=None, ge=MIN_VLAN, le=MAX_VLAN)

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("client_id must not be blank")
        return value


class RenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int | None = Field(default=None, ge=1)
    vlan: int | None = Field(default=None, ge=MIN_VLAN, le=MAX_VLAN)


class PoolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cidr: str = Field(min_length=1, max_length=32)
    vlan: int | None = Field(default=None, ge=MIN_VLAN, le=MAX_VLAN)


class ErrorBody(BaseModel):
    code: str
    message: str


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": ErrorBody(code=code, message=message).model_dump()},
    )


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store.initialize()
        yield

    app = FastAPI(
        title="Branch Gateway Lease API",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store or Store(settings)

    def get_store(request: Request) -> Store:
        return request.app.state.store

    def require_operator(
        request: Request,
        x_operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
    ) -> None:
        expected = request.app.state.settings.operator_token
        if expected is None:
            return
        if x_operator_token is None or not _constant_time_eq(x_operator_token, expected):
            raise LeaseError(401, "unauthorized", "a valid X-Operator-Token is required")

    @app.exception_handler(LeaseError)
    async def handle_lease_error(_: Request, exc: LeaseError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Keep the error envelope uniform across domain and schema failures.
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'][1:]) or 'body'}: {err['msg']}"
            for err in exc.errors()
        )
        return error_response(422, "invalid_request", details or "invalid request")

    # ------------------------------------------------------------------ meta

    @app.get("/health", tags=["meta"], summary="Liveness and configuration check")
    def health(request: Request) -> dict[str, Any]:
        return {"status": "ok", "pool": request.app.state.store.pool_info()["cidr"]}

    # ------------------------------------------------------------------ pool

    @app.get("/v1/pools", tags=["pool"], summary="List configured VLAN pools")
    def list_pools(store: Store = Depends(get_store)) -> dict[str, Any]:
        pools = store.configured_vlans()
        return {"count": len(pools), "pools": pools}

    @app.get("/v1/pool", tags=["pool"], summary="Show a VLAN's pool and usage")
    def get_pool(
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
    ) -> dict[str, Any]:
        return store.pool_info(scope_of(vlan))

    @app.put(
        "/v1/pool",
        tags=["pool"],
        summary="Configure or switch a VLAN's pool atomically (operator)",
    )
    def put_pool(
        body: PoolRequest,
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
        _: None = Depends(require_operator),
    ) -> dict[str, Any]:
        scope = resolve_vlan(vlan, body.vlan)
        return store.switch_pool(body.cidr, scope_of(scope))

    # ---------------------------------------------------------------- leases

    @app.post(
        "/v1/leases",
        tags=["leases"],
        summary="Acquire a lease in a VLAN (idempotent per client)",
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "Existing unexpired lease returned"}},
    )
    def acquire(
        body: LeaseRequest,
        response: Response,
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
    ) -> dict[str, Any]:
        scope = resolve_vlan(vlan, body.vlan)
        lease, reused = store.acquire(
            body.client_id, body.ttl_seconds, scope_of(scope)
        )
        if reused:
            response.status_code = status.HTTP_200_OK
            lease["reused"] = True
        else:
            response.status_code = status.HTTP_201_CREATED
            if scope is None:
                response.headers["Location"] = f"/v1/leases/{lease['lease_id']}"
            else:
                response.headers["Location"] = (
                    f"/v1/leases/{lease['lease_id']}?vlan={scope}"
                )
            lease["reused"] = False
        return lease

    @app.get("/v1/leases/{lease_id}", tags=["leases"], summary="Inspect a lease")
    def get_lease(
        lease_id: str,
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
    ) -> dict[str, Any]:
        return store.get_lease(lease_id, scope_of(vlan))

    @app.post(
        "/v1/leases/{lease_id}/renew",
        tags=["leases"],
        summary="Renew an active lease in its VLAN's current pool",
    )
    def renew(
        lease_id: str,
        body: RenewRequest | None = None,
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
    ) -> dict[str, Any]:
        ttl = body.ttl_seconds if body is not None else None
        scope = resolve_vlan(vlan, body.vlan if body is not None else None)
        return store.renew(lease_id, ttl, scope_of(scope))

    @app.post(
        "/v1/leases/{lease_id}/release",
        tags=["leases"],
        summary="Release an active lease (repeat release is a no-op)",
    )
    def release(
        lease_id: str,
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
    ) -> dict[str, Any]:
        lease, already = store.release(lease_id, scope_of(vlan))
        lease["already_released"] = already
        return lease

    # ----------------------------------------------------------------- admin

    @app.get(
        "/v1/admin/out-of-pool",
        tags=["admin"],
        summary="List still-occupied addresses outside a VLAN's pool (operator)",
    )
    def out_of_pool(
        vlan: VlanParam = None,
        store: Store = Depends(get_store),
        _: None = Depends(require_operator),
    ) -> dict[str, Any]:
        leases = store.out_of_pool_leases(scope_of(vlan))
        return {"vlan": vlan, "count": len(leases), "leases": leases}

    return app


def _constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


app = create_app()
