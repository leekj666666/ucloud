"""Real process groups, including descendants; no model API calls."""

import importlib
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSRE_DIAG_WORK", str(tmp_path / "jobs"))
    monkeypatch.setenv("CLOUDSRE_DIAG_TOKEN", "diagnostic-test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-test-value")
    monkeypatch.setenv("CLOUDSRE_HOLMES_PYTHON", __import__("sys").executable)
    monkeypatch.setenv("CLOUDSRE_DIAG_TIMEOUT_SECONDS", "30")
    child_runner = tmp_path / "heartbeat.py"
    child_runner.write_text(
        "import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(0.05)\n"
    )
    runner = tmp_path / "blocking_runner.py"
    runner.write_text(f"""import sys,subprocess,time
from pathlib import Path
folder=Path(sys.argv[1])
child=subprocess.Popen([sys.executable,{str(child_runner)!r},str(folder/'child-heartbeat')])
(folder/'child.pid').write_text(str(child.pid))
while True: time.sleep(1)
""")
    monkeypatch.setenv("CLOUDSRE_DIAGNOSTIC_RUNNER", str(runner))
    module = importlib.import_module("cloudsre.diagnosis_jobs")
    if module.ROOT != tmp_path / "jobs":
        module.POOL.shutdown(wait=True, cancel_futures=True)
        module = importlib.reload(module)
    with TestClient(module.app) as client:
        yield client, module, {"X-Diagnostic-Token": "diagnostic-test-token"}
    for process in list(module.PROCESSES.values()):
        module.terminate_group(process)
    module.POOL.shutdown(wait=True, cancel_futures=True)


def wait_until(fn, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for actual process state")


def test_cancel_kills_running_diagnostic_and_child(jobs):
    client, module, headers = jobs
    ident = uuid.uuid4().hex
    assert (
        client.post(
            f"/jobs/{ident}", json={"incident": {"id": "bounded"}}, headers=headers
        ).status_code
        == 200
    )
    heartbeat = module.ROOT / ident / "child-heartbeat"
    wait_until(heartbeat.exists)
    assert client.post(f"/jobs/{ident}/cancel").status_code == 403
    response = client.post(f"/jobs/{ident}/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "cancelled"
    before = heartbeat.read_text()
    time.sleep(0.2)
    assert heartbeat.read_text() == before
    assert (
        client.post(f"/jobs/{ident}/cancel", headers=headers).json()["state"]
        == "cancelled"
    )
    assert ident not in module.PROCESSES


def test_cancel_before_create_fences_delayed_submission(jobs):
    client, module, headers = jobs
    ident = uuid.uuid4().hex
    assert (
        client.post(f"/jobs/{ident}/cancel", headers=headers).json()["state"]
        == "cancelled"
    )
    response = client.post(
        f"/jobs/{ident}", json={"incident": {"id": "late"}}, headers=headers
    )
    assert response.status_code == 409
    assert not (module.ROOT / ident / "child.pid").exists()


def test_timeout_terminates_descendants_and_retains_state(jobs, monkeypatch):
    client, module, headers = jobs
    monkeypatch.setenv("CLOUDSRE_DIAG_TIMEOUT_SECONDS", "1")
    ident = uuid.uuid4().hex
    assert (
        client.post(
            f"/jobs/{ident}", json={"incident": {}}, headers=headers
        ).status_code
        == 200
    )
    path = module.ROOT / ident / "status.json"
    state = wait_until(
        lambda: d if (d := json.loads(path.read_text()))["state"] != "running" else None
    )
    assert state["state"] == "timeout"
    heartbeat = module.ROOT / ident / "child-heartbeat"
    before = heartbeat.read_text()
    time.sleep(0.2)
    assert heartbeat.read_text() == before
    assert (module.ROOT / ident / "process.log").exists()
