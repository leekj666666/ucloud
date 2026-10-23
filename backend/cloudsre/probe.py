from sqlalchemy.exc import SQLAlchemyError

"""Continuous external business black-box probe; no model calls or repair."""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CollectorRegistry, Gauge, Histogram, generate_latest

from .business_verify import verify

registry = CollectorRegistry()
success = Gauge(
    "sre_business_probe_success", "Last real transaction success", registry=registry
)
last = Gauge(
    "sre_business_probe_last_completed_unixtime",
    "Last completed probe",
    registry=registry,
)
latency = Histogram(
    "sre_business_probe_duration_seconds", "Business probe latency", registry=registry
)
healthy = False


async def loop():
    global healthy
    while True:
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                verify, os.environ["GATEWAY_URL"], os.environ["INVENTORY_URL"]
            )
            healthy = result["passed"]
        except (OSError, SQLAlchemyError, ValueError, RuntimeError) as exc:
            healthy = False
            result = {"passed": False, "error_type": type(exc).__name__}
        elapsed = time.monotonic() - started
        success.set(int(healthy))
        last.set(time.time())
        latency.observe(elapsed)
        print(
            json.dumps(
                {
                    "event": "business_probe",
                    "service": "probe",
                    "duration_seconds": elapsed,
                    **result,
                }
            ),
            flush=True,
        )
        await asyncio.sleep(max(0, 10 - elapsed))


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/ready")
@app.get("/health")
def health():
    return {"status": "alive"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")
