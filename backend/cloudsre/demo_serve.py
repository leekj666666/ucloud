import os

from .demo import create_demo

app = create_demo(
    os.environ["SERVICE_ROLE"],
    os.environ["DATABASE_URL"],
    os.environ.get("INVENTORY_URL", "http://inventory:8000"),
)
