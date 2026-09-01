from __future__ import annotations

import argparse

from datahub_plugin.projections.context_window import build_projection
from datahub_plugin.projections.context_window.render import render_markdown


def main(
    argv: list[str] | None = None,
    *,
    prog: str = "ct plugin datahub session context-window",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Inspect context composition and trajectory events for one session.",
    )
    parser.add_argument("session_id")
    parser.add_argument(
        "--turn",
        dest="turn_id",
        default=None,
        help="Limit the event timeline to one turn.",
    )
    parser.add_argument(
        "--output",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format. Defaults to markdown.",
    )
    args = parser.parse_args(argv)
    try:
        projection = build_projection(args.session_id, turn_id=args.turn_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        projection.model_dump_json(indent=2)
        if args.output == "json"
        else render_markdown(projection)
    )
    return 0
