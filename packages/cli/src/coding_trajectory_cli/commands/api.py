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
    workspace_id = getattr(args, "remote_workspace_id", None)
    if args.snapshot_sequence is not None and workspace_id is None:
        raise ValueError("--snapshot-sequence requires --remote-workspace-id")
    if workspace_id is not None:
        url, api_key = _remote_service_config(args)
        access_token = args.access_token or os.environ.get("CT_ACCESS_TOKEN")
        if not access_token:
            raise ValueError("remote API requires CT_ACCESS_TOKEN")
        return RemoteRuntimeFactory(
            url=url,
            api_key=api_key,
            workspace_id=workspace_id,
        ).build(access_token, snapshot_sequence=args.snapshot_sequence)
    return ServiceRuntime(
        global_scope=args.global_scope,
        current_dir=Path.cwd(),
    )


def _remote_service_config(args: argparse.Namespace) -> tuple[str, str]:
    url = args.supabase_url or os.environ.get("CT_SUPABASE_URL")
    api_key = args.supabase_api_key or os.environ.get("CT_SUPABASE_ANON_KEY")
    missing = [
        name
        for name, value in (
            ("CT_SUPABASE_URL", url),
            ("CT_SUPABASE_ANON_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ValueError("remote API requires " + ", ".join(missing))
    return str(url), str(api_key)


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
    url, api_key = _remote_service_config(args)
    serve_http(
        factory=RemoteRuntimeFactory(
            url=url, api_key=api_key, workspace_id=args.remote_workspace_id
        ),
        host=args.host,
        port=args.port,
    )


def _add_remote_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--remote-workspace-id",
        type=UUID,
        help="Use the authoritative remote historical snapshot for this workspace.",
    )
    parser.add_argument(
        "--snapshot-sequence",
        type=_nonnegative_int,
        help="Pin remote reads to this workspace sequence (defaults to latest).",
    )
    parser.add_argument("--supabase-url", help="Defaults to CT_SUPABASE_URL.")
    parser.add_argument("--supabase-api-key", help="Defaults to CT_SUPABASE_ANON_KEY.")
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
    serve.add_argument("--supabase-url", help="Defaults to CT_SUPABASE_URL.")
    serve.add_argument("--supabase-api-key", help="Defaults to CT_SUPABASE_ANON_KEY.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(_plugin_handler=_handle_api_serve, _default_output="json")
