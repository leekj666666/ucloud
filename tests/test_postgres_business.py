"""Real TCP services and PostgreSQL. CI requires SRE_TEST_DATABASE_URL."""

import concurrent.futures
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cloudsre.business_verify import verify
from cloudsre.demo import Order, Reservation, Stock, Tenant
from cloudsre.verification_data import VerificationData
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

URL = os.environ.get("SRE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="isolated PostgreSQL integration database required"
)


@pytest.fixture(scope="module")
def running(tmp_path_factory):
    saved = os.environ.get("SRE_CI_EVIDENCE_DIR")
    folder = (
        Path(saved) / "http-services"
        if saved
        else tmp_path_factory.mktemp("postgres-http")
    )
    folder.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL)
    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path("backend/cloudsre/demo_migrations").resolve())
    )
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    processes, urls, logs = [], {}, []
    try:
        for role in ("inventory", "order", "gateway"):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            urls[role] = "http://127.0.0.1:" + str(port)
            env = {
                **os.environ,
                "SERVICE_ROLE": role,
                "DATABASE_URL": URL,
                "INVENTORY_URL": urls["inventory"],
                "ORDER_URL": urls.get("order", ""),
                "RELEASE_ID": "ci-v1",
                "PYTHONPATH": "backend",
            }
            log = (folder / (role + ".log")).open("w")
            logs.append(log)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "cloudsre.demo_serve:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(process)
            for _ in range(80):
                try:
                    if httpx.get(urls[role] + "/ready", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Integration service exited")
                time.sleep(0.1)
            else:
                raise RuntimeError("Integration service not ready")
        yield urls, engine, folder
    finally:
        for p in processes:
            p.terminate()
        for p in processes:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()
        engine.dispose()


def test_postgres_gateway_verification_and_parallel_isolation(running):
    urls, engine, folder = running

    def run(_):
        return verify(
            urls["gateway"],
            urls["inventory"],
            database_url=URL,
            evidence_dir=folder / "evidence",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert all(r["passed"] and r["cleanup"]["cleaned"] for r in results), results
    with Session(engine) as s:
        for model in (Order, Reservation, Stock):
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.tenant_id != "demo")
                )
                == 0
            )
        assert (
            s.scalar(
                select(func.count()).select_from(Tenant).where(Tenant.id != "demo")
            )
            == 0
        )


def test_postgres_concurrent_same_key_only_deducts_once(running):
    urls, engine, folder = running
    for round_id in range(20):
        fixture = VerificationData(URL, folder / "evidence")
        fixture.create()
        try:
            barrier = threading.Barrier(4)
            headers = {
                "X-Test-Tenant": fixture.tenant,
                "Idempotency-Key": fixture.tenant + "-same",
            }
            payload = {"sku": fixture.sku, "quantity": 2}

            def request(_, *, barrier=barrier, payload=payload, headers=headers):
                barrier.wait(timeout=10)
                return httpx.post(
                    urls["gateway"] + "/orders",
                    json=payload,
                    headers=headers,
                    timeout=15,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(request, range(4)))
            assert all(r.status_code == 200 for r in responses), (
                round_id,
                [(r.status_code, r.text) for r in responses],
            )
            assert len({r.json()["id"] for r in responses}) == 1
            with Session(engine) as session:
                assert session.get(Stock, fixture.sku).available == 98
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.tenant_id == fixture.tenant)
                    )
                    == 1
                )
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Reservation)
                        .where(Reservation.tenant_id == fixture.tenant)
                    )
                    == 1
                )
            other = httpx.post(
                urls["gateway"] + "/orders",
                json={**payload, "quantity": 3},
                headers=headers,
            )
            assert other.status_code == 409
            with Session(engine) as session:
                assert session.get(Stock, fixture.sku).available == 98
        finally:
            fixture.cleanup()
            fixture.close()
