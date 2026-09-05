#!/usr/bin/env python3
"""Publish bounded shareable artifacts from the local collector source window.

This script deliberately delegates serialization and delivery to ``ct collector
run``.  That command fences the local source bytes, validates the body-free
``ct.shareable_graph.v1`` artifact, and uses the credential profile to refresh
an access token in memory.  It never exports raw vendor logs or prints a token.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from uuid import UUID


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default="default",
        help="Keychain-backed collector credential profile (default: %(default)s).",
    )
    parser.add_argument(
        "--project-name",
        required=True,
        help="Portable project display name; never a host path.",
    )
    parser.add_argument("--project-id", type=_uuid)
    parser.add_argument("--session-id", type=_uuid)
    parser.add_argument("--agent-vendor")
    parser.add_argument("--repository-identity")
    parser.add_argument("--project-alias", action="append", default=[])
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--since-days", type=_positive_int, default=7)
    parser.add_argument(
        "--no-heartbeat",
        action="store_true",
        help="Do not update the collector liveness sequence.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the redacted collector invocation without publishing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = [
        "uv",
        "run",
        "ct",
        "collector",
        "run",
        "--credential-profile",
        args.profile,
        "--project-name",
        args.project_name,
        "--state-path",
        str(args.state_path.expanduser()),
        "--since-days",
        str(args.since_days),
    ]
    if args.project_id:
        command.extend(("--project-id", str(args.project_id)))
    if args.session_id:
        command.extend(("--session-id", str(args.session_id)))
    if args.agent_vendor:
        command.extend(("--agent-vendor", args.agent_vendor))
    if args.repository_identity:
        command.extend(("--repository-identity", args.repository_identity))
    for alias in args.project_alias:
        command.extend(("--project-alias", alias))
    if args.no_heartbeat:
        command.append("--no-heartbeat")

    if args.dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
