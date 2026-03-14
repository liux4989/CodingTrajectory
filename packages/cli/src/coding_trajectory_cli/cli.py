"""CLI for querying canonical trajectory resources."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from coding_trajectory_cli.rpc_client import RpcClient, RpcError

EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_DOCUMENT_ERROR = 4

# JSON-RPC error code ranges
_NOT_FOUND_CODES = {40400, 40401, 40402, 40403}


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
        with RpcClient(global_scope=bool(getattr(args, "global_scope", False))) as client:
            if args.action == "get":
                payload = _handle_get(client, args)
            elif args.action == "list":
                payload = _handle_list(client, args)
            else:
                raise ValueError(f"unsupported action: {args.action}")

        if args.fields:
            payload = select_output_fields(payload, args.fields)

        write_output(payload, args.view)
    except RpcError as exc:
        print(str(exc), file=sys.stderr)
        if exc.code in _NOT_FOUND_CODES:
            return EXIT_NOT_FOUND
        if exc.code == -32603:
            return EXIT_DOCUMENT_ERROR
        return EXIT_USAGE
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    return 0


def _handle_get(client: RpcClient, args: argparse.Namespace) -> dict[str, Any]:
    resource = args.resource
    resource_id = args.resource_id
    view = args.view
    no_truncate = args.no_truncate

    if resource == "trajectory":
        raw = client.call("trajectory.get", {"trajectory_id": resource_id})
        if view == "raw":
            return raw
        return summarize_trajectory(raw)

    if resource == "session":
        if view == "raw":
            return client.call("session.get", {"session_id": resource_id})
        bundle = client.call("session.bundle", {"session_id": resource_id})
        return summarize_session(bundle["session"], bundle=bundle, no_truncate=no_truncate, include_timeline=True)

    if resource == "turn":
        raw = client.call("turn.get", {"turn_id": resource_id})
        if view == "raw":
            return raw
        return summarize_turn_detail(raw, no_truncate=no_truncate)

    if resource == "event":
        raw = client.call("event.get", {"event_id": resource_id})
        if view == "raw":
            return raw
        return summarize_event(raw, no_truncate=no_truncate)

    raise ValueError(f"unsupported resource: {resource}")


def _handle_list(client: RpcClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    resource = args.resource
    view = args.view
    no_truncate = args.no_truncate

    if resource == "trajectory":
        result = client.call("trajectory.list", {})
    elif resource == "session":
        trajectory_id = _extract_trajectory_filter(args)
        params: dict[str, Any] = {}
        if trajectory_id:
            params["trajectory_id"] = trajectory_id
        result = client.call("session.list", params)
    else:
        raise ValueError(f"unsupported list resource: {resource}")

    items = result["items"]
    discovery_note = result.get("discovery_note", "")

    if discovery_note:
        print(discovery_note, file=sys.stderr)

    if view == "raw":
        return items

    if resource == "trajectory":
        return [summarize_trajectory(item) for item in items]
    return [summarize_session(item, no_truncate=no_truncate, include_timeline=False) for item in items]


def _extract_trajectory_filter(args: argparse.Namespace) -> str | None:
    trajectory_id = getattr(args, "trajectory_id", None)
    if not trajectory_id and getattr(args, "parent_id", None):
        trajectory_id = args.parent_id
    return trajectory_id


# ---------------------------------------------------------------------------
# Pretty rendering — works on plain dicts (JSON-RPC wire payloads)
# ---------------------------------------------------------------------------


def prune_nones(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.replace("+00:00", "Z")
    return value.isoformat().replace("+00:00", "Z")


def summarize_trajectory(raw: dict[str, Any]) -> dict[str, Any]:
    summary = raw.get("summary") or {}
    return prune_nones(
        {
            "id": raw["trajectory_id"],
            "project": raw.get("project_identifier"),
            "task": raw.get("task_reference"),
            "multi_agent_mode": raw.get("multi_agent_mode"),
            "session_count": summary.get("session_count", len(raw.get("session_ids", []))),
            "operation_count": len(raw.get("operations", [])),
            "section_count": len(raw.get("sections", [])),
            "session_ids": raw.get("session_ids", []),
        }
    )


def summarize_session(
    raw: dict[str, Any],
    *,
    no_truncate: bool = False,
    include_timeline: bool = False,
    bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeline_items = raw.get("timeline", [])

    if bundle:
        event_count = len(bundle.get("events", []))
        turn_count = len(bundle.get("turns", []))
    else:
        event_count = raw.get("event_count", 0)
        turn_count = raw.get("turn_count", 0)

    timeline = None
    if include_timeline and bundle:
        timeline = summarize_session_timeline(raw, bundle=bundle, no_truncate=no_truncate)

    summary: dict[str, Any] = {
        "id": raw["session_id"],
        "trajectory": raw.get("trajectory_id"),
        "parent": raw.get("parent_session_id"),
        "vendor": raw.get("vendor"),
        "started_at": format_datetime(raw.get("started_at")),
        "ended_at": format_datetime(raw.get("ended_at")),
        "status": status_from_end(raw.get("ended_at")),
        "timeline_count": len(timeline_items),
        "event_count": event_count,
        "turn_count": turn_count,
    }
    if include_timeline and timeline is not None:
        summary["timeline"] = timeline

    return prune_nones(summary)


def summarize_session_timeline(
    session_raw: dict[str, Any],
    *,
    bundle: dict[str, Any],
    no_truncate: bool,
) -> list[dict[str, Any]]:
    events_list = bundle.get("events", [])
    turns_list = bundle.get("turns", [])
    event_by_id = {e["event_id"]: e for e in events_list}
    turn_by_id = {t["turn_id"]: t for t in turns_list}

    tool_name_by_call_id, model_by_request_id = build_session_preview_context(events_list)
    timeline: list[dict[str, Any]] = []

    for index, item in enumerate(session_raw.get("timeline", []), start=1):
        kind = item["kind"]
        item_id = item["id"]

        if kind == "event":
            event = event_by_id.get(item_id)
            if event is None:
                timeline.append(prune_nones({"idx": index, "kind": "event", "id": item_id}))
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

        turn = turn_by_id.get(item_id)
        if turn is None:
            timeline.append(prune_nones({"idx": index, "kind": "turn", "id": item_id}))
            continue
        timeline.append(
            summarize_session_turn_item(
                turn,
                idx=index,
                no_truncate=no_truncate,
            )
        )

    return timeline


def build_session_preview_context(events: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    tool_name_by_call_id: dict[str, str] = {}
    model_by_request_id: dict[str, str] = {}

    for event in events:
        payload = event.get("payload", {})
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
    turn: dict[str, Any],
    *,
    idx: int,
    no_truncate: bool,
) -> dict[str, Any]:
    preview = turn.get("user_request")
    if preview and not no_truncate:
        preview = shorten_line(preview)

    return prune_nones(
        {
            "idx": idx,
            "kind": "turn",
            "id": turn["turn_id"],
            "started_at": format_datetime(turn.get("started_at")),
            "preview": preview,
            "event_count": len(turn.get("event_ids", [])),
        }
    )


def summarize_session_event_item(
    event: dict[str, Any],
    *,
    idx: int,
    no_truncate: bool,
    tool_name_by_call_id: dict[str, str],
    model_by_request_id: dict[str, str],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "idx": idx,
        "kind": "event",
        "id": event["event_id"],
        "timestamp": format_datetime(event.get("timestamp")),
        "type": event.get("type"),
        "actor": event.get("actor"),
    }
    preview = timeline_payload_preview(
        enrich_preview_payload(
            event.get("payload", {}),
            tool_name_by_call_id=tool_name_by_call_id,
            model_by_request_id=model_by_request_id,
        ),
        no_truncate=no_truncate,
    )
    if preview:
        entry["payload_preview"] = preview
    return prune_nones(entry)


def summarize_turn(turn: dict[str, Any], *, no_truncate: bool) -> dict[str, Any]:
    preview = turn.get("user_request")
    if preview and not no_truncate:
        preview = shorten_line(preview)

    return prune_nones(
        {
            "id": turn["turn_id"],
            "session": turn.get("session_id"),
            "user_request": turn.get("user_request"),
            "started_at": format_datetime(turn.get("started_at")),
            "ended_at": format_datetime(turn.get("ended_at")),
            "status": status_from_end(turn.get("ended_at")),
            "event_count": len(turn.get("event_ids", [])),
            "preview": preview,
        }
    )


def summarize_turn_detail(turn: dict[str, Any], *, no_truncate: bool) -> dict[str, Any]:
    summary = summarize_turn(turn, no_truncate=no_truncate)
    summary["event_ids"] = turn.get("event_ids", [])
    return summary


def summarize_event(event: dict[str, Any], *, no_truncate: bool) -> dict[str, Any]:
    preview = payload_preview(event.get("payload", {}), no_truncate=no_truncate)

    return prune_nones(
        {
            "id": event["event_id"],
            "session": event.get("session_id"),
            "turn": event.get("turn_id"),
            "timestamp": format_datetime(event.get("timestamp")),
            "type": event.get("type"),
            "actor": event.get("actor"),
            "vendor": event.get("vendor_source"),
            "provenance": event.get("provenance"),
            "confidence": event.get("confidence"),
            "payload_preview": preview,
        }
    )


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
