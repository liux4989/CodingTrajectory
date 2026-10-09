"""Count-based, query-bound live keyset pages (no snapshot or byte budget)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from coding_trajectory.contracts import service_contract

_JSON = TypeAdapter(Any)


class LocalQueryError(ValueError):
    """Public query failure understood by the local runtime."""

    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


def _json(value: Any) -> str:
    return json.dumps(
        _JSON.dump_python(value, mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def paginate(
    rows: list[Any],
    *,
    method: str,
    params: dict[str, Any],
    key: Callable[[Any], tuple],
    scope: Any = None,
    older: bool = False,
) -> tuple[list[Any], str | None]:
    """Page canonical keys, returning rows in ascending source/rank order.

    ``params`` must already be contract-validated. Scope augments its session /
    graph filters for inventory calls (global flag and current directory).
    A cursor carries the last key, query hash and method contract version;
    changing page size is allowed, changing filters or scope is not. Keys must
    be unique comparable tuples of JSON scalars, including an ID tie-breaker.
    Live insertions/deletions do not invalidate a cursor's source position.
    """
    version = service_contract(method).version
    binding = hashlib.sha256(
        _json(
            [
                method,
                {k: v for k, v in params.items() if k not in {"limit", "cursor"}},
                scope,
            ]
        ).encode()
    ).hexdigest()
    ordered = sorted(rows, key=key)
    cursor = params.get("cursor")
    if cursor is not None:
        try:
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"position", "query", "version"}
                or value["query"] != binding
                or type(value["version"]) is not int
                or value["version"] != version
                or not isinstance(value["position"], list)
                or not value["position"]
                or any(type(v) not in {str, int, float} for v in value["position"])
                or _json(value).encode() != raw
            ):
                raise ValueError("invalid cursor payload")
            position = tuple(value["position"])
            if ordered and (
                len(position) != len(key(ordered[0]))
                or any(
                    type(a) is not type(b) for a, b in zip(position, key(ordered[0]))
                )
            ):
                raise ValueError("invalid cursor key")
            ordered = [
                row
                for row in ordered
                if (key(row) < position if older else key(row) > position)
            ]
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise LocalQueryError("invalid_cursor") from exc
    limit = params["limit"]
    page = ordered[-limit:] if older else ordered[:limit]
    more = len(ordered) > len(page)
    token = None
    if more:
        position = key(page[0] if older else page[-1])
        token = (
            base64.urlsafe_b64encode(
                _json(
                    {"position": position, "query": binding, "version": version}
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return page, token
