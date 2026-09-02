"""Run the service-role graph projection worker."""

from __future__ import annotations

import argparse
import os
import socket
from typing import Any

from coding_trajectory.control_plane.remote import ProjectionWorker, SupabaseRpcClient

from coding_trajectory_cli._shared import GhFormatter


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _handle_run(args: argparse.Namespace) -> dict[str, Any]:
    url = args.supabase_url or os.environ.get("CT_SUPABASE_URL")
    service_key = args.service_role_key or os.environ.get("CT_SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service_key:
        raise ValueError(
            "projector run requires CT_SUPABASE_URL and CT_SUPABASE_SERVICE_ROLE_KEY"
        )
    worker = ProjectionWorker(
        client=SupabaseRpcClient(
            url=url, api_key=service_key, access_token=service_key
        ),
        worker_id=args.worker_id,
    )
    completed = 0
    for _ in range(args.max_jobs):
        if not worker.run_once(lease_seconds=args.lease_seconds):
            break
        completed += 1
    return {"completed": completed, "drained": completed < args.max_jobs}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "projector",
        prog="ct projector",
        help="Materialize accepted canonical observations into graph revisions.",
        formatter_class=GhFormatter,
    )
    commands = parser.add_subparsers(dest="projector_action", required=True)
    run = commands.add_parser("run", help="Lease and process projection jobs.")
    run.add_argument("--max-jobs", type=_positive_int, default=1)
    run.add_argument("--lease-seconds", type=_positive_int, default=120)
    run.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}")
    run.add_argument("--supabase-url", help="Defaults to CT_SUPABASE_URL.")
    run.add_argument(
        "--service-role-key", help="Defaults to CT_SUPABASE_SERVICE_ROLE_KEY."
    )
    run.set_defaults(_plugin_handler=_handle_run, _default_output="json")
