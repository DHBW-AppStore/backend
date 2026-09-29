"""Exercise real TCP listeners and the application health route in each mode."""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize("host,workers,reload", [
    ("127.0.0.1", 1, False), ("::", 1, False), ("::", 2, False), ("::", 1, True),
])
def test_server_health_over_configured_families(host, workers, reload, tmp_path):
    family = socket.AF_INET6 if host == "::" else socket.AF_INET
    with socket.socket(family) as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "HOST": host, "PORT": str(port), "WORKERS": str(workers),
           "DISABLE_BACKGROUND_TASKS": "1"}
    command = [sys.executable, "-m", "app.server"] + (["--reload"] if reload else [])
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            for target in (["127.0.0.1", "::1"] if host == "::" else ["127.0.0.1"]):
                deadline = time.monotonic() + 15
                response = b""
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection((target, port), timeout=1) as client:
                            client.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                            chunks = []
                            while chunk := client.recv(4096):
                                chunks.append(chunk)
                            response = b"".join(chunks)
                        if b"200 OK" in response:
                            break
                    except OSError:
                        pass
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                log.flush()
                log.seek(0)
                assert b"200 OK" in response and b'"status":"healthy"' in response, log.read()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
