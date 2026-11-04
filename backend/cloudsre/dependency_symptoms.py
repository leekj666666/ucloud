"""Bound dependency symptoms; business rejection alone never authorizes repair."""
import json
from urllib.parse import urlsplit


def matches_dependency_failure(lines, current, expected):
    for line in lines:
        # Preserve the established transport-failure path; no policy relaxation.
        if current and current in line and "inventory_unavailable" in line:
            return True
        try:
            event = json.loads(line)
            live, healthy = urlsplit(current), urlsplit(expected)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        # Only same-origin path drift with exact target and operation correlation.
        # 409/422 (stock, idempotency, validation) are not connectivity evidence.
        if (
            event.get("event") == "inventory_rejected"
            and event.get("service") == "order"
            and event.get("status") == 404
            and event.get("upstream") == current
            and event.get("request_path") == "/reservations"
            and live.scheme in {"http", "https"}
            and (live.scheme, live.netloc) == (healthy.scheme, healthy.netloc)
            and live.path.rstrip("/") != healthy.path.rstrip("/")
            and not live.query and not live.fragment
            and not healthy.query and not healthy.fragment
            and not live.username and not healthy.username
        ):
            return True
    return False
