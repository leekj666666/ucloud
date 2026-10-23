from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response

from .alerts import AlertBatch, ingest
from .domain import IncidentInput
from .store import Store


def create_app(database_url):
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
        return {"status": "ok", "stage": "D2"}

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

    return app
