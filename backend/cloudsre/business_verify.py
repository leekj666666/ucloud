"""Model-independent transaction, isolated tenant, evidence before cleanup."""

import uuid

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .verification_data import VerificationData


def verify(order_url, inventory_url, *, database_url=None, evidence_dir=None):
    fixture = VerificationData(database_url, evidence_dir)
    result = {"passed": False, "tenant": fixture.tenant, "checks": {}}
    try:
        fixture.create()
        payload = {"sku": fixture.sku, "quantity": 2}
        headers = {
            "Idempotency-Key": fixture.tenant + "-order",
            "X-Request-ID": str(uuid.uuid4()),
            "X-Test-Tenant": fixture.tenant,
        }
        with httpx.Client(timeout=10) as c:

            def stock():
                r = c.get(inventory_url + "/stock/" + fixture.sku, headers=headers)
                r.raise_for_status()
                return r.json()["available"]

            before = stock()
            created = c.post(order_url + "/orders", json=payload, headers=headers)
            result["http_status"] = created.status_code
            result["trace_id"] = created.headers.get("x-trace-id", "")
            if created.status_code != 200:
                result["check"] = "create_order"
                return result
            data = created.json()
            result["order_id"] = data["id"]
            repeat = c.post(order_url + "/orders", json=payload, headers=headers)
            repeat.raise_for_status()
            read = c.get(order_url + "/orders/" + data["id"], headers=headers)
            read.raise_for_status()
            after_repeat = stock()
            conflict = c.post(
                order_url + "/orders", json={**payload, "quantity": 3}, headers=headers
            )
            after_conflict = stock()
            result["checks"] = {
                "confirmed": data["status"] == "CONFIRMED",
                "payload_matches": (data["sku"], data["quantity"]) == (fixture.sku, 2),
                "readback": read.json() == data,
                "idempotent": repeat.json() == data,
                "stock_delta": before - after_repeat == 2,
                "payload_conflict": conflict.status_code == 409,
                "conflict_stock_unchanged": after_conflict == after_repeat,
            }
            result["passed"] = all(result["checks"].values())
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        result["error_type"] = type(exc).__name__
    finally:
        try:
            result["evidence_path"] = fixture.archive(result)
        except OSError as exc:
            result["passed"] = False
            result["archive_error_type"] = type(exc).__name__
        try:
            result["cleanup"] = fixture.cleanup()
        except (OSError, SQLAlchemyError, ValueError, RuntimeError) as exc:
            result["passed"] = False
            result["cleanup_error_type"] = type(exc).__name__
        try:
            fixture.archive(result)
        except OSError as exc:
            result["passed"] = False
            result["archive_error_type"] = type(exc).__name__
        fixture.close()
    return result
