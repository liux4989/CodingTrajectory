"""CLI for querying canonical and enriched trajectory resources."""

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
        add_common_query_arguments(get_parser, default_view=_default_view_for(resource, "get"))

        if resource in ("trajectory", "session"):
            list_parser = resource_subparsers.add_parser("list")
            add_common_query_arguments(list_parser, default_view=_default_view_for(resource, "list"))
            add_list_filters(list_parser, resource)

    return parser


def _default_view_for(resource: str, action: str) -> str:
    if resource == "trajectory":
        return "pretty"
    return "raw"


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

        write_output(payload)
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

    if args.view == "raw":
        return _get_canonical_resource(client, resource, resource_id)

    if resource == "trajectory":
        return client.call("trajectory.enrich", {"trajectory_id": resource_id})

    raise ValueError(f"pretty view is not supported for {resource}. Use --view raw.")


def _handle_list(client: RpcClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    resource = args.resource

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

    if args.view == "raw":
        return items

    if resource == "trajectory":
        return [
            client.call("trajectory.enrich", {"trajectory_id": item["trajectory_id"]})
            for item in items
        ]

    raise ValueError(f"pretty view is not supported for {resource} list. Use --view raw.")


def _get_canonical_resource(client: RpcClient, resource: str, resource_id: str) -> dict[str, Any]:
    if resource == "trajectory":
        return client.call("trajectory.get", {"trajectory_id": resource_id})
    if resource == "session":
        return client.call("session.get", {"session_id": resource_id})
    if resource == "turn":
        return client.call("turn.get", {"turn_id": resource_id})
    if resource == "event":
        return client.call("event.get", {"event_id": resource_id})
    raise ValueError(f"unsupported resource: {resource}")


def _extract_trajectory_filter(args: argparse.Namespace) -> str | None:
    trajectory_id = getattr(args, "trajectory_id", None)
    if not trajectory_id and getattr(args, "parent_id", None):
        trajectory_id = args.parent_id
    return trajectory_id


def write_output(payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def select_output_fields(payload: dict[str, Any] | list[dict[str, Any]], fields: str) -> dict[str, Any] | list[dict[str, Any]]:
    if isinstance(payload, list):
        return [select_fields(item, fields) for item in payload]
    return select_fields(payload, fields)


def select_fields(payload: dict[str, Any], fields: str) -> dict[str, Any]:
    names = [field.strip() for field in fields.split(",") if field.strip()]
    return {name: payload[name] for name in names if name in payload}


if __name__ == "__main__":
    raise SystemExit(main())
