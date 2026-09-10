"""Structured service API command for plugin and automation callers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.contracts import command_schema
from coding_trajectory.control_plane.http_service import (
    RemoteRuntimeFactory,
    serve_http,
)
from coding_trajectory.runtime import ServiceRuntime

from coding_trajectory_cli._shared import GhFormatter, add_params_flag


def _request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "id": args.request_id,
        "method": args.method,
        "params": dict(args.params_json or {}),
    }


def _read_batch_requests(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.requests_json is not None:
        raw = args.requests_json
    elif args.input == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.input).read_text(encoding="utf-8")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("batch input must be a JSON array")
    for item in parsed:
        if not isinstance(item, dict):
            raise argparse.ArgumentTypeError("batch items must be JSON objects")
    return parsed


def _runtime(args: argparse.Namespace) -> ServiceRuntime:
    """Build a local-first runtime with a lazy remote Chronicles fallback."""

    from coding_trajectory.control_plane.connections import query_source

    source = query_source(
        source=getattr(args, "source", None),
        profile_name=getattr(args, "credential_profile", None),
    )
    if getattr(args, "snapshot_sequence", None) is not None:
        if source == "local":
            raise ValueError("snapshot sequence requires a shared query source")
        source = "shared"
    if source == "shared":
        return _remote_runtime(args)
    return ServiceRuntime(
        global_scope=getattr(args, "global_scope", False),
        current_dir=Path.cwd(),
        connection_profile=getattr(args, "credential_profile", None),
        fallback_factory=(lambda: _remote_runtime(args)) if source == "auto" else None,
    )


def _remote_runtime(args: argparse.Namespace) -> ServiceRuntime:
    """Resolve the same connection used by collectors, only when required."""
    from coding_trajectory.control_plane.connections import resolve_credentials

    credentials = resolve_credentials(
        profile_name=getattr(args, "credential_profile", None),
        url=getattr(args, "cloudflare_url", None),
        access_token=getattr(args, "access_token", None),
        workspace_id=getattr(args, "remote_workspace_id", None),
    )
    factory = RemoteRuntimeFactory(
        url=str(credentials.profile.cloudflare_url),
        workspace_id=credentials.profile.workspace_id,
    )
    return factory.build(
        credentials.access_token,
        snapshot_sequence=getattr(args, "snapshot_sequence", None),
        local_evidence=False,
        current_dir=Path.cwd(),
    )


def _remote_service_config(args: argparse.Namespace) -> str:
    url = args.cloudflare_url or os.environ.get("CT_CLOUDFLARE_URL")
    if not url:
        raise ValueError("remote API requires CT_CLOUDFLARE_URL")
    return str(url)


def _handle_api_call(args: argparse.Namespace) -> dict[str, Any]:
    with _runtime(args) as runtime:
        return runtime.execute(_request_from_args(args))


def _handle_api_batch(args: argparse.Namespace) -> dict[str, Any]:
    requests = _read_batch_requests(args)
    with _runtime(args) as runtime:
        return runtime.batch(requests)


def _handle_api_schema(args: argparse.Namespace) -> dict[str, Any]:
    return command_schema(args.method, command=f"ct api call {args.method}")


def _handle_api_serve(args: argparse.Namespace) -> None:
    url = _remote_service_config(args)
    serve_http(
        factory=RemoteRuntimeFactory(url=url, workspace_id=args.remote_workspace_id),
        host=args.host,
        port=args.port,
    )


def _add_remote_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile", dest="credential_profile", default=argparse.SUPPRESS
    )
    parser.add_argument(
        "--source", choices=("local", "shared", "auto"), default=argparse.SUPPRESS
    )
    parser.add_argument(
        "--remote-workspace-id",
        type=UUID,
        help="Select the fallback Chronicles workspace (defaults to environment or credential profile).",
    )
    parser.add_argument(
        "--snapshot-sequence",
        type=_nonnegative_int,
        help="Read directly from this pinned remote workspace sequence.",
    )
    parser.add_argument("--cloudflare-url", help="Defaults to CT_CLOUDFLARE_URL.")
    parser.add_argument("--access-token", help="Defaults to CT_ACCESS_TOKEN.")


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    api_parser = subparsers.add_parser(
        "api",
        prog="ct api",
        usage="ct api <command> [flags]",
        help="Call versioned service methods for plugins and automation.",
        formatter_class=GhFormatter,
    )
    api_sub = api_parser.add_subparsers(dest="action", required=True)

    call = api_sub.add_parser(
        "call",
        prog="ct api call",
        help="Call one versioned service method.",
        formatter_class=GhFormatter,
    )
    call.add_argument("method", metavar="METHOD", help="Service method name.")
    call.add_argument(
        "--id",
        dest="request_id",
        default=None,
        help="Request id echoed in the response.",
    )
    call.add_argument(
        "--global-scope",
        action="store_true",
        help="Use global discovery for requests without a session entry point.",
    )
    add_params_flag(call)
    _add_remote_flags(call)
    call.set_defaults(
        _plugin_handler=_handle_api_call,
        _default_output="json",
    )

    batch = api_sub.add_parser(
        "batch",
        prog="ct api batch",
        help="Call multiple versioned service methods from a JSON array.",
        formatter_class=GhFormatter,
    )
    batch.add_argument(
        "--input",
        "-i",
        default="-",
        help="Read request array from a file, or '-' for stdin.",
    )
    _add_remote_flags(batch)
    batch.add_argument(
        "--requests",
        dest="requests_json",
        default=None,
        help="Inline JSON request array.",
    )
    batch.add_argument(
        "--global-scope",
        action="store_true",
        help="Use global discovery for requests without session entry points.",
    )
    batch.set_defaults(
        _plugin_handler=_handle_api_batch,
        _default_output="json",
    )

    schema = api_sub.add_parser(
        "schema",
        prog="ct api schema",
        help="Print the JSON schema for one service method.",
        formatter_class=GhFormatter,
    )
    schema.add_argument("method", metavar="METHOD", help="Service method name.")
    schema.set_defaults(
        _plugin_handler=_handle_api_schema,
        _default_output="json",
    )

    serve = api_sub.add_parser(
        "serve",
        prog="ct api serve",
        help="Serve authenticated remote CT call, batch, and schema endpoints.",
        formatter_class=GhFormatter,
    )
    serve.add_argument("--remote-workspace-id", type=UUID, required=True)
    serve.add_argument("--cloudflare-url", help="Defaults to CT_CLOUDFLARE_URL.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(_plugin_handler=_handle_api_serve, _default_output="json")
