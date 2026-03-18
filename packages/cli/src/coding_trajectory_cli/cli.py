"""Command-line interface for reading coding trajectory data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch, resolve_store


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    log_file = getattr(args, "logfile", None)
    current_dir = Path.cwd()
    cache = IndexCache.load()

    if args.command == "project":
        agent_vendor = getattr(args, "agent_vendor", None)
        if log_file is not None and args.name is None:
            method = "project.logfile"
            params: dict[str, Any] = {"path": log_file}
        elif args.name == "list":
            method = "project.list"
            params = {}
            if agent_vendor is not None:
                params["agent_vendor"] = agent_vendor
        else:
            method = "trajectory.list"
            params = {"project_name": args.name}
            if agent_vendor is not None:
                params["agent_vendor"] = agent_vendor
    elif args.command == "trajectory" and args.action == "overview":
        method = "trajectory.overview"
        params = {"trajectory_id": args.resource_id}
    elif args.command == "trajectory" and args.action == "scan":
        method = "trajectory.scan"
        params = {"trajectory_id": args.resource_id, "type": args.step_type, "filters": args.filters}
    elif args.command == "step" and args.action == "details":
        method = "step.details"
        params = {"step_id": args.resource_id}
    elif args.command == "event" and args.action == "detail":
        method = "event.detail"
        params = {"event_id": args.resource_id}
    else:
        raise ValueError(f"unsupported command: {args.command}")

    effective_global_scope = True if method == "project.list" else args.global_scope
    store, discovery_note = resolve_store(
        params,
        log_file=Path(log_file) if log_file else None,
        global_scope=effective_global_scope,
        current_dir=current_dir,
        cache=cache,
    )
    result = dispatch(
        method,
        params,
        store=store,
        global_scope=effective_global_scope,
        current_dir=current_dir,
        discovery_note=discovery_note,
        cache=cache,
    )
    cache.save()
    return result


_EPILOG = """
WORKFLOW
  1. ct project --logfile <path>           → get trajectory_id
  2. ct trajectory overview <tid>          → see sessions, turns, step_ids
  3. ct trajectory scan <tid> --type TYPE  → find steps by type (with optional --filter)
  4. ct step details <step_id>             → full detail for one step
  5. ct event detail <event_id>            → expand $truncated fields
"""

_SCAN_EPILOG = """
STEP TYPES (for --type)
  tool_call            A tool invocation (Bash, Read, Edit, …)
  assistant_response   A text message from the assistant
  plan_subagent        A sub-agent spawned during planning
  todo_list            A todo/task list update
  session_handoff      A handoff between sessions

FILTER SYNTAX (for --filter)
  key=value            Exact match on a shape field
  key=*               Field must exist
  key=!               Field must be absent/null
  Dot-paths supported: tool_output.error=*
"""

_LOGFILE_HELP = "Absolute path to the JSONL log file to analyze."


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


def _add_logfile(p: argparse.ArgumentParser) -> None:
    p.add_argument("--logfile", metavar="PATH", dest="logfile", help=_LOGFILE_HELP)


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    p.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")
    p.add_argument("--global-scope", action="store_true", help="Search all known log files instead of the most-recent session.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct",
        description="Inspect coding-session trajectories stored in JSONL log files.",
        epilog=_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    project_parser = subparsers.add_parser(
        "project",
        help="List projects, list trajectories, or load a log file.",
        formatter_class=_GhFormatter,
        epilog="Use 'list' as PROJECT_NAME to list all known projects.",
    )
    project_parser.add_argument(
        "name",
        metavar="PROJECT_NAME",
        nargs="?",
        default=None,
        help="Project name to list trajectories for, or 'list' to list all projects.",
    )
    project_parser.add_argument(
        "--agent-vendor",
        metavar="AGENT_VENDOR",
        dest="agent_vendor",
        default=None,
        help=(
            "Filter by agent vendor. "
            "Known values: claude_code, codex_cli, gemini_cli, amp."
        ),
    )
    _add_logfile(project_parser)
    _add_output_flags(project_parser)

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
    _add_logfile(traj_overview)
    _add_output_flags(traj_overview)

    traj_scan = traj_sub.add_parser(
        "scan",
        help="Return steps matching --type and optional --filter expressions.",
        epilog=_SCAN_EPILOG,
        formatter_class=_GhFormatter,
    )
    traj_scan.add_argument("resource_id", metavar="TRAJECTORY_ID", nargs="?", default=None)
    _add_logfile(traj_scan)
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
    _add_output_flags(step_details)

    event_parser = subparsers.add_parser(
        "event",
        help="Resolve a single event by its event ID (subcommands: detail).",
        formatter_class=_GhFormatter,
    )
    event_sub = event_parser.add_subparsers(dest="action", required=True)
    event_detail = event_sub.add_parser(
        "detail",
        help="Return the full content of a single event (resolves $truncated refs from step details or scan).",
        formatter_class=_GhFormatter,
    )
    event_detail.add_argument("resource_id", metavar="EVENT_ID")
    _add_output_flags(event_detail)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        payload = _dispatch(args)
    except ResourceNotFoundError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    except DocumentError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI fallback
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    text = json.dumps(payload, indent=indent, ensure_ascii=False)

    if args.output_file:
        Path(args.output_file).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
