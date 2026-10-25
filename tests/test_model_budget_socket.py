import concurrent.futures
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import uvicorn
from cloudsre.model_budget import ModelBudget, create_proxy


def test_budget_counts_real_local_upstream_http_and_rejects_concurrent_overflow(
    tmp_path,
):
    calls = []
    lock = threading.Lock()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            with lock:
                calls.append(
                    {
                        "body": json.loads(body),
                        "authorization": self.headers["authorization"],
                    }
                )
            encoded = json.dumps(
                {"choices": [], "usage": {"total_tokens": 10}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    budget = ModelBudget(tmp_path / "real-http.db", 7)
    token = budget.register("same-run", 8)
    app = create_proxy(
        budget,
        upstream_base=f"http://127.0.0.1:{provider.server_port}",
        provider_key="test-provider-key",
        model="frozen",
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    proxy = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    proxy.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)

        def invoke(_):
            with httpx.Client(timeout=20) as client:
                return client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "model": "frozen",
                        "messages": [{"role": "user", "content": "actual socket test"}],
                    },
                ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(invoke, range(24)))
        assert statuses.count(200) == 7 and statuses.count(429) == 17
        assert len(calls) == 7
        assert all(row["authorization"] == "Bearer test-provider-key" for row in calls)
        assert budget.summary()["used_slots"] == 7
        assert (
            ModelBudget(tmp_path / "real-http.db", 7).summary()["known_usage_attempts"]
            == 7
        )
    finally:
        server.should_exit = True
        proxy.join(timeout=10)
        listener.close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        assert not proxy.is_alive() and not thread.is_alive()
