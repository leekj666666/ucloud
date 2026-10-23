"""One isolated real order per load request, with measured HTTP latency."""

import time
import uuid

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .verification_data import VerificationData


def sample(order_url, inventory_url):
    fixture = VerificationData()
    result = {"passed": False, "tenant": fixture.tenant, "http_status": 0}
    begin = time.monotonic()
    try:
        fixture.create()
        headers = {"X-Test-Tenant": fixture.tenant, "Idempotency-Key": uuid.uuid4().hex}
        with httpx.Client(timeout=10) as client:
            begin = time.monotonic()
            r = client.post(
                order_url + "/orders",
                headers=headers,
                json={"sku": fixture.sku, "quantity": 1},
            )
            result["latency_seconds"] = time.monotonic() - begin
            result["http_status"] = r.status_code
            result["trace_id"] = r.headers.get("x-trace-id")
            if r.status_code == 200:
                body = r.json()
                stock = client.get(
                    inventory_url + "/stock/" + fixture.sku, headers=headers
                )
                stock.raise_for_status()
                readback = client.get(
                    order_url + "/orders/" + body["id"], headers=headers
                )
                readback.raise_for_status()
                result["passed"] = (
                    body["sku"] == fixture.sku
                    and body["quantity"] == 1
                    and body["status"] == "CONFIRMED"
                    and stock.json()["available"] == 99
                    and readback.json() == body
                )
    except (httpx.HTTPError, ValueError, KeyError, SQLAlchemyError) as exc:
        result["error_type"] = type(exc).__name__
    finally:
        result.setdefault("latency_seconds", time.monotonic() - begin)
        try:
            result["evidence_path"] = fixture.archive(result)
        except OSError:
            result["passed"] = False
        try:
            result["cleanup"] = fixture.cleanup()
        except (SQLAlchemyError, ValueError, OSError):
            result["passed"] = False
            result["cleanup"] = {"cleaned": False}
        try:
            fixture.archive(result)
        except OSError:
            result["passed"] = False
        fixture.close()
    return result
