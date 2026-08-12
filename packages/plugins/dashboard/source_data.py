"""Short-lived, validated cache for shared dashboard source reads."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

try:
    from .work_manager import DashboardWorkManager
except ImportError:
    from work_manager import DashboardWorkManager


_CACHEABLE_METHODS = {"project.list", "project.sessions"}
_CONTRACT_VERSION = {"project.list": 1, "project.sessions": 1}
_REQUEST_FIELDS = {
    "project.list": {"agent_vendor"},
    "project.sessions": {
        "project_name",
        "since_days",
        "modified_since",
        "agent_vendor",
        "include",
    },
}


class DashboardSourceData:
    """Cache exact project inventory reads shared by dashboard projections.

    The cache intentionally stops at collection methods. Session telemetry can
    change under a stable session id and does not yet expose a source revision
    suitable for a longer-lived cross-route cache.
    """

    def __init__(
        self,
        *,
        ct_json: Callable[[list[str]], dict[str, Any]],
        ttl_seconds: float = 30,
    ) -> None:
        self._ct_json = ct_json
        self._work = DashboardWorkManager(
            ttl_seconds,
            stale_ttl_seconds=ttl_seconds,
            max_entries=64,
            refresh_workers=1,
        )

    def clear(self) -> None:
        self._work.clear_all()

    def shutdown(self) -> None:
        self._work.shutdown(wait=False)

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        global_scope: bool,
    ) -> dict[str, Any]:
        if method not in _CACHEABLE_METHODS:
            raise ValueError(f"unsupported cached source method: {method}")
        if method == "project.list":
            global_scope = True
        normalized = _validated_params(method, params)
        key = (
            "ct_source",
            method,
            _CONTRACT_VERSION[method],
            global_scope,
            json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        )
        result = self._work.get_or_compute(
            key,
            lambda: self._call_uncached(
                method,
                normalized,
                global_scope=global_scope,
            ),
        )
        return copy.deepcopy(result)

    def json(self, args: list[str]) -> dict[str, Any]:
        parsed = _cacheable_command(args)
        if parsed is None:
            return self._ct_json(args)
        method, params, global_scope, output_form = parsed
        result = self.call(method, params, global_scope=global_scope)
        if output_form == "api":
            return {
                "id": None,
                "method": method,
                "ok": True,
                "result": result,
            }
        if method == "project.list":
            return _compact_project_list(result)
        return _compact_project_sessions(result)

    def _call_uncached(
        self,
        method: str,
        params: dict[str, Any],
        *,
        global_scope: bool,
    ) -> dict[str, Any]:
        args = ["api", "call", method]
        if global_scope:
            args.append("--global-scope")
        args.extend(["--params", json.dumps(params)])
        payload = self._ct_json(args)
        if not payload.get("ok"):
            error = payload.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else error
            raise RuntimeError(str(message or f"ct api request failed: {method}"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"ct api request returned invalid result: {method}")
        _validate_result(method, result)
        return result


def _cacheable_command(
    args: list[str],
) -> tuple[str, dict[str, Any], bool, str] | None:
    if len(args) >= 3 and args[:2] == ["api", "call"]:
        method = args[2]
        if method not in _CACHEABLE_METHODS:
            return None
        return (
            method,
            _params_arg(args),
            "--global-scope" in args,
            "api",
        )
    if len(args) >= 2 and args[0] == "project":
        action = args[1]
        method = {
            "list": "project.list",
            "sessions": "project.sessions",
        }.get(action)
        if method is None:
            return None
        params = _params_arg(args)
        if method == "project.sessions" and "since_days" not in params:
            params["since_days"] = 30
        return method, params, method == "project.list", "cli"
    return None


def _params_arg(args: list[str]) -> dict[str, Any]:
    try:
        index = args.index("--params")
        value = json.loads(args[index + 1])
    except (ValueError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "dashboard source command has invalid --params JSON"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("dashboard source command params must be an object")
    return value


def _validated_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(params)
    unknown = set(normalized) - _REQUEST_FIELDS[method]
    if unknown:
        raise ValueError(f"unsupported {method} parameter: {sorted(unknown)[0]}")
    for field in ("agent_vendor", "project_name"):
        value = normalized.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{method} {field} must be a string")
    if method == "project.sessions":
        include = normalized.get("include", [])
        if not isinstance(include, list) or any(
            value not in {"runtime", "usage"} for value in include
        ):
            raise ValueError("project.sessions include must contain runtime or usage")
        since_days = normalized.get("since_days")
        if since_days is not None and (
            not isinstance(since_days, int)
            or isinstance(since_days, bool)
            or since_days < 1
        ):
            raise ValueError("project.sessions since_days must be at least 1")
        normalized["include"] = sorted(set(include))
    return normalized


def _validate_result(method: str, result: dict[str, Any]) -> None:
    items = result.get("items")
    if method == "project.list":
        valid = isinstance(items, dict) and all(
            isinstance(name, str) and isinstance(item, dict)
            for name, item in items.items()
        )
    else:
        valid = isinstance(items, list) and all(
            isinstance(item, dict) and isinstance(item.get("root_session_id"), str)
            for item in items
        )
    if not valid:
        raise RuntimeError(f"ct api request returned invalid result: {method}")


def _compact_project_list(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        raise RuntimeError("project.list returned invalid items")
    return {
        "items": {
            name: {
                key: item[key]
                for key in ("path", "vendors", "sessions")
                if item.get(key) is not None
            }
            for name, item in items.items()
            if isinstance(item, dict)
        }
    }


def _compact_project_sessions(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [
            {
                key: value
                for key, value in {
                    "id": item.get("root_session_id"),
                    "project": item.get("project"),
                    "title": item.get("title"),
                    "vendors": item.get("vendors"),
                    "sessions": item.get("session_ids"),
                    "runtime": item.get("runtime"),
                    "usage": item.get("usage"),
                    "warnings": item.get("warnings") or None,
                }.items()
                if value is not None
            }
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
    }
