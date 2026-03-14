"""CLI for querying canonical trajectory resources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from coding_trajectory.discovery import discover_store, format_discovery_sources, normalize_project_key
from coding_trajectory.ingestion.models import Event, Session, Trajectory, Turn
from coding_trajectory.query import DocumentError, DocumentStore, ResourceNotFoundError
from coding_trajectory.service import (
    format_datetime,
    prune_nones,
    resolve_collection,
    resolve_resource,
    serialize_event_detail,
    serialize_session_detail,
    serialize_timeline_item,
    serialize_trajectory_detail,
    serialize_turn_detail,
)

EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_DOCUMENT_ERROR = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-trajectory")
    subparsers = parser.add_subparsers(dest="resource", required=True)

    for resource in ("trajectory", "session", "turn", "event"):
        resource_parser = subparsers.add_parser(resource)
        resource_subparsers = resource_parser.add_subparsers(dest="action", required=True)

        get_parser = resource_subparsers.add_parser("get")
        get_parser.add_argument("resource_id")
        add_common_query_arguments(get_parser, default_view="pretty")

        if resource in ("trajectory", "session"):
            list_parser = resource_subparsers.add_parser("list")
            add_common_query_arguments(list_parser, default_view="pretty")
            add_list_filters(list_parser, resource)

    return parser


def add_common_query_arguments(parser: argparse.ArgumentParser, *, default_view: str) -> None:
    parser.add_argument(
        "-g",
        "--global",
        dest="global_scope",
        action="store_true",
        help="Search across all projects instead of scoping to the current project.",
    )
    parser.add_argument("--view", choices=("pretty", "raw"), default=default_view)
    parser.add_argument("--json", action="store_true", help="Alias for --view raw.")
    parser.add_argument("--fields", help="Comma-separated fields to include in JSON output.")
    parser.add_argument("--no-truncate", action="store_true", help="Disable text truncation in summary/pretty views.")


def add_list_filters(parser: argparse.ArgumentParser, resource: str) -> None:
    if resource == "session":
        parser.add_argument("parent_id", nargs="?", default=None, help="Shorthand for --trajectory-id.")
        parser.add_argument("--trajectory-id")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.json:
        args.view = "raw"

    try:
        discovery = discover_store(
            current_dir=Path.cwd(),
            global_scope=bool(getattr(args, "global_scope", False)),
        )
        store = discovery.store
        discovery_note = format_discovery_sources(discovery.sources)

        if args.action == "get":
            resource = resolve_resource(store, args.resource, args.resource_id)
            payload = render_item(resource, args.view, no_truncate=args.no_truncate)
        elif args.action == "list":
            trajectory_id = _extract_trajectory_filter(args)
            resources = resolve_collection(
                store,
                args.resource,
                global_scope=args.global_scope,
                trajectory_id=trajectory_id,
                current_dir=Path.cwd(),
            )
            payload = render_collection(resources, args.view, no_truncate=args.no_truncate)
        else:
            raise ValueError(f"unsupported action: {args.action}")

        if args.fields:
            payload = select_output_fields(payload, args.fields)

        if discovery_note and args.action == "list":
            print(discovery_note, file=sys.stderr)
        write_output(payload, args.view)
    except ResourceNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_FOUND
    except DocumentError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_DOCUMENT_ERROR
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    return 0


def _extract_trajectory_filter(args: argparse.Namespace) -> str | None:
    trajectory_id = getattr(args, "trajectory_id", None)
    if not trajectory_id and getattr(args, "parent_id", None):
        trajectory_id = args.parent_id
    return trajectory_id


def render_item(resource: Trajectory | Session | Turn | Event, view: str, *, no_truncate: bool) -> dict[str, Any]:
    if isinstance(resource, Trajectory):
        if view == "raw":
            return serialize_trajectory_detail(resource)
        return summarize_trajectory(resource)

    if isinstance(resource, Session):
        if view == "raw":
            return serialize_session_detail(resource)
        return summarize_session(resource, no_truncate=no_truncate, include_timeline=True)

    if isinstance(resource, Turn):
        if view == "raw":
            return serialize_turn_detail(resource)
        return summarize_turn_detail(resource, no_truncate=no_truncate)

    if view == "raw":
        return serialize_event_detail(resource)
    return summarize_event(resource, no_truncate=no_truncate)


def render_collection(
    resources: list[Trajectory | Session],
    view: str,
    *,
    no_truncate: bool,
) -> list[dict[str, Any]]:
    if view == "raw":
        return [serialize_trajectory_detail(resource) if isinstance(resource, Trajectory) else serialize_session_detail(resource) for resource in resources]
    return [summarize_collection_item(resource, no_truncate=no_truncate) for resource in resources]


def summarize_trajectory(trajectory: Trajectory) -> dict[str, Any]:
    summary = trajectory.summary.model_dump(mode="json") if trajectory.summary else {}
    return prune_nones(
        {
            "id": str(trajectory.trajectory_id),
            "project": trajectory.project_identifier,
            "task": trajectory.task_reference,
            "multi_agent_mode": trajectory.multi_agent_mode,
            "session_count": summary.get("session_count", len(trajectory.sessions)),
            "operation_count": len(trajectory.operations),
            "section_count": len(trajectory.sections),
            "session_ids": [str(session.session_id) for session in trajectory.sessions],
        }
    )


def summarize_session(session: Session, *, no_truncate: bool, include_timeline: bool) -> dict[str, Any]:
    event_count = len(session.events)
    turn_count = len(session.turns)
    timeline = summarize_session_timeline(session, no_truncate=no_truncate) if include_timeline else None

    summary = {
        "id": str(session.session_id),
        "trajectory": str(session.trajectory_id),
        "parent": str(session.parent_session_id) if session.parent_session_id else None,
        "vendor": session.vendor.value,
        "started_at": format_datetime(session.started_at),
        "ended_at": format_datetime(session.ended_at),
        "status": status_from_end(session.ended_at),
        "timeline_count": len(session.timeline),
        "event_count": event_count,
        "turn_count": turn_count,
    }
    if include_timeline:
        summary["timeline"] = timeline

    return prune_nones(summary)


def summarize_session_timeline(session: Session, *, no_truncate: bool) -> list[dict[str, Any]]:
    event_by_id = {event.event_id: event for event in session.events}
    turn_by_id = {turn.turn_id: turn for turn in session.turns}
    tool_name_by_call_id, model_by_request_id = build_session_preview_context(session.events)
    timeline: list[dict[str, Any]] = []

    for index, item in enumerate(session.timeline, start=1):
        if item.kind == "event":
            event = event_by_id.get(item.id)
            if event is None:
                timeline.append(prune_nones({"idx": index, "kind": "event", "id": str(item.id)}))
                continue
            timeline.append(
                summarize_session_event_item(
                    event,
                    idx=index,
                    no_truncate=no_truncate,
                    tool_name_by_call_id=tool_name_by_call_id,
                    model_by_request_id=model_by_request_id,
                )
            )
            continue

        turn = turn_by_id.get(item.id)
        if turn is None:
            timeline.append(prune_nones({"idx": index, "kind": "turn", "id": str(item.id)}))
            continue
        timeline.append(
            summarize_session_turn_item(
                turn,
                idx=index,
                no_truncate=no_truncate,
            )
        )

    return timeline


def build_session_preview_context(events: list[Event]) -> tuple[dict[str, str], dict[str, str]]:
    tool_name_by_call_id: dict[str, str] = {}
    model_by_request_id: dict[str, str] = {}

    for event in events:
        payload = event.payload
        tool_call_id = payload.get("tool_call_id")
        tool_name = payload.get("tool_name")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            tool_name_by_call_id.setdefault(tool_call_id, tool_name)

        request_id = payload.get("request_id")
        model = payload.get("model")
        if isinstance(request_id, str) and isinstance(model, str):
            model_by_request_id.setdefault(request_id, model)

    return tool_name_by_call_id, model_by_request_id


def enrich_preview_payload(
    payload: dict[str, Any],
    *,
    tool_name_by_call_id: dict[str, str],
    model_by_request_id: dict[str, str],
) -> dict[str, Any]:
    enriched = payload

    tool_call_id = payload.get("tool_call_id")
    if "tool_name" not in payload and isinstance(tool_call_id, str):
        tool_name = tool_name_by_call_id.get(tool_call_id)
        if tool_name:
            enriched = {**enriched, "tool_name": tool_name}

    request_id = payload.get("request_id")
    if "model" not in enriched and isinstance(request_id, str):
        model = model_by_request_id.get(request_id)
        if model:
            enriched = {**enriched, "model": model}

    return enriched


def summarize_session_turn_item(
    turn: Turn,
    *,
    idx: int,
    no_truncate: bool,
) -> dict[str, Any]:
    preview = turn.user_request
    if preview and not no_truncate:
        preview = shorten_line(preview)

    return prune_nones(
        {
            "idx": idx,
            "kind": "turn",
            "id": str(turn.turn_id),
            "started_at": format_datetime(turn.started_at),
            "preview": preview,
            "event_count": len(turn.event_ids),
        }
    )


def summarize_session_event_item(
    event: Event,
    *,
    idx: int,
    no_truncate: bool,
    tool_name_by_call_id: dict[str, str],
    model_by_request_id: dict[str, str],
) -> dict[str, Any]:
    entry = {
        "idx": idx,
        "kind": "event",
        "id": str(event.event_id),
        "timestamp": format_datetime(event.timestamp),
        "type": event.type.value,
        "actor": event.actor,
    }
    preview = timeline_payload_preview(
        enrich_preview_payload(
            event.payload,
            tool_name_by_call_id=tool_name_by_call_id,
            model_by_request_id=model_by_request_id,
        ),
        no_truncate=no_truncate,
    )
    if preview:
        entry["payload_preview"] = preview
    return prune_nones(entry)


def summarize_turn(turn: Turn, *, no_truncate: bool) -> dict[str, Any]:
    preview = turn.user_request
    if preview and not no_truncate:
        preview = shorten_line(preview)

    return prune_nones(
        {
            "id": str(turn.turn_id),
            "session": str(turn.session_id),
            "user_request": turn.user_request,
            "started_at": format_datetime(turn.started_at),
            "ended_at": format_datetime(turn.ended_at),
            "status": status_from_end(turn.ended_at),
            "event_count": len(turn.event_ids),
            "preview": preview,
        }
    )


def summarize_turn_detail(turn: Turn, *, no_truncate: bool) -> dict[str, Any]:
    summary = summarize_turn(turn, no_truncate=no_truncate)
    summary["event_ids"] = [str(event_id) for event_id in turn.event_ids]
    return summary


def summarize_event(event: Event, *, no_truncate: bool) -> dict[str, Any]:
    preview = payload_preview(event.payload, no_truncate=no_truncate)

    return prune_nones(
        {
            "id": str(event.event_id),
            "session": str(event.session_id),
            "turn": str(event.turn_id) if event.turn_id else None,
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "actor": event.actor,
            "vendor": event.vendor_source.value,
            "provenance": event.provenance.value,
            "confidence": event.confidence.value,
            "payload_preview": preview,
        }
    )


def summarize_collection_item(resource: Trajectory | Session, *, no_truncate: bool) -> dict[str, Any]:
    if isinstance(resource, Trajectory):
        return summarize_trajectory(resource)
    return summarize_session(resource, no_truncate=no_truncate, include_timeline=False)


def payload_preview(payload: dict[str, Any], *, no_truncate: bool) -> dict[str, Any]:
    preview_keys = (
        "tool_name",
        "tool_call_id",
        "status",
        "model",
        "request_id",
        "input_tokens",
        "output_tokens",
        "decision",
        "scope",
    )

    preview: dict[str, Any] = {}
    for key in preview_keys:
        if key in payload:
            preview[key] = payload[key]
        if len(preview) == 4:
            break

    if not preview:
        for key, value in payload.items():
            preview[key] = value
            if len(preview) == 4:
                break

    if no_truncate:
        return preview

    return {key: truncate_value(value) for key, value in preview.items()}


def timeline_payload_preview(payload: dict[str, Any], *, no_truncate: bool) -> dict[str, Any] | None:
    preview_keys = {
        "tool_name",
        "tool_call_id",
        "status",
        "model",
        "request_id",
        "input_tokens",
        "output_tokens",
        "decision",
        "scope",
    }
    if not any(key in payload for key in preview_keys):
        return None
    return payload_preview(payload, no_truncate=no_truncate)


def write_output(payload: dict[str, Any] | list[dict[str, Any]], view: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def select_output_fields(payload: dict[str, Any] | list[dict[str, Any]], fields: str) -> dict[str, Any] | list[dict[str, Any]]:
    if isinstance(payload, list):
        return [select_fields(item, fields) for item in payload]
    return select_fields(payload, fields)


def select_fields(payload: dict[str, Any], fields: str) -> dict[str, Any]:
    names = [field.strip() for field in fields.split(",") if field.strip()]
    return {name: payload[name] for name in names if name in payload}


def status_from_end(ended_at: object) -> str:
    return "completed" if ended_at is not None else "in_progress"


def shorten_line(text: str, width: int = 72) -> str:
    single_line = " ".join(text.splitlines())
    if len(single_line) <= width:
        return single_line
    return f"{single_line[: width - 3]}..."


def truncate_value(value: Any) -> Any:
    if isinstance(value, str):
        return shorten_line(value, width=48)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
