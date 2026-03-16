"""Command-line interface for reading coding trajectory data."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from coding_trajectory_cli.rpc_client import RpcClient, RpcError


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    with RpcClient(global_scope=args.global_scope, log_file=getattr(args, "log_file", None)) as client:
        if args.command == "list":
            result = client.call("trajectory.list", {})
            return result if isinstance(result, dict) else {"items": result}

        if args.command in {"trajectory", "session", "turn", "step", "event"}:
            method = f"{args.command}.{args.action}"
            param_name = f"{args.command}_id"
            return client.call(method, {param_name: args.resource_id})

    raise ValueError(f"unsupported command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-trajectory")
    parser.add_argument("--global-scope", action="store_true")
    parser.add_argument("--log-file", metavar="PATH", dest="log_file", help="Absolute path to a specific coding log file to use instead of auto-discovery.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")

    for command in ("trajectory", "session", "turn", "step", "event"):
        command_parser = subparsers.add_parser(command)
        command_subparsers = command_parser.add_subparsers(dest="action", required=True)
        for action in ("overview", "get"):
            action_parser = command_subparsers.add_parser(action)
            action_parser.add_argument("resource_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        payload = _dispatch(args)
    except RpcError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc), "data": exc.data}}, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI fallback
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1

    if args.pretty:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
