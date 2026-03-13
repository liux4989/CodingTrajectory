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
        add_common_query_arguments(get_parser, default_view="summary")

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
    parser.add_argument("--view", choices=("summary", "pretty", "raw"), default=default_view)
    parser.add_argument("--json", action="store_true", help="Alias for --view raw.")
    parser.add_argument("--fields", help="Comma-separated fields to include in JSON output.")
    parser.add_argument("--no-truncate", action="store_true", help="Disable text truncation in summary/pretty views.")


def add_list_filters(parser: argparse.ArgumentParser, resource: str) -> None:
    if resource == "session":
        parser.add_argument("--trajectory-id")
    elif resource == "turn":
        parser.add_argument("--session-id")
    elif resource == "event":
        parser.add_argument("--session-id")
        parser.add_argument("--turn-id")


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
            resources = resolve_collection(store, args.resource, args)
            payload = render_collection(resources, args.view, no_truncate=args.no_truncate)
        else:
            raise ValueError(f"unsupported action: {args.action}")

        if args.fields:
            if args.view == "pretty":
                parser.error("--fields is only supported with summary or raw output")
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


def resolve_resource(store: DocumentStore, resource: str, raw_id: str) -> Trajectory | Session | Turn | Event:
    resource_id = UUID(raw_id)

    if resource == "trajectory":
        return store.get_trajectory(resource_id)
    if resource == "session":
        return store.get_session(resource_id)
    if resource == "turn":
        return store.get_turn(resource_id)
    if resource == "event":
        return store.get_event(resource_id)

    raise ValueError(f"unsupported resource: {resource}")


def resolve_collection(store: DocumentStore, resource: str, args: argparse.Namespace) -> list[Trajectory | Session | Turn | Event]:
    if resource == "trajectory":
        trajectories = list(store.trajectories.values())
        if not args.global_scope:
            current_project = normalize_project_key(Path.cwd().name)
            trajectories = [
                item
                for item in trajectories
                if item.project_identifier and normalize_project_key(item.project_identifier) == current_project
            ]
        return sorted(trajectories, key=lambda item: (item.project_identifier or "", str(item.trajectory_id)))

    if resource == "session":
        sessions = list(store.sessions.values())
        if args.trajectory_id:
            trajectory_id = UUID(args.trajectory_id)
            sessions = [item for item in sessions if item.trajectory_id == trajectory_id]
        return sorted(sessions, key=lambda item: (item.started_at, str(item.session_id)))

    if resource == "turn":
        turns = list(store.turns.values())
        if args.session_id:
            session_id = UUID(args.session_id)
            turns = [item for item in turns if item.session_id == session_id]
        return sorted(turns, key=lambda item: (item.started_at, str(item.turn_id)))

    if resource == "event":
        events = list(store.events.values())
        if args.session_id:
            session_id = UUID(args.session_id)
            events = [item for item in events if item.session_id == session_id]
        if args.turn_id:
            turn_id = UUID(args.turn_id)
            events = [item for item in events if item.turn_id == turn_id]
        return sorted(events, key=lambda item: (item.timestamp, str(item.event_id)))

    raise ValueError(f"unsupported resource: {resource}")


def render_item(resource: Trajectory | Session | Turn | Event, view: str, *, no_truncate: bool) -> dict[str, Any] | str:
    if isinstance(resource, Trajectory):
        if view == "raw":
            return resource.model_dump(mode="json")
        if view == "summary":
            return summarize_trajectory(resource)
        return pretty_trajectory(resource)

    if isinstance(resource, Session):
        if view == "raw":
            return resource.model_dump(mode="json")
        if view == "summary":
            return summarize_session(resource, no_truncate=no_truncate, include_timeline=True)
        return pretty_session(resource)

    if isinstance(resource, Turn):
        if view == "raw":
            return resource.model_dump(mode="json")
        if view == "summary":
            return summarize_turn(resource, no_truncate=no_truncate)
        return pretty_turn(resource, no_truncate=no_truncate)

    if view == "raw":
        return resource.model_dump(mode="json")
    if view == "summary":
        return summarize_event(resource, no_truncate=no_truncate)
    return pretty_event(resource, no_truncate=no_truncate)


def render_collection(
    resources: list[Trajectory | Session | Turn | Event],
    view: str,
    *,
    no_truncate: bool,
) -> list[dict[str, Any]] | str:
    if view == "raw":
        return [resource.model_dump(mode="json") for resource in resources]
    if view == "summary":
        return [summarize_collection_item(resource, no_truncate=no_truncate) for resource in resources]
    return pretty_collection(resources, no_truncate=no_truncate)


def summarize_trajectory(trajectory: Trajectory) -> dict[str, Any]:
    return prune_nones(
        {
            "id": str(trajectory.trajectory_id),
            "project": trajectory.project_identifier,
            "task": trajectory.task_reference,
            "session_count": len(trajectory.sessions),
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
    timeline: list[dict[str, Any]] = []

    for index, item in enumerate(session.timeline, start=1):
        if item.kind == "event":
            event = event_by_id.get(item.id)
            if event is None:
                timeline.append(prune_nones({"idx": index, "kind": "event", "id": str(item.id)}))
                continue
            timeline.append(summarize_session_event_item(event, idx=index, no_truncate=no_truncate))
            continue

        turn = turn_by_id.get(item.id)
        if turn is None:
            timeline.append(prune_nones({"idx": index, "kind": "turn", "id": str(item.id)}))
            continue
        timeline.append(
            summarize_session_turn_item(turn, idx=index, no_truncate=no_truncate, event_by_id=event_by_id)
        )

    return timeline


def summarize_session_turn_item(
    turn: Turn,
    *,
    idx: int,
    no_truncate: bool,
    event_by_id: dict[Any, Event],
) -> dict[str, Any]:
    preview = turn.user_request
    if preview and not no_truncate:
        preview = shorten_line(preview)

    events = [
        summarize_turn_event_item(event_by_id[event_id], idx=event_index, no_truncate=no_truncate)
        for event_index, event_id in enumerate(turn.event_ids, start=1)
        if event_id in event_by_id
    ]

    return prune_nones(
        {
            "idx": idx,
            "kind": "turn",
            "id": str(turn.turn_id),
            "started_at": format_datetime(turn.started_at),
            "preview": preview,
            "event_count": len(turn.event_ids),
            "events": events,
        }
    )


def summarize_session_event_item(event: Event, *, idx: int, no_truncate: bool) -> dict[str, Any]:
    entry = {
        "idx": idx,
        "kind": "event",
        "id": str(event.event_id),
        "timestamp": format_datetime(event.timestamp),
        "type": event.type.value,
        "actor": event.actor,
    }
    preview = timeline_payload_preview(event.payload, no_truncate=no_truncate)
    if preview:
        entry["payload_preview"] = preview
    return prune_nones(entry)


def summarize_turn_event_item(event: Event, *, idx: int, no_truncate: bool) -> dict[str, Any]:
    entry = {
        "idx": idx,
        "id": str(event.event_id),
        "timestamp": format_datetime(event.timestamp),
        "type": event.type.value,
        "actor": event.actor,
    }
    preview = timeline_payload_preview(event.payload, no_truncate=no_truncate)
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


def summarize_collection_item(resource: Trajectory | Session | Turn | Event, *, no_truncate: bool) -> dict[str, Any]:
    if isinstance(resource, Trajectory):
        return summarize_trajectory(resource)
    if isinstance(resource, Session):
        return summarize_session(resource, no_truncate=no_truncate, include_timeline=False)
    if isinstance(resource, Turn):
        return summarize_turn(resource, no_truncate=no_truncate)
    return summarize_event(resource, no_truncate=no_truncate)


def pretty_trajectory(trajectory: Trajectory) -> str:
    lines = [
        f"Trajectory  {trajectory.trajectory_id}",
        f"Project     {trajectory.project_identifier or '-'}",
        f"Task        {trajectory.task_reference or '-'}",
        f"Sessions    {len(trajectory.sessions)}",
    ]

    if trajectory.sessions:
        lines.append("")
        for index, session in enumerate(trajectory.sessions, start=1):
            lines.append(f"{index}. {session.session_id}")

    return "\n".join(lines)


def pretty_session(session: Session) -> str:
    event_count = sum(1 for item in session.timeline if item.kind == "event")
    turn_count = sum(1 for item in session.timeline if item.kind == "turn")

    return "\n".join(
        [
            f"Session     {session.session_id}",
            f"Trajectory  {session.trajectory_id}",
            f"Vendor      {session.vendor.value}",
            f"Status      {status_from_end(session.ended_at)}",
            f"Started     {format_datetime(session.started_at)}",
            f"Ended       {format_datetime(session.ended_at) or '-'}",
            f"Timeline    {len(session.timeline)} items ({event_count} events, {turn_count} turns)",
        ]
    )


def pretty_turn(turn: Turn, *, no_truncate: bool) -> str:
    request = turn.user_request or "-"
    if request != "-" and not no_truncate:
        request = shorten_line(request)

    return "\n".join(
        [
            f"Turn        {turn.turn_id}",
            f"Session     {turn.session_id}",
            f"Status      {status_from_end(turn.ended_at)}",
            f"Started     {format_datetime(turn.started_at)}",
            f"Ended       {format_datetime(turn.ended_at) or '-'}",
            f"Events      {len(turn.event_ids)}",
            f"Request     {request}",
        ]
    )


def pretty_event(event: Event, *, no_truncate: bool) -> str:
    lines = [
        f"Event       {event.event_id}",
        f"Type        {event.type.value}",
        f"Time        {format_datetime(event.timestamp)}",
        f"Actor       {event.actor or '-'}",
        f"Session     {event.session_id}",
        f"Turn        {event.turn_id or '-'}",
        f"Vendor      {event.vendor_source.value}",
        f"Source      {event.provenance.value}",
        f"Confidence  {event.confidence.value}",
    ]

    preview = payload_preview(event.payload, no_truncate=no_truncate)
    if preview:
        lines.append("")
        lines.append("Payload")
        for key, value in preview.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)


def pretty_collection(resources: list[Trajectory | Session | Turn | Event], *, no_truncate: bool) -> str:
    if not resources:
        return "No results."

    first = resources[0]
    if isinstance(first, Trajectory):
        headers = ["ID", "PROJECT", "TASK", "SESSIONS"]
        rows = [
            [
                str(item.trajectory_id),
                item.project_identifier or "-",
                item.task_reference or "-",
                str(len(item.sessions)),
            ]
            for item in resources
            if isinstance(item, Trajectory)
        ]
        return format_table(headers, rows)

    if isinstance(first, Session):
        headers = ["ID", "VENDOR", "STATUS", "STARTED", "TRAJECTORY"]
        rows = [
            [
                str(item.session_id),
                item.vendor.value,
                status_from_end(item.ended_at),
                format_datetime(item.started_at) or "-",
                str(item.trajectory_id),
            ]
            for item in resources
            if isinstance(item, Session)
        ]
        return format_table(headers, rows)

    if isinstance(first, Turn):
        headers = ["ID", "STATUS", "EVENTS", "STARTED", "PREVIEW"]
        rows = [
            [
                str(item.turn_id),
                status_from_end(item.ended_at),
                str(len(item.event_ids)),
                format_datetime(item.started_at) or "-",
                item.user_request if no_truncate else shorten_line(item.user_request or "-"),
            ]
            for item in resources
            if isinstance(item, Turn)
        ]
        return format_table(headers, rows)

    headers = ["TIME", "TYPE", "ACTOR", "SESSION", "TURN"]
    rows = [
        [
            format_datetime(item.timestamp) or "-",
            item.type.value,
            item.actor or "-",
            str(item.session_id),
            str(item.turn_id) if item.turn_id else "-",
        ]
        for item in resources
        if isinstance(item, Event)
    ]
    return format_table(headers, rows)


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


def write_output(payload: dict[str, Any] | list[dict[str, Any]] | str, view: str) -> None:
    if view == "pretty":
        print(payload)
    else:
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


def format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def shorten_line(text: str, width: int = 72) -> str:
    single_line = " ".join(text.splitlines())
    if len(single_line) <= width:
        return single_line
    return f"{single_line[: width - 3]}..."


def truncate_value(value: Any) -> Any:
    if isinstance(value, str):
        return shorten_line(value, width=48)
    return value


def prune_nones(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    formatted_rows = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in rows:
        formatted_rows.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(formatted_rows)


if __name__ == "__main__":
    raise SystemExit(main())
