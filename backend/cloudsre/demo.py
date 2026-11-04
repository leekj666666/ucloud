"""Small real order/inventory workload. No repair or fault-injection endpoints."""

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace import Status as SpanStatus
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, create_engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .telemetry import client_span, provider, trace_id


class DemoBase(DeclarativeBase):
    pass


class Tenant(DemoBase):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(40))


class Stock(DemoBase):
    __tablename__ = "stock"
    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    available: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[str] = mapped_column(String(80), default="demo", index=True)


class Reservation(DemoBase):
    __tablename__ = "reservations"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    sku: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[str] = mapped_column(String(80), default="demo", index=True)


class Order(DemoBase):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    sku: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    trace_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(80), default="demo", index=True)


class Purchase(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(gt=0, le=100)


def log(event, **fields):
    print(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                "release_id": os.environ.get("RELEASE_ID", "dev"),
                **fields,
            }
        ),
        flush=True,
    )


def create_demo(role, database_url, inventory_url="http://inventory:8000"):
    tracing = provider(role)
    tracer = tracing.get_tracer("cloudsre.demo")
    engine = create_engine(
        database_url, pool_pre_ping=True, pool_size=4, max_overflow=2
    )
    sessions = sessionmaker(engine)
    registry = CollectorRegistry()
    requests = Counter(
        "sre_http_requests_total",
        "HTTP requests",
        ["service", "route", "status"],
        registry=registry,
    )
    duration = Histogram(
        "sre_http_duration_seconds",
        "Request duration",
        ["service", "route"],
        registry=registry,
    )

    @asynccontextmanager
    async def lifespan(app):
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        log("service_started", service=role)
        yield
        tracing.shutdown()
        engine.dispose()

    app = FastAPI(title=f"Cloud SRE demo {role}", lifespan=lifespan)

    @app.middleware("http")
    async def telemetry(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "")
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        tenant = request.headers.get("x-test-tenant", "demo")
        if tenant != "demo" and (not tenant.startswith("verify-") or len(tenant) != 39):
            return Response(status_code=400)
        request.state.tenant = tenant
        if request.url.path in ("/metrics", "/health", "/ready"):
            request.state.trace_id = ""
            response = await call_next(request)
            response.headers["x-release-id"] = os.environ.get("RELEASE_ID", "dev")
            return response
        with tracer.start_as_current_span(
            role + " " + request.method + " " + request.url.path,
            context=propagate.extract(dict(request.headers)),
            kind=SpanKind.SERVER,
        ) as span:
            request.state.trace_id = trace_id()
            start = time.monotonic()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers["x-request-id"] = request_id
                response.headers["x-trace-id"] = request.state.trace_id
                response.headers["x-release-id"] = os.environ.get("RELEASE_ID", "dev")
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                elapsed = time.monotonic() - start
                span.set_attribute("http.response.status_code", status)
                span.set_attribute("http.route", route)
                if status >= 500:
                    span.set_status(SpanStatus(StatusCode.ERROR))
                requests.labels(role, route, str(status)).inc()
                duration.labels(role, route).observe(elapsed)
                log(
                    "http_request",
                    service=role,
                    route=route,
                    status=status,
                    trace_id=request.state.trace_id,
                    request_id=request_id,
                    duration_seconds=elapsed,
                )

    @app.get("/health")
    def health():
        return {"status": "alive", "service": role}

    @app.get("/ready")
    def ready():
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ready", "service": role}

    @app.get("/metrics")
    def metrics():
        return Response(
            generate_latest(registry), media_type="text/plain; version=0.0.4"
        )

    if role == "inventory":

        @app.get("/stock/{sku}")
        def stock(sku: str, request: Request):
            with sessions() as s:
                row = s.get(Stock, sku)
                if not row or row.tenant_id != request.state.tenant:
                    raise HTTPException(404, "Unknown SKU")
                return {"sku": row.sku, "available": row.available}

        @app.post("/reservations")
        def reserve(
            data: Purchase,
            request: Request,
            idempotency_key: str = Header(min_length=1, max_length=200),
        ):
            with sessions.begin() as s:
                # Serialize on SKU; conflict-safe insert also protects same key across SKUs.
                stock = s.scalar(
                    select(Stock).where(Stock.sku == data.sku).with_for_update()
                )
                if stock is None or stock.tenant_id != request.state.tenant:
                    raise HTTPException(404, "Unknown SKU")
                existing = s.get(Reservation, idempotency_key)
                if existing:
                    if (existing.sku, existing.quantity, existing.tenant_id) != (
                        data.sku,
                        data.quantity,
                        request.state.tenant,
                    ):
                        raise HTTPException(409, "Idempotency key payload mismatch")
                    return {"key": existing.key, "status": "reserved"}
                if stock.available < data.quantity:
                    raise HTTPException(409, "Insufficient stock")
                created = s.execute(
                    insert(Reservation)
                    .values(
                        key=idempotency_key,
                        tenant_id=request.state.tenant,
                        **data.model_dump(),
                    )
                    .on_conflict_do_nothing(index_elements=["key"])
                    .returning(Reservation.key)
                ).scalar()
                if not created:
                    existing = s.get(Reservation, idempotency_key)
                    if (existing.sku, existing.quantity, existing.tenant_id) != (
                        data.sku,
                        data.quantity,
                        request.state.tenant,
                    ):
                        raise HTTPException(409, "Idempotency key payload mismatch")
                    return {"key": existing.key, "status": "reserved"}
                stock.available -= data.quantity
                return {"key": idempotency_key, "status": "reserved"}

    elif role == "order":

        @app.post("/orders")
        def purchase(
            data: Purchase,
            request: Request,
            idempotency_key: str = Header(min_length=1, max_length=200),
        ):
            # Persist PENDING before contacting the remote service. A timed-out request
            # must retry the same reservation ID, even if the remote side committed.
            order_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, "cloudsre-order:" + idempotency_key)
            )
            with sessions.begin() as s:
                s.execute(
                    insert(Order)
                    .values(
                        id=order_id,
                        key=idempotency_key,
                        **data.model_dump(),
                        status="PENDING",
                        trace_id=request.state.trace_id,
                        tenant_id=request.state.tenant,
                    )
                    .on_conflict_do_nothing()
                )
            with sessions.begin() as s:
                row = s.scalar(
                    select(Order).where(Order.key == idempotency_key).with_for_update()
                )
                if row is None or row.id != order_id:
                    raise HTTPException(
                        409, "Order identity conflicts with idempotency key"
                    )
                if (row.sku, row.quantity, row.tenant_id) != (
                    data.sku,
                    data.quantity,
                    request.state.tenant,
                ):
                    raise HTTPException(409, "Idempotency key payload mismatch")
                if row.status != "CONFIRMED":
                    try:
                        url = inventory_url + "/reservations"
                        with client_span(
                            tracer,
                            "POST",
                            url,
                            {
                                "Idempotency-Key": row.id,
                                "X-Request-ID": request.state.request_id,
                                "X-Test-Tenant": request.state.tenant,
                            },
                        ) as (headers, outgoing):
                            response = httpx.post(
                                url, json=data.model_dump(), headers=headers, timeout=3
                            )
                            outgoing.set_attribute(
                                "http.response.status_code", response.status_code
                            )
                            if response.status_code >= 500:
                                outgoing.set_status(SpanStatus(StatusCode.ERROR))
                        if response.status_code != 200:
                            log(
                                "inventory_rejected",
                                service=role,
                                upstream=inventory_url,
                                request_path="/reservations",
                                status=response.status_code,
                                trace_id=request.state.trace_id,
                            )
                            raise HTTPException(
                                409 if response.status_code in (404, 409, 422) else 503,
                                "Inventory rejected request",
                            )
                        if response.json().get("status") != "reserved":
                            raise ValueError("Invalid inventory response")
                    except (httpx.HTTPError, ValueError):
                        log(
                            "inventory_unavailable",
                            service=role,
                            upstream=inventory_url,
                            trace_id=request.state.trace_id,
                        )
                        raise HTTPException(503, "Inventory unavailable") from None
                    row.status = "CONFIRMED"
                return {
                    "id": row.id,
                    "sku": row.sku,
                    "quantity": row.quantity,
                    "status": row.status,
                    "trace_id": row.trace_id,
                }

        @app.get("/orders/{ident}")
        def order(ident: str, request: Request):
            with sessions() as s:
                row = s.get(Order, ident)
                if not row or row.tenant_id != request.state.tenant:
                    raise HTTPException(404, "Unknown order")
                return {
                    "id": row.id,
                    "sku": row.sku,
                    "quantity": row.quantity,
                    "status": row.status,
                    "trace_id": row.trace_id,
                }
    elif role == "gateway":

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def proxy(path: str, request: Request):
            if path != "orders" and not path.startswith("orders/"):
                raise HTTPException(404, "Unknown gateway route")
            url = os.environ.get("ORDER_URL", "http://order:8000") + "/" + path
            allowed_headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() in {"content-type", "idempotency-key", "x-test-tenant"}
            }
            allowed_headers["X-Request-ID"] = request.state.request_id
            try:
                with client_span(tracer, request.method, url, allowed_headers) as (
                    headers,
                    outgoing,
                ):
                    async with httpx.AsyncClient(timeout=8) as c:
                        upstream = await c.request(
                            request.method,
                            url,
                            content=await request.body(),
                            headers=headers,
                        )
                    outgoing.set_attribute(
                        "http.response.status_code", upstream.status_code
                    )
                    if upstream.status_code >= 500:
                        outgoing.set_status(SpanStatus(StatusCode.ERROR))
                    return Response(
                        content=upstream.content,
                        status_code=upstream.status_code,
                        media_type="application/json",
                    )
            except httpx.HTTPError:
                log(
                    "order_unavailable",
                    service=role,
                    upstream=url,
                    trace_id=request.state.trace_id,
                )
                raise HTTPException(503, "Order service unavailable") from None
    else:
        raise ValueError("Unknown demo role")
    return app
