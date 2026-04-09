"""Common timestamp, payload, and output normalization helpers."""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Mapping


_EXIT_CODE_RE = re.compile(r"Process exited with code (?P<code>-?\d+)")
_JSON_EXIT_CODE_RE = re.compile(r'"exit_code"\s*:\s*(?P<code>-?\d+)')


def prune_nones(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None values from a dict (shallow)."""
    return {key: value for key, value in payload.items() if value is not None}


def format_datetime(value: Any) -> str | None:
    """Serialize a datetime to ISO-8601 UTC string, or None."""
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def normalize_project_key(value: str) -> str:
    """Normalize an arbitrary project name to a lowercase slug."""
    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    return re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-").lower()


def compact_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """Drop None values while preserving falsey values."""
    return {key: value for key, value in data.items() if value is not None}


def parse_iso_timestamp(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into an aware datetime."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_timestamp(raw: str | int | float | None) -> datetime | None:
    """Parse either ISO-8601 or epoch-millisecond timestamps."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
    return parse_iso_timestamp(raw)


def extract_exit_code(output: Any) -> int | None:
    """Best-effort exit-code extraction from structured or string tool output."""
    queue: deque[Any] = deque([output])
    json_unwraps = 0
    max_json_unwraps = 8

    while queue:
        current = queue.popleft()

        if isinstance(current, Mapping):
            for key in ("exit_code", "exitCode"):
                value = current.get(key)
                if isinstance(value, int):
                    return value
            queue.extend(current.values())
            continue

        if isinstance(current, list):
            queue.extend(current)
            continue

        if not isinstance(current, str):
            continue

        for regex in (_EXIT_CODE_RE, _JSON_EXIT_CODE_RE):
            match = regex.search(current)
            if match:
                return int(match.group("code"))

        if json_unwraps >= max_json_unwraps:
            continue

        try:
            parsed = json.loads(current)
        except json.JSONDecodeError:
            continue

        json_unwraps += 1
        queue.append(parsed)

    return None


def infer_tool_success(output: Any) -> bool | None:
    """Infer tool success from a vendor payload blob when possible."""
    exit_code = extract_exit_code(output)
    if exit_code is not None:
        return exit_code == 0

    if isinstance(output, str):
        lowered = output.lower()
        if "user did not allow tool call" in lowered:
            return False
        if "<tool_use_error>" in lowered or "error:" in lowered:
            return False

    if isinstance(output, dict) and output.get("error"):
        return False

    return None
