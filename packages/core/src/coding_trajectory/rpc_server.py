"""JSON-RPC 2.0 server over stdin/stdout for coding-trajectory queries."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from coding_trajectory.discovery import discover_store, format_discovery_sources
from coding_trajectory.query import DocumentError, ResourceNotFoundError
from coding_trajectory.service import (
    resolve_collection,
    resolve_resource,
    serialize_event_detail,
    serialize_session_detail,
    serialize_step_detail,
    serialize_trajectory_detail,
    serialize_turn_detail,
)

# JSON-RPC / session-api error codes
_ERROR_CODES: dict[str, int] = {
    "session_not_found": 40400,
    "trajectory_not_found": 40401,
    "turn_not_found": 40402,
    "event_not_found": 40403,
    "step_not_found": 40404,
    "invalid_request": -32600,
    "method_not_found": -32601,
    "invalid_params": -32602,
    "internal_error": -32603,
}

_RESOURCE_NOT_FOUND_CODES: dict[str, int] = {
    "session": _ERROR_CODES["session_not_found"],
    "trajectory": _ERROR_CODES["trajectory_not_found"],
    "turn": _ERROR_CODES["turn_not_found"],
    "event": _ERROR_CODES["event_not_found"],
    "step": _ERROR_CODES["step_not_found"],
}


def _error_code_for_resource(message: str) -> int:
    for resource, code in _RESOURCE_NOT_FOUND_CODES.items():
        if resource in message:
            return code
    return _ERROR_CODES["session_not_found"]


def _make_response(id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    return resp


def _make_error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def _dispatch(
    method: str,
    params: dict[str, Any],
    *,
    store: Any,
    global_scope: bool,
    current_dir: Path,
    discovery_note: str,
) -> Any:
    if method == "trajectory.get":
        trajectory = resolve_resource(store, "trajectory", params["trajectory_id"])
        return serialize_trajectory_detail(trajectory)

    if method == "trajectory.list":
        trajectories = resolve_collection(store, "trajectory", global_scope=global_scope, current_dir=current_dir)
        return {
            "items": [serialize_trajectory_detail(t) for t in trajectories],
            "discovery_note": discovery_note,
        }

    if method == "session.get":
        session = resolve_resource(store, "session", params["session_id"])
        return serialize_session_detail(session)

    if method == "session.list":
        sessions = resolve_collection(
            store, "session", trajectory_id=params.get("trajectory_id"),
        )
        return {
            "items": [serialize_session_detail(s) for s in sessions],
            "discovery_note": discovery_note,
        }

    if method == "session.bundle":
        session = resolve_resource(store, "session", params["session_id"])
        return {
            "session": serialize_session_detail(session),
            "turns": [serialize_turn_detail(t) for t in session.turns],
        }

    if method == "turn.get":
        turn = resolve_resource(store, "turn", params["turn_id"])
        return serialize_turn_detail(turn)

    if method == "turn.bundle":
        turn = resolve_resource(store, "turn", params["turn_id"])
        steps = [serialize_step_detail(s) for s in turn.steps]
        return {"turn": serialize_turn_detail(turn), "steps": steps}

    if method == "event.get":
        event = resolve_resource(store, "event", params["event_id"])
        return serialize_event_detail(event)

    if method == "event.batch_get":
        event_ids = params.get("event_ids") or []
        if not isinstance(event_ids, list):
            raise TypeError("event_ids must be an array")
        return {
            "items": [
                serialize_event_detail(resolve_resource(store, "event", event_id))
                for event_id in event_ids
            ]
        }

    if method == "step.get":
        step = resolve_resource(store, "step", params["step_id"])
        return serialize_step_detail(step)

    raise KeyError(method)


def _handle_request(
    request: dict[str, Any],
    *,
    store: Any,
    global_scope: bool,
    current_dir: Path,
    discovery_note: str,
) -> dict[str, Any]:
    req_id = request.get("id")

    method = request.get("method")
    if not isinstance(method, str):
        return _make_response(req_id, error=_make_error(_ERROR_CODES["invalid_request"], "missing or invalid method"))

    params = request.get("params", {})
    if not isinstance(params, dict):
        return _make_response(req_id, error=_make_error(_ERROR_CODES["invalid_params"], "params must be an object"))

    try:
        result = _dispatch(
            method,
            params,
            store=store,
            global_scope=global_scope,
            current_dir=current_dir,
            discovery_note=discovery_note,
        )
    except KeyError as exc:
        if str(exc).strip("'\"") == method:
            return _make_response(
                req_id, error=_make_error(_ERROR_CODES["method_not_found"], f"unknown method: {method}"),
            )
        raise
    except ResourceNotFoundError as exc:
        code = _error_code_for_resource(str(exc))
        return _make_response(req_id, error=_make_error(code, str(exc)))
    except DocumentError as exc:
        return _make_response(req_id, error=_make_error(_ERROR_CODES["internal_error"], str(exc)))
    except (ValueError, TypeError) as exc:
        return _make_response(req_id, error=_make_error(_ERROR_CODES["invalid_params"], str(exc)))

    return _make_response(req_id, result=result)


def serve(argv: list[str] | None = None) -> None:
    """Run the JSON-RPC server loop reading from stdin, writing to stdout."""
    if argv is None:
        argv = sys.argv[1:]

    global_scope = "--global-scope" in argv
    current_dir = Path.cwd()

    try:
        discovery = discover_store(current_dir=current_dir, global_scope=global_scope)
    except DocumentError as exc:
        # Write a single error response for any request that arrives, then exit.
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                request = {}
            response = _make_response(
                request.get("id"), error=_make_error(_ERROR_CODES["internal_error"], str(exc)),
            )
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            break
        return

    store = discovery.store
    discovery_note = format_discovery_sources(discovery.sources)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _make_response(None, error=_make_error(_ERROR_CODES["invalid_request"], f"parse error: {exc}"))
        else:
            response = _handle_request(
                request,
                store=store,
                global_scope=global_scope,
                current_dir=current_dir,
                discovery_note=discovery_note,
            )

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve()
