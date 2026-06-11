"""Structured service API command for plugin and automation callers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.contracts import command_schema, service_contract
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import (
    IndexCache,
    dispatch,
    project_list_metadata,
    resolve_store,
)
from coding_trajectory_cli._shared import GhFormatter, add_params_flag


def _request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "id": args.request_id,
        "method": args.method,
        "params": dict(args.params_json or {}),
    }


def _read_batch_requests(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.requests_json is not None:
        raw = args.requests_json
    elif args.input == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.input).read_text(encoding="utf-8")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("batch input must be a JSON array")
    for item in parsed:
        if not isinstance(item, dict):
            raise argparse.ArgumentTypeError("batch items must be JSON objects")
    return parsed


def _entrypoint_ids(requests: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for request in requests:
        params = request.get("params") or {}
        if not isinstance(params, dict):
            continue
        for key in ("session_id", "root_session_id", "turn_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                ids.append(value)
        session_ids = params.get("session_ids")
        if isinstance(session_ids, list):
            ids.extend(
                value for value in session_ids if isinstance(value, str) and value
            )
    return list(dict.fromkeys(ids))


class _ApiRuntime:
    def __init__(self, *, global_scope: bool, current_dir: Path) -> None:
        self.global_scope = global_scope
        self.current_dir = current_dir
        self.cache = IndexCache.load()
        self._shared_store: tuple[Any, str] | None = None

    def prepare_batch(self, requests: list[dict[str, Any]]) -> None:
        ids = _entrypoint_ids(requests)
        if not ids:
            return
        self._shared_store = resolve_store(
            {"session_ids": ids},
            log_file=None,
            global_scope=True,
            current_dir=self.current_dir,
            cache=self.cache,
        )

    def finish(self) -> None:
        self.cache.save()

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str) or not method:
            return _error_item(request_id, method, "method is required")
        if not isinstance(params, dict):
            return _error_item(request_id, method, "params must be an object")

        try:
            contract = service_contract(method)
            validated_params = contract.validate_request(params)
            if method == "project.list":
                result = contract.validate_response(
                    project_list_metadata(
                        validated_params,
                        global_scope=True,
                        current_dir=self.current_dir,
                    )
                )
            else:
                store, discovery_note = self._store_for(validated_params)
                result = dispatch(
                    method,
                    validated_params,
                    store=store,
                    global_scope=self.global_scope,
                    current_dir=self.current_dir,
                    discovery_note=discovery_note,
                    cache=self.cache,
                )
        except (KeyError, ValueError, ResourceNotFoundError, DocumentError) as exc:
            return _error_item(request_id, method, str(exc))
        return {
            "id": request_id,
            "method": method,
            "ok": True,
            "result": result,
        }

    def _store_for(self, params: dict[str, Any]) -> tuple[Any, str]:
        if self._shared_store is not None and _entrypoint_ids(
            [{"params": params}]
        ):
            return self._shared_store
        return resolve_store(
            params,
            log_file=None,
            global_scope=self.global_scope,
            current_dir=self.current_dir,
            cache=self.cache,
        )


def _error_item(request_id: Any, method: Any, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": method,
        "ok": False,
        "error": {"message": message},
    }


def _handle_api_call(args: argparse.Namespace) -> dict[str, Any]:
    runtime = _ApiRuntime(global_scope=args.global_scope, current_dir=Path.cwd())
    try:
        return runtime.execute(_request_from_args(args))
    finally:
        runtime.finish()


def _handle_api_batch(args: argparse.Namespace) -> dict[str, Any]:
    requests = _read_batch_requests(args)
    runtime = _ApiRuntime(global_scope=args.global_scope, current_dir=Path.cwd())
    try:
        runtime.prepare_batch(requests)
        return {"items": [runtime.execute(request) for request in requests]}
    finally:
        runtime.finish()


def _handle_api_schema(args: argparse.Namespace) -> dict[str, Any]:
    return command_schema(args.method, command=f"ct api call {args.method}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    api_parser = subparsers.add_parser(
        "api",
        prog="ct api",
        usage="ct api <command> [flags]",
        help="Call versioned service methods for plugins and automation.",
        formatter_class=GhFormatter,
    )
    api_sub = api_parser.add_subparsers(dest="action", required=True)

    call = api_sub.add_parser(
        "call",
        prog="ct api call",
        help="Call one versioned service method.",
        formatter_class=GhFormatter,
    )
    call.add_argument("method", metavar="METHOD", help="Service method name.")
    call.add_argument(
        "--id",
        dest="request_id",
        default=None,
        help="Request id echoed in the response.",
    )
    call.add_argument(
        "--global-scope",
        action="store_true",
        help="Search all known log files.",
    )
    add_params_flag(call)
    call.set_defaults(
        _plugin_handler=_handle_api_call,
        _default_output="json",
    )

    batch = api_sub.add_parser(
        "batch",
        prog="ct api batch",
        help="Call multiple versioned service methods from a JSON array.",
        formatter_class=GhFormatter,
    )
    batch.add_argument(
        "--input",
        "-i",
        default="-",
        help="Read request array from a file, or '-' for stdin.",
    )
    batch.add_argument(
        "--requests",
        dest="requests_json",
        default=None,
        help="Inline JSON request array.",
    )
    batch.add_argument(
        "--global-scope",
        action="store_true",
        help="Search all known log files.",
    )
    batch.set_defaults(
        _plugin_handler=_handle_api_batch,
        _default_output="json",
    )

    schema = api_sub.add_parser(
        "schema",
        prog="ct api schema",
        help="Print the JSON schema for one service method.",
        formatter_class=GhFormatter,
    )
    schema.add_argument("method", metavar="METHOD", help="Service method name.")
    schema.set_defaults(
        _plugin_handler=_handle_api_schema,
        _default_output="json",
    )
