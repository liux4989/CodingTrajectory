"""Run the scoped Cloudflare remote estimation worker."""

from __future__ import annotations

import argparse
import os
import socket
from typing import Any

from coding_trajectory.control_plane.remote import CloudflareRpcClient
from coding_trajectory.control_plane.remote_estimation import RemoteEstimationWorker
from coding_trajectory.estimation.codex import CodexAppServerEstimator

from coding_trajectory_cli._shared import GhFormatter


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _handle_run(args: argparse.Namespace) -> dict[str, Any]:
    url = args.cloudflare_url or os.environ.get("CT_CLOUDFLARE_URL")
    access_token = args.access_token or os.environ.get(
        "CT_ESTIMATOR_ACCESS_TOKEN"
    )
    if not url or not access_token:
        raise ValueError(
            "estimator run requires CT_CLOUDFLARE_URL and CT_ESTIMATOR_ACCESS_TOKEN"
        )
    worker = RemoteEstimationWorker(
        client=CloudflareRpcClient(
            url=url, access_token=access_token
        ),
        worker_id=args.worker_id,
        executor=CodexAppServerEstimator(timeout_seconds=args.provider_timeout),
    )
    completed = 0
    for _ in range(args.max_jobs):
        if not worker.run_once(lease_seconds=args.lease_seconds):
            break
        completed += 1
    return {"completed": completed, "drained": completed < args.max_jobs}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "estimator",
        prog="ct estimator",
        help="Execute queued remote forecasts with server-owned credentials.",
        formatter_class=GhFormatter,
    )
    commands = parser.add_subparsers(dest="estimator_action", required=True)
    run = commands.add_parser("run", help="Lease and process estimation jobs.")
    run.add_argument("--max-jobs", type=_positive_int, default=1)
    run.add_argument("--lease-seconds", type=_positive_int, default=300)
    run.add_argument("--provider-timeout", type=_positive_int, default=300)
    run.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}")
    run.add_argument("--cloudflare-url", help="Defaults to CT_CLOUDFLARE_URL.")
    run.add_argument(
        "--scoped Cloudflare-key", help="Defaults to CT_ESTIMATOR_ACCESS_TOKEN."
    )
    run.set_defaults(_plugin_handler=_handle_run, _default_output="json")
