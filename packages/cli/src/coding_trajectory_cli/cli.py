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

        if args.command == "trajectory" and args.action == "overview":
            return client.call("trajectory.overview", {"trajectory_id": args.resource_id})

        if args.command == "trajectory" and args.action == "scan":
            return client.call("trajectory.scan", {
                "trajectory_id": args.resource_id,
                "type": args.step_type,
                "filters": args.filters,
            })

        if args.command == "step" and args.action == "details":
            return client.call("step.details", {"step_id": args.resource_id})

    raise ValueError(f"unsupported command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-trajectory")
    parser.add_argument("--global-scope", action="store_true")
    parser.add_argument("--log-file", metavar="PATH", dest="log_file", help="Absolute path to a specific coding log file to use instead of auto-discovery.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")

    traj_parser = subparsers.add_parser("trajectory")
    traj_sub = traj_parser.add_subparsers(dest="action", required=True)
    traj_overview = traj_sub.add_parser("overview")
    traj_overview.add_argument("resource_id")

    traj_scan = traj_sub.add_parser("scan")
    traj_scan.add_argument("resource_id")
    traj_scan.add_argument("--type", dest="step_type", required=True, metavar="TYPE",
                           help="Step type to match (e.g. tool_call, assistant_response, plan_subagent)")
    traj_scan.add_argument("--filter", dest="filters", action="append", metavar="KEY=VALUE", default=[],
                           help="Filter on shape fields. Chainable. Use KEY=* (exists) or KEY=! (absent).")

    step_parser = subparsers.add_parser("step")
    step_sub = step_parser.add_subparsers(dest="action", required=True)
    step_details = step_sub.add_parser("details")
    step_details.add_argument("resource_id")

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

    indent = 2 if args.pretty else None
    text = json.dumps(payload, indent=indent)

    if args.output_file:
        from pathlib import Path
        Path(args.output_file).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
