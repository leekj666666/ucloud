"""Independent verifier runs outside Kubernetes through real NodePort/Service routes."""

import asyncio
import os

from fastapi import FastAPI

from .business_verify import verify
from .load_verify import sample

app = FastAPI(title="Cloud SRE independent business verifier")


@app.get("/health")
def health():
    return {
        "status": "alive",
        "path": "docker-network -> NodePort -> Service -> gateway",
    }


@app.post("/verify")
async def verify_business():
    return await asyncio.to_thread(
        verify, os.environ["GATEWAY_URL"], os.environ["INVENTORY_URL"]
    )


@app.post("/sample")
async def load_sample():
    return await asyncio.to_thread(
        sample, os.environ["GATEWAY_URL"], os.environ["INVENTORY_URL"]
    )
