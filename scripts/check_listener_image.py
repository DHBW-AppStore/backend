#!/usr/bin/env python3
"""Start a disposable image with synthetic settings and probe its real CMD."""
import argparse
import subprocess
import time
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("image")
args = parser.parse_args()

for host in ("0.0.0.0", "::"):
    name = f"backend-listener-test-{uuid.uuid4().hex[:10]}"
    subprocess.run([
        "docker", "run", "--rm", "-d", "--name", name,
        "-e", f"HOST={host}", "-e", "DISABLE_BACKGROUND_TASKS=1",
        "-e", "DATABASE_URL=postgresql+psycopg2://test:test@127.0.0.1/unused",
        "-e", "CREDENTIAL_ENCRYPTION_KEY=Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",
        args.image,
    ], check=True, stdout=subprocess.DEVNULL)
    try:
        urls = ["http://127.0.0.1:8000/health"]
        if host == "::":
            urls.append("http://[::1]:8000/health")
        for url in urls:
            deadline = time.monotonic() + 45
            while True:
                result = subprocess.run([
                    "docker", "exec", name, "curl", "--noproxy", "*", "-fsS",
                    "--max-time", "2", url,
                ], capture_output=True, text=True)
                if result.returncode == 0 and '"status":"healthy"' in result.stdout:
                    print(f"{args.image} HOST={host} {url}: OK")
                    break
                if time.monotonic() >= deadline:
                    subprocess.run(["docker", "logs", "--tail", "30", name], check=False)
                    raise SystemExit(f"Listener check failed: HOST={host} {url}")
                time.sleep(0.5)
    finally:
        subprocess.run(["docker", "stop", "--time", "10", name], check=True, stdout=subprocess.DEVNULL)
