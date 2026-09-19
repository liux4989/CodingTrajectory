#!/usr/bin/env python3
"""Capture one bounded staging driver invocation with Wrangler realtime tail."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

WORKER = "coding-trajectory-control-plane-staging"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--wrangler", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("driver command required after --")
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = args.trace.with_suffix(args.trace.suffix + ".stderr")
    metadata = {
        "schema_version": "ct.prepared-api-staging-capture.v1",
        "worker": WORKER,
        "trace_source": "wrangler-realtime-4.129.1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completed_at": None,
        "driver_exit_code": None,
        "tail_exit_code": None,
    }
    args.metadata.write_text(json.dumps(metadata, indent=2) + "\n")
    with args.trace.open("wb") as raw, stderr_path.open("wb") as tail_stderr:
        tail = subprocess.Popen(
            [str(args.wrangler), "tail", WORKER, "--format", "json"],
            stdout=raw,
            stderr=tail_stderr,
            cwd=args.wrangler.resolve().parents[2],
            env=os.environ.copy(),
        )
        try:
            time.sleep(6)
            if tail.poll() is not None:
                raise RuntimeError(
                    f"Wrangler tail exited before driver: {tail.returncode}"
                )
            driver = subprocess.run(command, env=os.environ.copy(), check=False)
            metadata["driver_exit_code"] = driver.returncode
            time.sleep(10)
        finally:
            if tail.poll() is None:
                tail.send_signal(signal.SIGINT)
                try:
                    tail.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    tail.terminate()
                    tail.wait(timeout=5)
            metadata["tail_exit_code"] = tail.returncode
            metadata["completed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            args.metadata.write_text(json.dumps(metadata, indent=2) + "\n")
    if metadata["driver_exit_code"] != 0:
        raise RuntimeError(f"driver exited {metadata['driver_exit_code']}")
    if not args.trace.stat().st_size:
        raise RuntimeError("Wrangler trace capture is empty")
    print(
        json.dumps(
            {
                "driver_exit_code": metadata["driver_exit_code"],
                "tail_exit_code": metadata["tail_exit_code"],
                "trace_bytes": args.trace.stat().st_size,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
