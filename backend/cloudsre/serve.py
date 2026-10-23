import os
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

from .app import create_app

cfg = dotenv_values(Path.home() / "sre-project/infra/.env")
app = create_app(
    os.environ.get("DATABASE_URL")
    or (
        "postgresql+psycopg://sre:"
        + quote(cfg["POSTGRES_PASSWORD"], safe="")
        + "@localhost:15432/sre"
    )
)
