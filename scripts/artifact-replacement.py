#!/usr/bin/env python3
"""Validate, reset for, import, and verify one frozen artifact replacement.

Remote operations read the bearer from an environment variable and never print
it. Reset commands make one request only: an incomplete bounded R2 deletion must
be inspected and explicitly resumed by rerunning the same exact command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.artifact_replacement import (
    access_token_from_env,
    import_replacement,
    load_replacement,
    verify_replacement,
)
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)

DESTRUCTIVE_SCOPE = "workspace-sql-all+workspace-artifact-r2-prefix"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "preview"):
        command = subparsers.add_parser(name)
        command.add_argument("bundle", type=Path)
    for name in ("reset-preview", "reset-execute", "import", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("bundle", type=Path)
        command.add_argument("--url", required=True)
        command.add_argument("--token-env", default="CT_ACCESS_TOKEN")
        command.add_argument("--confirmed-workspace", type=_uuid, required=True)
        command.add_argument("--confirmed-export-sha256", required=True)
        if name == "reset-execute":
            command.add_argument(
                "--confirmed-destructive-scope",
                required=True,
                help=f"must be exactly {DESTRUCTIVE_SCOPE!r}",
            )
    return parser.parse_args()


def _confirmed(args: argparse.Namespace):
    replacement = load_replacement(args.bundle)
    manifest = replacement.manifest
    if args.confirmed_workspace != manifest.workspace_id:
        raise ValueError("confirmed workspace does not match frozen replacement")
    if args.confirmed_export_sha256 != manifest.replacement_sha256:
        raise ValueError("confirmed export digest does not match frozen replacement")
    return replacement


def _replace(args: argparse.Namespace, mode: str) -> dict[str, object]:
    replacement = _confirmed(args)
    token = access_token_from_env(args.token_env)
    workspace_id = str(replacement.manifest.workspace_id)
    digest = replacement.manifest.replacement_sha256
    rpc = CloudflareRpcClient(url=args.url, access_token=token)
    try:
        # Quota/access preflight. A reset cannot restore Cloudflare daily quota.
        snapshot = rpc.call("ct_workspace_snapshot", {"workspace_id": workspace_id})
        result = rpc.call(
            "ct_workspace_replace",
            {
                "workspace_id": workspace_id,
                "mode": mode,
                "expected_export_sha256": digest,
                "confirmation": f"{mode}:{workspace_id}:{digest}",
            },
        )
        return {"quota_preflight_snapshot": snapshot["snapshot_sequence"], **result}
    finally:
        rpc.close()


def main() -> int:
    args = parse_args()
    replacement = load_replacement(args.bundle)
    if args.command == "validate":
        result = {"status": "valid", **replacement.summary()}
    elif args.command == "preview":
        result = {
            "status": "offline-preview",
            **replacement.summary(),
            "destructive_scope": DESTRUCTIVE_SCOPE,
            "r2_prefix": (f"workspaces/{replacement.manifest.workspace_id}/artifacts/"),
            "preserved": [
                "credentials and deployed bindings",
                "other workspace Durable Objects",
                "other R2 prefixes",
            ],
            "execution_requirements": [
                "explicit matching workspace and export digest",
                "owner credential scoped to that workspace",
                "enabled exact replacement gate",
                "successful quota preflight",
            ],
        }
    elif args.command == "reset-preview":
        result = _replace(args, "preview")
    elif args.command == "reset-execute":
        if args.confirmed_destructive_scope != DESTRUCTIVE_SCOPE:
            raise ValueError("destructive scope confirmation mismatch")
        result = _replace(args, "execute")
    elif args.command == "import":
        replacement = _confirmed(args)
        result = import_replacement(
            replacement,
            url=args.url,
            access_token=access_token_from_env(args.token_env),
        )
    elif args.command == "verify":
        replacement = _confirmed(args)
        result = verify_replacement(
            replacement,
            url=args.url,
            access_token=access_token_from_env(args.token_env),
        )
    else:  # pragma: no cover - argparse guarantees a command
        raise AssertionError(args.command)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RemoteControlPlaneError, ValueError) as exc:
        print(f"artifact replacement failed: {exc}", file=sys.stderr)
        sys.exit(1)
