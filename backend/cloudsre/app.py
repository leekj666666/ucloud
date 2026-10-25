from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response

from .alerts import AlertBatch, ingest
from .domain import IncidentInput
from .store import Store


def create_app(database_url, *, console_token=None, evidence_roots=(), static_dir=None):
    store = Store(database_url)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield
        store.engine.dispose()

    app = FastAPI(title="云巡 SRE", lifespan=lifespan)

    @app.get("/health")
    def health():
        from sqlalchemy import text

        with store.engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ok", "stage": "D4"}

    @app.get("/incidents")
    def listing():
        return store.list()

    @app.post("/webhooks/alertmanager")
    def alertmanager(batch: AlertBatch):
        return ingest(store, batch)

    @app.post("/incidents")
    def create(data: IncidentInput, response: Response):
        try:
            row, created = store.create(data)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response.status_code = 201 if created else 200
        return row

    @app.get("/incidents/{ident}")
    def get(ident: str):
        row = store.get(ident)
        if not row:
            raise HTTPException(404, "Incident not found")
        return row

    @app.get("/incidents/{ident}/alerts")
    def alerts(ident: str):
        if not store.get(ident):
            raise HTTPException(404, "Incident not found")
        return store.receipts(ident)

    @app.get("/incidents/{ident}/runs")
    def runs(ident: str):
        if not store.get(ident):
            raise HTTPException(404, "Incident not found")
        return store.runs(ident)

    from .console_api import install

    install(app, store, token=console_token, evidence_roots=evidence_roots)
    if static_dir:
        from pathlib import Path

        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        dist = Path(static_dir)
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def console_index():
            return FileResponse(
                dist / "index.html",
                headers={
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-cache",
                },
            )

    return app
