"""Reconnect local Kubernetes port forwards after demo Pod replacement."""

import subprocess
import threading
import time
from contextlib import contextmanager

import httpx

from .kube import PREFIX

ORDER_URL = "http://localhost:18081"
INVENTORY_URL = "http://localhost:18082"
GATEWAY_URL = "http://localhost:18083"


@contextmanager
def business_ports():
    stop = threading.Event()

    def supervise(service, port):
        while not stop.is_set():
            p = subprocess.Popen(
                PREFIX
                + [
                    "port-forward",
                    "--address",
                    "127.0.0.1",
                    "service/" + service,
                    f"{port}:8000",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                while p.poll() is None and not stop.wait(0.5):
                    pass
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
            stop.wait(1)

    threads = [
        threading.Thread(target=supervise, args=(s, p), daemon=True)
        for s, p in [("order", 18081), ("inventory", 18082), ("gateway", 18083)]
    ]
    for t in threads:
        t.start()
    try:
        for url in [ORDER_URL, INVENTORY_URL, GATEWAY_URL]:
            for attempt in range(30):
                try:
                    if httpx.get(url + "/ready", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if attempt == 29:
                    raise RuntimeError("Business port forward unavailable")
                time.sleep(1)
        yield
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
