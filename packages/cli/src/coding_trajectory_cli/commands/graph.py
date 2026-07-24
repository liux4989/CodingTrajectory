"""Explicit graph-level command registration and renderers."""

from __future__ import annotations

import argparse
from typing import Any

from coding_trajectory_cli._shared import (
    GhFormatter,
    add_output_flags,
    add_params_flag,
    add_session_source,
    add_turn_window_flags,
    params_from_json,
)
from coding_trajectory_cli.commands.session import (
    _render_session_overview_text,
    _render_session_stats_text,
    _render_session_usage_text,
)


def _graph_entry_params(args: argparse.Namespace) -> dict[str, Any]:
    params = params_from_json(args)
    if getattr(args, "session_id", None):
        params["session_id"] = args.session_id
    if getattr(args, "num_turns", None) is not None:
        params["num_turns"] = args.num_turns
    if getattr(args, "drop_turns", None) is not None:
        params["drop_turns"] = args.drop_turns
    return params


def _graph_usage_params(args: argparse.Namespace) -> dict[str, Any]:
    params = _graph_entry_params(args)
    if getattr(args, "turn_id", None):
        params["turn_id"] = args.turn_id
    return params


def _render_graph_overview_text(payload: dict[str, Any]) -> str:
    body = _render_session_overview_text(payload)
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
        help="Inspect a connected multi-session graph.",
        formatter_class=GhFormatter,
    )
    graph_sub = graph_parser.add_subparsers(dest="action", required=True)

    graph_overview = graph_sub.add_parser(
        "overview",
        prog="ct graph overview",
        help="Show all sessions and structural edges in a graph.",
        formatter_class=GhFormatter,
    )
    add_session_source(graph_overview)
    add_turn_window_flags(graph_overview, view_name="projection")
    add_output_flags(graph_overview)
    add_params_flag(graph_overview)
    graph_overview.set_defaults(
        _method="graph.overview",
        _params=_graph_entry_params,
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
    add_output_flags(graph_stats)
    add_params_flag(graph_stats)
    graph_stats.set_defaults(
        _method="graph.stats",
        _params=_graph_entry_params,
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
    add_output_flags(graph_usage)
    add_params_flag(graph_usage)
    graph_usage.set_defaults(
        _method="graph.usage",
        _params=_graph_usage_params,
        _default_output="markdown",
        _renderer=_render_session_usage_text,
    )
