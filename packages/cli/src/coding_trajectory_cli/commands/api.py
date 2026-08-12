"""Structured service API command for plugin and automation callers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.contracts import command_schema
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


def _handle_api_call(args: argparse.Namespace) -> dict[str, Any]:
    with ServiceRuntime(global_scope=args.global_scope, current_dir=Path.cwd()) as runtime:
        return runtime.execute(_request_from_args(args))


def _handle_api_batch(args: argparse.Namespace) -> dict[str, Any]:
    requests = _read_batch_requests(args)
    with ServiceRuntime(global_scope=args.global_scope, current_dir=Path.cwd()) as runtime:
        return runtime.batch(requests)


def _handle_api_schema(args: argparse.Namespace) -> dict[str, Any]:
    return command_schema(args.method, command=f"ct api call {args.method}")


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
