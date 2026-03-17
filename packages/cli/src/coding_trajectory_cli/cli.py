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


_EPILOG = """
STEP TYPES
  tool_call            A tool invocation (Bash, Read, Edit, …)
  assistant_response   A text message from the assistant
  plan_subagent        A sub-agent spawned during planning

LEARN MORE
  Use 'coding-trajectory <command> --help' for more information about a command.
"""

_LOG_FILE_HELP = "Absolute path to the JSONL log file identifying the session to inspect."


class _GhFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that matches the gh/git style: ALL CAPS sections, USAGE prefix."""

    def start_section(self, heading: str | None) -> None:
        # Defer positional rename — we'll promote to COMMANDS if subparsers are found.
        _renames = {"positional arguments": "ARGUMENTS", "options": "FLAGS"}
        super().start_section(_renames.get(heading or "", heading or ""))

    def add_arguments(self, actions: object) -> None:
        # Promote "ARGUMENTS" → "COMMANDS" when the section holds subparsers.
        if any(isinstance(a, argparse._SubParsersAction) for a in actions):  # type: ignore[attr-defined]
            self._current_section.heading = "COMMANDS"
        super().add_arguments(actions)  # type: ignore[arg-type]

    def format_help(self) -> str:
        import re
        text = super().format_help()
        # "usage:" → "USAGE\n " (gh style)
        text = re.sub(r"^usage:", "USAGE\n ", text, flags=re.MULTILINE)
        # Remove trailing colon from section headers (e.g. "COMMANDS:" → "COMMANDS")
        text = re.sub(r"^([A-Z][A-Z ]+):$", r"\1", text, flags=re.MULTILINE)
        return text


def _add_log_file(p: argparse.ArgumentParser) -> None:
    p.add_argument("--log-file", metavar="PATH", dest="log_file", help=_LOG_FILE_HELP)


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    p.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")
    p.add_argument("--global-scope", action="store_true", help="Search all known log files instead of the most-recent session.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-trajectory",
        description="Inspect coding-session trajectories stored in JSONL log files.",
        epilog=_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser(
        "list",
        help="List available sessions. Returns PATH values for --log-file.",
        formatter_class=_GhFormatter,
    )
    _add_log_file(list_parser)
    _add_output_flags(list_parser)

    traj_parser = subparsers.add_parser(
        "trajectory",
        help="Inspect a trajectory (subcommands: overview, scan).",
        formatter_class=_GhFormatter,
    )
    traj_sub = traj_parser.add_subparsers(dest="action", required=True)

    traj_overview = traj_sub.add_parser(
        "overview",
        help="Return a high-level summary of a trajectory (title, step count, timestamps).",
        formatter_class=_GhFormatter,
    )
    traj_overview.add_argument("resource_id", metavar="TRAJECTORY_ID", nargs="?", default=None)
    _add_log_file(traj_overview)
    _add_output_flags(traj_overview)

    traj_scan = traj_sub.add_parser(
        "scan",
        help="Return steps matching --type and optional --filter expressions.",
        formatter_class=_GhFormatter,
    )
    traj_scan.add_argument("resource_id", metavar="TRAJECTORY_ID", nargs="?", default=None)
    _add_log_file(traj_scan)
    _add_output_flags(traj_scan)
    traj_scan.add_argument(
        "--type",
        dest="step_type",
        required=True,
        metavar="TYPE",
        help="Step type to match. Known types: tool_call, assistant_response, plan_subagent.",
    )
    traj_scan.add_argument(
        "--filter",
        dest="filters",
        action="append",
        metavar="KEY=VALUE",
        default=[],
        help=(
            "Filter on step shape fields. Repeatable. "
            "VALUE=* means field must exist; VALUE=! means field must be absent."
        ),
    )

    step_parser = subparsers.add_parser(
        "step",
        help="Inspect a single step by its step ID (subcommands: details).",
        formatter_class=_GhFormatter,
    )
    step_sub = step_parser.add_subparsers(dest="action", required=True)
    step_details = step_sub.add_parser(
        "details",
        help="Return the full detail payload for a single step.",
        formatter_class=_GhFormatter,
    )
    step_details.add_argument("resource_id", metavar="STEP_ID")
    _add_log_file(step_details)
    _add_output_flags(step_details)

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
    text = json.dumps(payload, indent=indent, ensure_ascii=False)

    if args.output_file:
        from pathlib import Path
        Path(args.output_file).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
