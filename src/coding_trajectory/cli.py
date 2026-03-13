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
    parser.add_argument("--view", choices=("summary", "pretty", "raw"), default=default_view)
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


def resolve_collection(store: DocumentStore, resource: str, args: argparse.Namespace) -> list[Trajectory | Session]:
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
        if not args.trajectory_id and getattr(args, "parent_id", None):
            args.trajectory_id = args.parent_id
        if args.trajectory_id:
            trajectory_id = UUID(args.trajectory_id)
            sessions = [item for item in sessions if item.trajectory_id == trajectory_id]
        return sorted(sessions, key=lambda item: (item.started_at, str(item.session_id)))

    raise ValueError(f"unsupported resource: {resource}")


def render_item(resource: Trajectory | Session | Turn | Event, view: str, *, no_truncate: bool) -> dict[str, Any] | str:
    if isinstance(resource, Trajectory):
        if view == "raw":
            return serialize_trajectory_detail(resource)
        if view == "summary":
            return summarize_trajectory(resource)
        return pretty_trajectory(resource)

    if isinstance(resource, Session):
        if view == "raw":
            return serialize_session_detail(resource)
        if view == "summary":
            return summarize_session(resource, no_truncate=no_truncate, include_timeline=True)
        return pretty_session(resource)

    if isinstance(resource, Turn):
        if view == "raw":
            return serialize_turn_detail(resource)
        if view == "summary":
            return summarize_turn_detail(resource, no_truncate=no_truncate)
        return pretty_turn(resource, no_truncate=no_truncate)

    if view == "raw":
        return serialize_event_detail(resource)
    if view == "summary":
        return summarize_event(resource, no_truncate=no_truncate)
    return pretty_event(resource, no_truncate=no_truncate)


def render_collection(
    resources: list[Trajectory | Session],
    view: str,
    *,
    no_truncate: bool,
) -> list[dict[str, Any]] | str:
    if view == "raw":
        return [serialize_trajectory_detail(resource) if isinstance(resource, Trajectory) else serialize_session_detail(resource) for resource in resources]
    if view == "summary":
        return [summarize_collection_item(resource, no_truncate=no_truncate) for resource in resources]
    return pretty_collection(resources, no_truncate=no_truncate)


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


def serialize_trajectory_detail(trajectory: Trajectory) -> dict[str, Any]:
    return prune_nones(
        {
            "trajectory_id": str(trajectory.trajectory_id),
            "project_identifier": trajectory.project_identifier,
            "task_reference": trajectory.task_reference,
            "multi_agent_mode": trajectory.multi_agent_mode,
            "summary": trajectory.summary.model_dump(mode="json") if trajectory.summary else None,
            "session_ids": [str(session.session_id) for session in trajectory.sessions],
            "session_refs": [item.model_dump(mode="json") for item in trajectory.session_refs],
            "edges": [item.model_dump(mode="json") for item in trajectory.edges],
            "operations": [item.model_dump(mode="json") for item in trajectory.operations],
            "sections": [item.model_dump(mode="json") for item in trajectory.sections],
            "inference_notes": [item.model_dump(mode="json") for item in trajectory.inference_notes],
        }
    )


def serialize_session_detail(session: Session) -> dict[str, Any]:
    return prune_nones(
        {
            "session_id": str(session.session_id),
            "trajectory_id": str(session.trajectory_id),
            "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
            "vendor": session.vendor.value,
            "started_at": format_datetime(session.started_at),
            "ended_at": format_datetime(session.ended_at),
            "timeline": [serialize_timeline_item(item) for item in session.timeline],
            "extensions": session.extensions.model_dump(mode="json") if session.extensions else None,
        }
    )


def serialize_turn_detail(turn: Turn) -> dict[str, Any]:
    return prune_nones(
        {
            "turn_id": str(turn.turn_id),
            "session_id": str(turn.session_id),
            "user_request": turn.user_request,
            "started_at": format_datetime(turn.started_at),
            "ended_at": format_datetime(turn.ended_at),
            "event_ids": [str(event_id) for event_id in turn.event_ids],
        }
    )


def serialize_event_detail(event: Event) -> dict[str, Any]:
    return prune_nones(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "turn_id": str(event.turn_id) if event.turn_id else None,
            "timestamp": format_datetime(event.timestamp),
            "type": event.type.value,
            "vendor_source": event.vendor_source.value,
            "actor": event.actor,
            "provenance": event.provenance.value,
            "confidence": event.confidence.value,
            "payload": event.payload,
        }
    )


def serialize_timeline_item(item: Any) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "id": str(item.id),
    }


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


def pretty_trajectory(trajectory: Trajectory) -> str:
    summary = trajectory.summary
    lines = [
        f"Trajectory  {trajectory.trajectory_id}",
        f"Project     {trajectory.project_identifier or '-'}",
        f"Task        {trajectory.task_reference or '-'}",
        f"Mode        {trajectory.multi_agent_mode or '-'}",
        f"Sessions    {summary.session_count if summary else len(trajectory.sessions)}",
        f"Operations  {len(trajectory.operations)}",
        f"Sections    {len(trajectory.sections)}",
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


def pretty_collection(resources: list[Trajectory | Session], *, no_truncate: bool) -> str:
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
    raise ValueError(f"unsupported collection resource type: {type(first).__name__}")


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
