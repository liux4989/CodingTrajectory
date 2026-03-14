"""JSON-RPC 2.0 client that communicates with the server over stdin/stdout via subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


class RpcError(Exception):
    """Raised when the JSON-RPC server returns an error response."""

    def __init__(self, code: int, message: str, data: object = None):
        super().__init__(message)
        self.code = code
        self.data = data


class RpcClient:
    """JSON-RPC 2.0 client that communicates with the server via subprocess stdin/stdout."""

    def __init__(self, *, global_scope: bool = False):
        cmd = [sys.executable, "-m", "coding_trajectory.rpc_server"]
        if global_scope:
            cmd.append("--global-scope")
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._next_id = 1

    def call(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request, read the response, and return the result or raise RpcError."""
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("Server process is not running")

        request_id = self._next_id
        self._next_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(request) + "\n"
        self._process.stdin.write(line.encode())
        self._process.stdin.flush()

        raw = self._process.stdout.readline()
        if not raw:
            raise RuntimeError("Server process closed stdout unexpectedly")

        response = json.loads(raw)

        if "error" in response:
            err = response["error"]
            raise RpcError(err["code"], err["message"], err.get("data"))

        return response.get("result")

    def close(self) -> None:
        """Terminate the server subprocess."""
        if self._process.stdin:
            self._process.stdin.close()
        self._process.terminate()
        self._process.wait()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()
