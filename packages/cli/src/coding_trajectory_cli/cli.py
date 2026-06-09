"""Command-line interface for reading coding session graph data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import IndexCache, dispatch, project_list_metadata, resolve_store
from coding_trajectory_cli._shared import (
    GhFormatter,
    add_output_flags,
    compact_payload,
    json_text,
    render_markdown_for_terminal,
    selected_output,
)
from coding_trajectory_cli.commands import REGISTRARS, dispatch_plugin_argv

EPILOG = """\
PROJECT
  ct project list                                  list all known projects
  ct project sessions [PROJECT_NAME]               list sessions for a project

SESSION
  ct session overview [SESSION_ID]                 compact session hierarchy
  ct session stats [SESSION_ID]                    compact context/token usage overview
  ct session usage [SESSION_ID] [--turn TURN_ID]   turn-level token and cost accounting
  ct session step-detail STEP_ID [...]             full detail for one or more steps
  ct session event-scan [SESSION_ID] --type TYPE [--filter KEY=VALUE]
                                                   query raw events by type
  ct session event-detail EVENT_ID                 expand $truncated fields from step details

NOTE
  Sessions are located automatically via cache; pass a SESSION_ID to use
  that coding session as the session tree entry point, or omit it to use the
  most-recent session in the current working directory.
"""


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    current_dir = Path.cwd()

    method: str = args._method
    params: dict[str, Any] = args._params(args)

    effective_global_scope = True if method == "project.list" else args.global_scope
    if method == "project.list":
        return project_list_metadata(
            params,
            global_scope=effective_global_scope,
            current_dir=current_dir,
        )

    cache = IndexCache.load()
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct",
        description="Inspect coding sessions stored in JSONL log files.",
        usage="ct <command> <subcommand> [flags]",
        epilog=EPILOG,
        formatter_class=GhFormatter,
    )
    add_output_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)
    for register in REGISTRARS:
        register(subparsers)
    return parser


def _render_payload(args: argparse.Namespace, payload: Any) -> str:
    plugin_renderer = getattr(args, "_render_payload", None)
    if callable(plugin_renderer):
        return plugin_renderer(args, payload)

    method = getattr(args, "_method", None)
    if selected_output(args) == "json":
        return json_text(compact_payload(method, payload)) if method else json_text(payload)

    renderer = getattr(args, "_renderer", None)
    if callable(renderer):
        return renderer(payload)

    return json_text(compact_payload(method, payload)) if method else json_text(payload)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    plugin_exit = dispatch_plugin_argv(raw_args)
    if plugin_exit is not None:
        return plugin_exit

    parser = _build_parser()
    args = parser.parse_args(raw_args)

    try:
        plugin_handler = getattr(args, "_plugin_handler", None)
        payload = plugin_handler(args) if callable(plugin_handler) else _dispatch(args)
        if isinstance(payload, int):
            return payload
    except ResourceNotFoundError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    except DocumentError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI fallback
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1

    output = _render_payload(args, payload)
    if selected_output(args) == "json":
        print(output)
    else:
        print(render_markdown_for_terminal(output))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
