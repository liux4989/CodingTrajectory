"""Command-line interface for reading coding session graph data."""

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


def _project_graphs_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.project_name:
        params["project_name"] = args.project_name
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _session_graph_turns_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.session_id:
        params["session_id"] = args.session_id
    if args.num_turns is not None:
        params["num_turns"] = args.num_turns
    if args.drop_turns is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _add_turn_window_flags(p: argparse.ArgumentParser, *, view_name: str) -> None:
    p.add_argument(
        "--turns",
        dest="num_turns",
        type=_positive_int,
        default=None,
        metavar="N",
        help=f"Limit each session {view_name} to its last N visible turns.",
    )
    p.add_argument(
        "--drop-turns",
        dest="drop_turns",
        type=_positive_int,
        default=None,
        metavar="K",
        help="Drop the last K visible turns, matching thread/rollback semantics.",
    )


_EPILOG = """\
NAVIGATE
  ct project list                                  list all known projects
  ct project graphs [PROJECT_NAME]                 list session graphs for a project
  ct session overview [SESSION_ID]                 session structure, step types, user requests
  ct session narrative [SESSION_ID]                deterministic turn narrative for summarizers
  ct graph metrics [SESSION_ID]                   token/quota/cost rollup by session graph hierarchy
  ct graph turns [SESSION_ID]                     token rollup by turn
  ct step detail STEP_ID [...]                     full detail for one or more steps

INSPECT COMMANDS
  ct event scan [SESSION_ID] --type TYPE [--filter KEY=VALUE]
                                                   query raw events by type
  ct event detail EVENT_ID                         expand $truncated fields from step details

NOTE
  Session graphs are located automatically via cache; pass a SESSION_ID to use
  that coding session as the graph entry point, or omit it to use the
  most-recent session in the current working directory.
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


def _add_session_graph_source(p: argparse.ArgumentParser) -> None:
    """Add optional SESSION_ID positional as the session graph entry point."""
    p.add_argument(
        "session_id",
        metavar="SESSION_ID",
        nargs="?",
        default=None,
        help="Session ID to use as the graph entry point. Omit to use the most-recent session.",
    )


def _add_base_output_flags(p: argparse.ArgumentParser) -> None:
    """Add --output flag (for ID-based lookups that don't need scope)."""
    p.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")
    p.set_defaults(global_scope=False)


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    """Add --output and --global-scope flags (for commands that need scope)."""
    p.add_argument("--output", "-o", metavar="FILE", dest="output_file", help="Write JSON output to FILE instead of stdout.")
    p.add_argument("--global-scope", action="store_true", help="Search all known log files instead of the most-recent session.")


def _add_metrics_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--extra-billing",
        action="store_true",
        help="Mark cost estimates as outside-plan/API billing instead of plan-usage estimates.",
    )


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
        description="Inspect coding-session graphs stored in JSONL log files.",
        usage="ct <command> <subcommand> [flags]",
        epilog=_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- project --------------------------------------------------------
    project_parser = subparsers.add_parser(
        "project",
        help="List projects or list session graphs within a project.",
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

    project_graphs = project_sub.add_parser(
        "graphs",
        help="List session graphs for a given project.",
        formatter_class=_GhFormatter,
    )
    project_graphs.add_argument(
        "project_name",
        metavar="PROJECT_NAME",
        nargs="?",
        default=None,
        help="Project name to list session graphs for. Defaults to the current directory.",
    )
    _add_agent_vendor_flag(project_graphs)
    _add_output_flags(project_graphs)
    project_graphs.set_defaults(
        _method="graph.list",
        _params=_project_graphs_params,
    )

    # -- session ---------------------------------------------------------
    session_parser = subparsers.add_parser(
        "session",
        help="Inspect a session and its connected graph.",
        formatter_class=_GhFormatter,
    )
    session_sub = session_parser.add_subparsers(dest="action", required=True)

    session_overview = session_sub.add_parser(
        "overview",
        help="Show session structure, step types, and user requests.",
        formatter_class=_GhFormatter,
    )
    _add_session_graph_source(session_overview)
    _add_turn_window_flags(session_overview, view_name="overview")
    _add_output_flags(session_overview)
    session_overview.set_defaults(
        _method="session.overview",
        _params=_session_graph_turns_params,
    )

    session_narrative = session_sub.add_parser(
        "narrative",
        help="Show deterministic user request, assistant response, and tool activity narrative.",
        formatter_class=_GhFormatter,
    )
    _add_session_graph_source(session_narrative)
    _add_turn_window_flags(session_narrative, view_name="narrative")
    _add_output_flags(session_narrative)
    session_narrative.set_defaults(
        _method="session.narrative",
        _params=_session_graph_turns_params,
    )

    # -- graph ----------------------------------------------------------
    graph_parser = subparsers.add_parser(
        "graph",
        help="Inspect session graph metrics and turn summaries.",
        formatter_class=_GhFormatter,
    )
    graph_sub = graph_parser.add_subparsers(dest="action", required=True)

    graph_metrics = graph_sub.add_parser(
        "metrics",
        help="Show token and quota metrics projected onto the session graph hierarchy.",
        formatter_class=_GhFormatter,
    )
    _add_session_graph_source(graph_metrics)
    _add_output_flags(graph_metrics)
    _add_metrics_flags(graph_metrics)
    graph_metrics.set_defaults(
        _method="metrics.session",
        _params=lambda args: {
            "extra_billing": args.extra_billing,
            **({"session_id": args.session_id} if args.session_id else {}),
        },
    )

    graph_turns = graph_sub.add_parser(
        "turns",
        help="Show token metrics summarized by turn.",
        formatter_class=_GhFormatter,
    )
    _add_session_graph_source(graph_turns)
    _add_output_flags(graph_turns)
    _add_metrics_flags(graph_turns)
    graph_turns.set_defaults(
        _method="metrics.turns",
        _params=lambda args: {
            "extra_billing": args.extra_billing,
            **({"session_id": args.session_id} if args.session_id else {}),
        },
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
    _add_session_graph_source(event_scan)
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
            **({"session_id": args.session_id} if args.session_id else {}),
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

    text = json.dumps(payload, indent=2, ensure_ascii=False)

    if args.output_file:
        Path(args.output_file).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
