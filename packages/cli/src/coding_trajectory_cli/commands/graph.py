"""Explicit graph-level command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_output_flags,
    add_session_source,
    add_turn_window_flags,
)
from coding_trajectory_cli.commands.session import (
    _render_session_overview_text,
    _render_session_stats_text,
    _render_session_usage_text,
)


def _graph_entry_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": args.session_id}
    if getattr(args, "num_turns", None) is not None:
        params["num_turns"] = args.num_turns
    if getattr(args, "drop_turns", None) is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _include_flag(params: dict[str, Any], enabled: bool, value: str) -> dict[str, Any]:
    if enabled:
        params["include"] = [value]
    return params


def _graph_overview_params(args: argparse.Namespace) -> dict[str, Any]:
    return _include_flag(_graph_entry_params(args), args.narrative, "narrative")


def _graph_stats_params(args: argparse.Namespace) -> dict[str, Any]:
    return _include_flag(
        _graph_entry_params(args),
        args.session_composition,
        "session_composition",
    )


def _graph_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _graph_entry_params(args)
    if args.turn_id:
        params["turn_id"] = args.turn_id
    return _include_flag(params, args.flat_turns, "flat_turns")


def _render_graph_overview_text(payload: dict[str, Any]) -> str:
    body = _render_session_overview_text(payload).replace(
        "# Session `", "# Graph `", 1
    )
    orchestration = (payload.get("graph") or {}).get("orchestration") or {}
    if orchestration:
        kind = orchestration.get("kind") or "-"
        versions = ", ".join(orchestration.get("multi_agent_versions") or []) or "-"
        body = "\n".join(
            [
                body,
                "",
                f"Orchestration: `{kind}`; multi-agent versions: `{versions}`",
            ]
        )
    edges = payload.get("edges") or []
    lines = [body, "", "## Edges", ""]
    if not edges:
        lines.append("No structural edges observed.")
        return "\n".join(lines)
    lines.extend(
        ["| Type | Source | Target | Provenance |", "| --- | --- | --- | --- |"]
    )
    for edge in edges:
        lines.append(
            "| {type} | `{source_session_id}` | `{target_session_id}` | {provenance} |".format(
                type=edge.get("type") or "-",
                source_session_id=edge.get("source_session_id") or "-",
                target_session_id=edge.get("target_session_id") or "-",
                provenance=edge.get("provenance") or "-",
            )
        )
    return "\n".join(lines)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    graph_parser = subparsers.add_parser(
        "graph",
        prog="ct graph",
        usage="ct graph <command> [flags]",
        help="Inspect an orchestration graph.",
        formatter_class=GhFormatter,
    )
    graph_sub = graph_parser.add_subparsers(dest="action", required=True)

    graph_overview = graph_sub.add_parser(
        "overview",
        prog="ct graph overview",
        help="Show graph capabilities, sessions, and structural edges.",
        formatter_class=GhFormatter,
    )
    add_session_source(graph_overview)
    add_turn_window_flags(graph_overview, view_name="projection")
    graph_overview.add_argument(
        "--narrative",
        action="store_true",
        help="Include turn requests and assistant-response narrative.",
    )
    add_output_flags(graph_overview)
    graph_overview.set_defaults(
        _method="graph.overview",
        _params=_graph_overview_params,
        _default_output="markdown",
        _renderer=_render_graph_overview_text,
    )

    graph_stats = graph_sub.add_parser(
        "stats",
        prog="ct graph stats",
        help="Show aggregate context and token statistics for a graph.",
        formatter_class=GhFormatter,
    )
    add_session_source(graph_stats)
    graph_stats.add_argument(
        "--session-composition",
        action="store_true",
        help="Include per-session context and usage composition.",
    )
    add_output_flags(graph_stats)
    graph_stats.set_defaults(
        _method="graph.stats",
        _params=_graph_stats_params,
        _default_output="markdown",
        _renderer=_render_session_stats_text,
    )

    graph_usage = graph_sub.add_parser(
        "usage",
        prog="ct graph usage",
        help="Show aggregate turn-level token usage for a graph.",
        formatter_class=GhFormatter,
    )
    add_session_source(graph_usage)
    graph_usage.add_argument(
        "--turn",
        dest="turn_id",
        metavar="TURN_ID",
        default=None,
        help="Limit usage analysis to one turn.",
    )
    graph_usage.add_argument(
        "--flat-turns",
        action="store_true",
        help="Include the graph-wide flat turn list.",
    )
    add_output_flags(graph_usage)
    graph_usage.set_defaults(
        _method="graph.usage",
        _params=_graph_usage_params,
        _default_output="markdown",
        _renderer=_render_session_usage_text,
    )
