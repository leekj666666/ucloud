import os
import time
import uuid

import httpx

while True:
    try:
        r = httpx.post(
            os.environ.get("ORDER_URL", "http://order:8000") + "/orders",
            json={"sku": "demo-sku", "quantity": 1},
            headers={"Idempotency-Key": "load-" + str(uuid.uuid4())},
            timeout=5,
        )
        print("workload response", r.status_code, flush=True)
    except httpx.HTTPError:
        print("workload network error", flush=True)
    time.sleep(2)
