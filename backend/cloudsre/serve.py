import json
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
    ),
    console_token=os.environ.get("CLOUDSRE_CONSOLE_TOKEN"),
    evidence_roots=json.loads(os.environ.get("CLOUDSRE_CONSOLE_EVIDENCE_ROOTS", "[]")),
    static_dir=os.environ.get("CLOUDSRE_UI_DIR"),
)
