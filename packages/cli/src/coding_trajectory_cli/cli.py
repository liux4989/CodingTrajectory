"""Command-line interface for reading coding session graph data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch, resolve_store


class _YamlDumper(yaml.SafeDumper):
    pass


def _yaml_string_representer(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_YamlDumper.add_representer(str, _yaml_string_representer)


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


def _project_sessions_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.project_name:
        params["project_name"] = args.project_name
    agent_vendor = getattr(args, "agent_vendor", None)
    if agent_vendor is not None:
        params["agent_vendor"] = agent_vendor
    return params


def _session_turn_window_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.session_id:
        params["session_id"] = args.session_id
    if args.num_turns is not None:
        params["num_turns"] = args.num_turns
    if args.drop_turns is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _session_overview_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "view": args.view,
        **_session_turn_window_params(args),
    }


def _session_stats_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "extra_billing": args.extra_billing,
        **({"session_id": args.session_id} if args.session_id else {}),
    }


def _session_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "extra_billing": args.extra_billing,
        **({"session_id": args.session_id} if args.session_id else {}),
        **({"turn_id": args.turn_id} if args.turn_id else {}),
    }


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
PROJECT
  ct project list                                  list all known projects
  ct project sessions [PROJECT_NAME]               list sessions for a project

SESSION
  ct session overview [SESSION_ID]                 compact session hierarchy
  ct session overview --view narrative [SESSION_ID]
                                                   deterministic activity narrative
  ct session stats [SESSION_ID]                    compact context/token usage overview
  ct session usage [SESSION_ID] [--turn TURN_ID]   turn-level activity cost and efficiency
  ct session step-detail STEP_ID [...]             full detail for one or more steps
  ct session event-scan [SESSION_ID] --type TYPE [--filter KEY=VALUE]
                                                   query raw events by type
  ct session event-detail EVENT_ID                 expand $truncated fields from step details

NOTE
  Sessions are located automatically via cache; pass a SESSION_ID to use
  that coding session as the session tree entry point, or omit it to use the
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


def _add_session_source(p: argparse.ArgumentParser) -> None:
    """Add optional SESSION_ID positional as the session tree entry point."""
    p.add_argument(
        "session_id",
        metavar="SESSION_ID",
        nargs="?",
        default=None,
        help="Session ID to use as the session tree entry point. Omit to use the most-recent session.",
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


def _add_format_flag(
    p: argparse.ArgumentParser,
    *,
    choices: tuple[str, ...] = ("json", "yaml"),
    default: str = "json",
) -> None:
    p.add_argument(
        "--format",
        choices=choices,
        default=default,
        help="Select stdout format. --output always writes JSON.",
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
        description="Inspect coding sessions stored in JSONL log files.",
        usage="ct <command> <subcommand> [flags]",
        epilog=_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- project --------------------------------------------------------
    project_parser = subparsers.add_parser(
        "project",
        help="List projects or sessions within a project.",
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
    _add_format_flag(project_list)
    project_list.set_defaults(
        _method="project.list",
        _params=_project_list_params,
    )

    project_sessions = project_sub.add_parser(
        "sessions",
        help="List sessions for a given project.",
        formatter_class=_GhFormatter,
    )
    project_sessions.add_argument(
        "project_name",
        metavar="PROJECT_NAME",
        nargs="?",
        default=None,
        help="Project name to list sessions for. Defaults to the current directory.",
    )
    _add_agent_vendor_flag(project_sessions)
    _add_output_flags(project_sessions)
    _add_format_flag(project_sessions)
    project_sessions.set_defaults(
        _method="project.sessions",
        _params=_project_sessions_params,
    )

    # -- session ---------------------------------------------------------
    session_parser = subparsers.add_parser(
        "session",
        help="Analyze a session and its connected session tree.",
        formatter_class=_GhFormatter,
    )
    session_sub = session_parser.add_subparsers(dest="action", required=True)

    session_overview = session_sub.add_parser(
        "overview",
        help="Show a compact session hierarchy or deterministic activity narrative.",
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_overview)
    session_overview.add_argument(
        "--view",
        choices=("overview", "narrative"),
        default="overview",
        help="Select the session analysis projection.",
    )
    _add_turn_window_flags(session_overview, view_name="projection")
    _add_output_flags(session_overview)
    _add_format_flag(session_overview)
    session_overview.set_defaults(
        _method="session.overview",
        _params=_session_overview_params,
    )

    session_stats = session_sub.add_parser(
        "stats",
        help="Show compact context/token usage composition.",
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_stats)
    _add_output_flags(session_stats)
    _add_format_flag(session_stats, choices=("text", "json", "yaml"), default="text")
    _add_metrics_flags(session_stats)
    session_stats.set_defaults(
        _method="session.stats",
        _params=_session_stats_params,
    )

    session_usage = session_sub.add_parser(
        "usage",
        help="Show turn-level activity cost and token efficiency.",
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_usage)
    session_usage.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit usage analysis to one turn.",
    )
    _add_output_flags(session_usage)
    _add_format_flag(session_usage, choices=("text", "json", "yaml"), default="text")
    _add_metrics_flags(session_usage)
    session_usage.set_defaults(
        _method="session.usage",
        _params=_session_usage_params,
    )

    session_step_detail = session_sub.add_parser(
        "step-detail",
        help="Show full detail for one or more steps.",
        formatter_class=_GhFormatter,
    )
    session_step_detail.add_argument("resource_ids", metavar="STEP_ID", nargs="+")
    _add_base_output_flags(session_step_detail)
    _add_format_flag(session_step_detail)
    session_step_detail.set_defaults(
        _method="step.details",
        _params=lambda args: {"step_ids": args.resource_ids},
    )

    session_event_detail = session_sub.add_parser(
        "event-detail",
        help="Expand the full content of a single event (resolves $truncated refs).",
        formatter_class=_GhFormatter,
    )
    session_event_detail.add_argument("resource_id", metavar="EVENT_ID")
    _add_base_output_flags(session_event_detail)
    _add_format_flag(session_event_detail)
    session_event_detail.set_defaults(
        _method="event.detail",
        _params=lambda args: {"event_id": args.resource_id},
    )

    session_event_scan = session_sub.add_parser(
        "event-scan",
        help="Query events matching --type and optional --filter expressions.",
        epilog=_EVENT_SCAN_EPILOG,
        formatter_class=_GhFormatter,
    )
    _add_session_source(session_event_scan)
    _add_output_flags(session_event_scan)
    _add_format_flag(session_event_scan)
    session_event_scan.add_argument(
        "--type",
        dest="event_type",
        required=True,
        metavar="TYPE",
        help="Event type to match (e.g. tool.call.succeeded, llm.response).",
    )
    session_event_scan.add_argument(
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
    session_event_scan.set_defaults(
        _method="event.scan",
        _params=lambda args: {
            "type": args.event_type,
            "filters": args.filters,
            **({"session_id": args.session_id} if args.session_id else {}),
        },
    )

    return parser


def _format_number(value: int | float) -> str:
    if isinstance(value, float):
        value = int(round(value))
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _format_money(value: float) -> str:
    return f"${value:.4f}" if value else "$0"


def _format_percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _usage_total_tokens(tokens: dict[str, Any]) -> int:
    return sum(value for _, value in _token_rows(tokens))


def _token_rows(tokens: dict[str, Any]) -> list[tuple[str, int]]:
    input_tokens = int(tokens.get("input_tokens") or 0)
    cached_input_tokens = int(tokens.get("cached_input_tokens") or 0)
    fresh_input_tokens = max(input_tokens - cached_input_tokens, 0)
    rows = [
        ("Fresh input", fresh_input_tokens),
        ("Cached input", cached_input_tokens),
        ("Output", int(tokens.get("output_tokens") or 0)),
        ("Reasoning output", int(tokens.get("reasoning_output_tokens") or 0)),
    ]
    return [(label, value) for label, value in rows if value]


def _first_model(payload: dict[str, Any]) -> str | None:
    for session in payload.get("sessions") or []:
        for turn in session.get("turns") or []:
            model = turn.get("model")
            if isinstance(model, str) and model:
                return model
    for turn in payload.get("turns") or []:
        model = turn.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _render_stats_text(payload: dict[str, Any]) -> str:
    tokens = payload.get("token_usage") if isinstance(payload.get("token_usage"), dict) else {}
    rows = _token_rows(tokens)
    used = _usage_total_tokens(tokens)
    denominator = used or 1
    lines = [f"Model: {_first_model(payload) or 'unknown'}", ""]
    for label, value in rows:
        share = value / denominator if denominator else 0
        lines.append(f"{label:<24} {_format_number(value):>8}  ({share * 100:.1f}%)")
    lines.append("")
    lines.append(f"Used: {_format_number(used)} tokens")
    cost = float(payload.get("cost") or 0)
    if cost:
        lines.append(f"Cost: {_format_money(cost)}")
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {warning}" for warning in warnings)
    return "\n".join(lines)


def _render_usage_text(payload: dict[str, Any]) -> str:
    turns = payload.get("turns") or []
    if not turns:
        return "No matching turns."

    if len(turns) == 1:
        turn = turns[0]
        tokens = turn.get("tokens") or {}
        efficiency = turn.get("efficiency") or {}
        lines = [
            f"Turn {turn.get('seq')}  {turn.get('activity')}",
            "",
        ]
        model = turn.get("model")
        if model:
            lines.append(f"{'Model':<18} {model}")
        lines.extend(
            [
                f"{'Input tokens':<18} {_format_number(int(tokens.get('input_tokens') or 0))}",
                f"{'Output tokens':<18} {_format_number(int(tokens.get('output_tokens') or 0))}",
                f"{'Cached input':<18} {_format_number(int(tokens.get('cached_input_tokens') or 0))}",
                f"{'Cache reuse':<18} {_format_percent(float(efficiency.get('cache_reuse_ratio') or 0))}",
                f"{'Output/1k input':<18} {float(efficiency.get('output_per_1k_input') or 0):.1f}",
                f"{'Cost':<18} {_format_money(float(turn.get('cost_usd') or 0))}",
            ]
        )
        drivers = turn.get("cost_drivers") or []
        if drivers:
            lines.append("")
            lines.append("Cost drivers")
            for driver in drivers:
                lines.append(f"{str(driver.get('kind') or '').replace('_', ' '):<18} {_format_money(float(driver.get('cost_usd') or 0))}")
        return "\n".join(lines)

    lines = [
        f"{'Turn':<5} {'Activity':<24} {'Input':>8} {'Output':>8} {'Cache':>8} {'Reuse':>7} {'Out/1k':>8} {'Cost':>10}"
    ]
    for turn in turns:
        tokens = turn.get("tokens") or {}
        efficiency = turn.get("efficiency") or {}
        lines.append(
            f"{str(turn.get('seq')):<5} "
            f"{str(turn.get('activity') or '')[:24]:<24} "
            f"{_format_number(int(tokens.get('input_tokens') or 0)):>8} "
            f"{_format_number(int(tokens.get('output_tokens') or 0)):>8} "
            f"{_format_number(int(tokens.get('cached_input_tokens') or 0)):>8} "
            f"{_format_percent(float(efficiency.get('cache_reuse_ratio') or 0)):>7} "
            f"{float(efficiency.get('output_per_1k_input') or 0):>8.1f} "
            f"{_format_money(float(turn.get('cost_usd') or 0)):>10}"
        )

    totals = payload.get("totals") or {}
    total_tokens = totals.get("tokens") if isinstance(totals.get("tokens"), dict) else {}
    total_efficiency = totals.get("efficiency") if isinstance(totals.get("efficiency"), dict) else {}
    lines.append(
        f"{'Total':<5} "
        f"{'':<24} "
        f"{_format_number(int(total_tokens.get('input_tokens') or 0)):>8} "
        f"{_format_number(int(total_tokens.get('output_tokens') or 0)):>8} "
        f"{_format_number(int(total_tokens.get('cached_input_tokens') or 0)):>8} "
        f"{_format_percent(float(total_efficiency.get('cache_reuse_ratio') or 0)):>7} "
        f"{float(total_efficiency.get('output_per_1k_input') or 0):>8.1f} "
        f"{_format_money(float(totals.get('cost_usd') or 0)):>10}"
    )
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {warning}" for warning in warnings)
    return "\n".join(lines)


def _render_payload(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    fmt = getattr(args, "format", "json")
    if fmt == "json":
        return json.dumps(payload, indent=2, ensure_ascii=False)
    if fmt == "yaml":
        json_compatible = json.loads(json.dumps(payload, ensure_ascii=False))
        return yaml.dump(json_compatible, Dumper=_YamlDumper, allow_unicode=True, sort_keys=False, width=120)
    if args._method == "session.stats":
        return _render_stats_text(payload)
    if args._method == "session.usage":
        return _render_usage_text(payload)
    return json.dumps(payload, indent=2, ensure_ascii=False)


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

    json_text = json.dumps(payload, indent=2, ensure_ascii=False)
    text = _render_payload(args, payload)

    if args.output_file:
        Path(args.output_file).write_text(json_text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
