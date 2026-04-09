"""Command-line interface for reading coding trajectory data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch, resolve_store


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    current_dir = Path.cwd()
    cache = IndexCache.load()

    method: str = args._method
    params: dict[str, Any] = args._params(args)

    effective_global_scope = True if method == "project.list" else args.global_scope
    store, discovery_note = resolve_store(
        params,
        log_file=None,
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


def _project_list_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _project_trajectories_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {"project_name": args.project_name}
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


_EPILOG = """\
NAVIGATE
  ct project list                                  list all known projects
  ct project trajectories PROJECT_NAME             list trajectories for a project
  ct trajectory overview [TRAJECTORY_ID]           session structure, step types, user requests
  ct step detail STEP_ID [...]                     full detail for one or more steps

INSPECT COMMANDS
  ct event scan [TRAJECTORY_ID] --type TYPE [--filter KEY=VALUE]
                                                   query raw events by type
  ct event detail EVENT_ID                         expand $truncated fields from step details

NOTE
  Trajectories are located automatically via cache; pass a TRAJECTORY_ID to
  target a specific session, or omit it to use the most-recent session in the
  current working directory.
"""

_EVENT_SCAN_EPILOG = """\
EVENT TYPES
  user.prompt.submitted    A user prompt submission
  tool.call.requested      A tool invocation request
  tool.call.succeeded      A tool call that succeeded
  tool.call.failed         A tool call that failed
  llm.response             An LLM response
  vendor.raw               A vendor-specific raw event

FILTER SYNTAX
  key=value     Exact match on a payload field
  key=*         Field must exist
  key=!         Field must be absent/null
  Dot-paths supported: result.error=*
"""


class _GhFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that matches the gh/git style: ALL CAPS sections, USAGE prefix."""

    def start_section(self, heading: str | None) -> None:
        _renames = {"positional arguments": "ARGUMENTS", "options": "FLAGS"}
        super().start_section(_renames.get(heading or "", heading or ""))

    def add_arguments(self, actions: object) -> None:
        # Promote "ARGUMENTS" → "CORE COMMANDS" when the section holds subparsers.
        if any(isinstance(a, argparse._SubParsersAction) for a in actions):  # type: ignore[attr-defined]
            self._current_section.heading = "CORE COMMANDS"
        super().add_arguments(actions)  # type: ignore[arg-type]

    def format_help(self) -> str:
        text = super().format_help()
        # "usage:" → "USAGE\n " (gh style)
        text = re.sub(r"^usage:", "USAGE\n ", text, flags=re.MULTILINE)
        # Remove trailing colon from auto-generated section headers (e.g. "FLAGS:" → "FLAGS")
        text = re.sub(r"^([A-Z][A-Z ]+):$", r"\1", text, flags=re.MULTILINE)
        # Move FLAGS section to the end (after epilog content)
        lines = text.split("\n")
        flags_start = next((i for i, ln in enumerate(lines) if ln == "FLAGS"), None)
        if flags_start is not None:
            flags_end = len(lines)
            for i in range(flags_start + 1, len(lines)):
                if lines[i] and re.match(r"^[A-Z][A-Z ]+$", lines[i]):
                    flags_end = i
                    break
            if flags_end < len(lines):  # only reorder when there's content after FLAGS
                flags_block = lines[flags_start:flags_end]
                remaining = lines[:flags_start] + lines[flags_end:]
                while remaining and not remaining[-1].strip():
                    remaining.pop()
                text = "\n".join(remaining) + "\n\n" + "\n".join(flags_block).rstrip() + "\n"
        return text


def _add_trajectory_source(p: argparse.ArgumentParser) -> None:
    """Add optional TRAJECTORY_ID positional as the trajectory source."""
    p.add_argument(
        "trajectory_id",
        metavar="TRAJECTORY_ID",
        nargs="?",
        default=None,
        help="Trajectory ID to target. Omit to use the most-recent session.",
    )


def _add_base_output_flags(p: argparse.ArgumentParser) -> None:
    """Add --pretty and --output flags (for ID-based lookups that don't need scope)."""
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    p.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")
    p.set_defaults(global_scope=False)


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    """Add --pretty, --output, and --global-scope flags (for commands that need scope)."""
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    p.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")
    p.add_argument("--global-scope", action="store_true", help="Search all known log files instead of the most-recent session.")


def _add_agent_vendor_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--agent-vendor",
        metavar="AGENT_VENDOR",
        dest="agent_vendor",
        default=None,
        help=(
            "Filter by agent vendor. "
            "Known values: claude_code, codex_cli, gemini_cli, amp."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct",
        description="Inspect coding-session trajectories stored in JSONL log files.",
        usage="ct <command> <subcommand> [flags]",
        epilog=_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- project --------------------------------------------------------
    project_parser = subparsers.add_parser(
        "project",
        help="List projects or list trajectories within a project.",
        formatter_class=_GhFormatter,
    )
    project_sub = project_parser.add_subparsers(dest="action", required=True)

    project_list = project_sub.add_parser(
        "list",
        help="List all known projects.",
        formatter_class=_GhFormatter,
    )
    _add_agent_vendor_flag(project_list)
    _add_output_flags(project_list)
    project_list.set_defaults(
        _method="project.list",
        _params=_project_list_params,
    )

    project_trajectories = project_sub.add_parser(
        "trajectories",
        help="List trajectories for a given project.",
        formatter_class=_GhFormatter,
    )
    project_trajectories.add_argument(
        "project_name",
        metavar="PROJECT_NAME",
        help="Project name to list trajectories for.",
    )
    _add_agent_vendor_flag(project_trajectories)
    _add_output_flags(project_trajectories)
    project_trajectories.set_defaults(
        _method="trajectory.list",
        _params=_project_trajectories_params,
    )

    # -- trajectory -----------------------------------------------------
    traj_parser = subparsers.add_parser(
        "trajectory",
        help="Inspect a trajectory.",
        formatter_class=_GhFormatter,
    )
    traj_sub = traj_parser.add_subparsers(dest="action", required=True)

    traj_overview = traj_sub.add_parser(
        "overview",
        help="Show session structure, step types, and user requests.",
        formatter_class=_GhFormatter,
    )
    _add_trajectory_source(traj_overview)
    _add_output_flags(traj_overview)
    traj_overview.set_defaults(
        _method="trajectory.overview",
        _params=lambda args: {"trajectory_id": args.trajectory_id} if args.trajectory_id else {},
    )

    # -- step -----------------------------------------------------------
    step_parser = subparsers.add_parser(
        "step",
        help="Inspect one or more steps by ID.",
        formatter_class=_GhFormatter,
    )
    step_sub = step_parser.add_subparsers(dest="action", required=True)
    step_detail = step_sub.add_parser(
        "detail",
        help="Show full detail for one or more steps.",
        formatter_class=_GhFormatter,
    )
    step_detail.add_argument("resource_ids", metavar="STEP_ID", nargs="+")
    _add_base_output_flags(step_detail)
    step_detail.set_defaults(
        _method="step.details",
        _params=lambda args: {"step_ids": args.resource_ids},
    )

    # -- event ----------------------------------------------------------
    event_parser = subparsers.add_parser(
        "event",
        help="Inspect events by ID or scan by type.",
        formatter_class=_GhFormatter,
    )
    event_sub = event_parser.add_subparsers(dest="action", required=True)

    event_detail = event_sub.add_parser(
        "detail",
        help="Expand the full content of a single event (resolves $truncated refs).",
        formatter_class=_GhFormatter,
    )
    event_detail.add_argument("resource_id", metavar="EVENT_ID")
    _add_base_output_flags(event_detail)
    event_detail.set_defaults(
        _method="event.detail",
        _params=lambda args: {"event_id": args.resource_id},
    )

    event_scan = event_sub.add_parser(
        "scan",
        help="Query events matching --type and optional --filter expressions.",
        epilog=_EVENT_SCAN_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_trajectory_source(event_scan)
    _add_output_flags(event_scan)
    event_scan.add_argument(
        "--type",
        dest="event_type",
        required=True,
        metavar="TYPE",
        help="Event type to match (e.g. tool.call.succeeded, llm.response).",
    )
    event_scan.add_argument(
        "--filter",
        dest="filters",
        action="append",
        metavar="KEY=VALUE",
        default=[],
        help=(
            "Filter on event payload fields. Repeatable. "
            "VALUE=* means field must exist; VALUE=! means field must be absent."
        ),
    )
    event_scan.set_defaults(
        _method="event.scan",
        _params=lambda args: {
            "type": args.event_type,
            "filters": args.filters,
            **({"trajectory_id": args.trajectory_id} if args.trajectory_id else {}),
        },
    )

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
