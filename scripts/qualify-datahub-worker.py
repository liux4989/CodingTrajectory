#!/usr/bin/env python3
"""Exercise the exact staged Datahub facade inside local workerd."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from datahub_plugin.api_models import validate_api_response

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "packages/plugins/datahub"
WRANGLER = PLUGIN / "web/node_modules/.bin/wrangler"
WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


class FixtureHandler(BaseHTTPRequestHandler):
    auth_requests: ClassVar[int] = 0

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        if self.path == "/auth/v1/token?grant_type=password":
            type(self).auth_requests += 1
            self._json({"access_token": "synthetic-reader-token", "expires_in": 3600})
            return
        if self.path == "/rest/v1/rpc/ct_workspace_snapshot":
            if self.headers.get("authorization") != "Bearer synthetic-reader-token":
                self._json({"message": "unauthorized"}, status=401)
                return
            self._json({"workspace_id": WORKSPACE_ID, "snapshot_sequence": 7})
            return
        self._json({"message": "not found"}, status=404)

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _json(self, payload: object, *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str) -> tuple[dict[str, object], float]:
    started = time.monotonic()
    with urlopen(url, timeout=30) as response:
        payload = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"workerd facade returned {response.status}")
    return payload, round((time.monotonic() - started) * 1000, 1)


def main() -> None:
    fixture_port = _port()
    worker_port = _port()
    fixture = ThreadingHTTPServer(("127.0.0.1", fixture_port), FixtureHandler)
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    receipt_path = ROOT / ".artifacts/datahub-release/runtime-qualification.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w+", prefix="ct-workerd-", suffix=".log") as log:
        process = subprocess.Popen(
            [
                str(WRANGLER),
                "dev",
                "--local",
                "--config",
                str(PLUGIN / "wrangler.facade-candidate.jsonc"),
                "--port",
                str(worker_port),
                "--var",
                f"CT_SUPABASE_URL:http://127.0.0.1:{fixture_port}",
                "--var",
                "CT_SUPABASE_ANON_KEY:synthetic",
                "--var",
                f"CT_REMOTE_WORKSPACE_ID:{WORKSPACE_ID}",
                "--var",
                "CT_READER_EMAIL:reader@example.invalid",
                "--var",
                "CT_READER_PASSWORD:synthetic",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            url = f"http://127.0.0.1:{worker_port}/api/datahub/snapshot"
            deadline = time.monotonic() + 45
            while True:
                try:
                    # Prove readiness without populating the reader-token cache.
                    urlopen(f"http://127.0.0.1:{worker_port}/not-api", timeout=30).read()
                    break
                except HTTPError as exc:
                    if exc.code == 404:
                        break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("workerd did not become ready") from None
                    time.sleep(0.25)
                except (URLError, ConnectionError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("workerd did not become ready") from None
                    time.sleep(0.25)
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_result = executor.submit(_request, url)
                concurrent_result = executor.submit(_request, url)
                first, first_ms = first_result.result()
                concurrent, concurrent_ms = concurrent_result.result()
            steady, steady_ms = _request(url)
            validate_api_response("snapshot", first)
            validate_api_response("snapshot", concurrent)
            validate_api_response("snapshot", steady)
            if FixtureHandler.auth_requests not in {1, 2}:
                raise RuntimeError(
                    "concurrent reader authentication exceeded the bounded request count"
                )
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-4000:])
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            fixture.shutdown()
            fixture.server_close()

        log.seek(0)
        runtime_log = log.read()
        if "runtime import failed" in runtime_log:
            raise RuntimeError("workerd reported a facade runtime import failure")
        receipt = {
            "first_request_ms": first_ms,
            "concurrent_request_ms": concurrent_ms,
            "steady_request_ms": steady_ms,
            "auth_requests": FixtureHandler.auth_requests,
            "snapshot_sequence": first["revision"],
        }
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        print(f"workerd runtime qualification: PASS ({receipt_path})")


if __name__ == "__main__":
    main()
