"""Shared helpers for analysis projections."""

from __future__ import annotations

from typing import Any

_MISSING = object()
_ITEM_DETAIL_TRUNCATE_LEN = 500
_EVENT_SCAN_PAYLOAD_PREVIEW_LEN = 300
_ELLIPSIS = "..."


def truncate_text_preview(value: Any, *, max_len: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    if max_len <= len(_ELLIPSIS):
        return _ELLIPSIS[: max(max_len, 0)]
    return text[: max_len - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def truncation_marker(
    length: int,
    event_ids: list,
    *,
    item_ref: str | None = None,
) -> str:
    if item_ref:
        return f"[{length:,} chars → session.items {item_ref} --include-content]"
    ref = " | ".join(str(eid) for eid in event_ids)
    return f"[{length:,} chars → session.events {ref}]"


def truncate_with_ref(
    value: Any,
    event_ids: list,
    max_len: int = _ITEM_DETAIL_TRUNCATE_LEN,
    *,
    item_ref: str | None = None,
) -> Any:
    if isinstance(value, str) and len(value) > max_len:
        return truncation_marker(len(value), event_ids, item_ref=item_ref)
    if isinstance(value, dict):
        return {
            key: truncate_with_ref(
                child,
                event_ids,
                max_len,
                item_ref=item_ref,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            truncate_with_ref(
                child,
                event_ids,
                max_len,
                item_ref=item_ref,
            )
            for child in value
        ]
    return value


def prune_empty_collections(value: Any) -> Any:
    if isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, child in value.items():
            child_pruned = prune_empty_collections(child)
            if child_pruned in (None, [], {}, ""):
                continue
            pruned[key] = child_pruned
        return pruned
    if isinstance(value, list):
        return [prune_empty_collections(child) for child in value]
    return value


def resolve_path(obj: Any, path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return _MISSING
        cur = cur.get(part, _MISSING)
        if cur is _MISSING:
            return _MISSING
    return cur


def match_filter(shape: dict[str, Any], expr: str) -> bool:
    if "=" not in expr:
        raise ValueError(
            f"invalid filter expression {expr!r}: expected key=value, key=*, or key=!"
        )
    key, _, value = expr.partition("=")
    resolved = resolve_path(shape, key)
    if value == "*":
        return resolved is not _MISSING and resolved is not None
    if value == "!":
        return resolved is _MISSING or resolved is None
    return str(resolved) == value


def truncate_payload_strings(
    obj: Any, max_len: int = _EVENT_SCAN_PAYLOAD_PREVIEW_LEN
) -> Any:
    if isinstance(obj, str):
        if len(obj) > max_len:
            return f"[{len(obj):,} chars]"
        return obj
    if isinstance(obj, dict):
        return {
            key: truncate_payload_strings(value, max_len) for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [truncate_payload_strings(value, max_len) for value in obj]
    return obj
