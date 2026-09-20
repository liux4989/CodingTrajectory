"""Bounded selection and signed paging over immutable prepared API objects."""

from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from coding_trajectory.contracts import service_contract
from coding_trajectory.contracts.prepared_api import (
    API_ENVELOPE_RESERVE,
    MAX_API_FETCH_BYTES,
    MAX_API_INDEX_BYTES,
    MAX_API_PACK_BYTES,
    MAX_API_RESPONSE_BYTES,
    MAX_API_TOPOLOGY_BYTES,
    MAX_API_TURN_BYTES,
    PREPARED_API_SCHEMA,
    PreparedMethod,
    PreparedObject,
    ViewIdentity,
)
from coding_trajectory.control_plane.prepared_api import (
    PreparedApi,
    PreparedApiError,
    encoded,
    json_value,
    sha256,
)


def local_signing_key() -> bytes:
    path = Path.home() / ".coding-trajectory" / "api-cursor.key"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise PreparedApiError("cursor_key_unavailable", 503)
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def issue_cursor(payload: dict[str, Any], key: bytes) -> str:
    body = _b64(encoded(payload))
    result = body + "." + _b64(hmac.digest(key, body.encode(), "sha256"))
    if len(result) > 4096:
        raise PreparedApiError("remote_result_too_large", 413)
    return result


def decode_cursor(token: str, key: bytes) -> dict[str, Any]:
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError()
        body, signature = token.split(".")
        expected = _b64(hmac.digest(key, body.encode(), "sha256"))
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        result = json.loads(
            base64.b64decode(
                body + "=" * (-len(body) % 4), altchars=b"-_", validate=True
            )
        )
        if (
            not isinstance(result, dict)
            or type(result.get("expires")) is not int
            or type(result.get("position")) is not int
        ):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PreparedApiError("invalid_cursor") from exc
    if result["expires"] <= int(time.time()):
        raise PreparedApiError("view_expired", 409)
    return result


def cursor_binding(method: str, params: dict[str, Any]) -> str:
    normalized = json_value(
        {
            key: value
            for key, value in params.items()
            if key not in {"cursor", "view_manifest_sha256"} and value is not None
        }
    )
    for key in ("item_ids", "event_ids", "types"):
        if key in normalized:
            normalized[key] = sorted(set(normalized[key]))
    return sha256(encoded({"method": method, "params": normalized}))


def selected_positions(
    index: dict[str, Any], params: dict[str, Any]
) -> tuple[list[int], list[str]]:
    selected: set[int] | None = None
    postings = index["postings"]
    missing = []
    for name in (
        "turn_id",
        "item_id",
        "item_ids",
        "event_ids",
        "types",
        "status",
        "tool_name",
        "project_id",
        "project_name",
        "agent_vendor",
    ):
        if name not in params or params[name] is None:
            continue
        values = params[name] if isinstance(params[name], list) else [params[name]]
        key = "id" if name in {"item_ids", "event_ids"} else name
        matches: set[int] = set()
        for value in values:
            if name == "project_name":
                from coding_trajectory.ingestion.common import normalize_project_key

                value = normalize_project_key(value)
            found = postings.get(key, {}).get(str(value), [])
            matches.update(found)
        if (
            name == "project_name"
            and sum(
                bool(matches.intersection(values))
                for values in postings.get("project_id", {}).values()
            )
            > 1
        ):
            raise PreparedApiError("ambiguous_project_name")
        selected = matches if selected is None else selected & matches
    if params.get("modified_since"):

        def instant(value: Any) -> datetime:
            stamp = (
                value if isinstance(value, datetime) else datetime.fromisoformat(value)
            )
            return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp

        cutoff = instant(params["modified_since"])
        matches = {
            position
            for stamp, positions in postings.get("modified", {}).items()
            if instant(stamp) >= cutoff
            for position in positions
        }
        selected = matches if selected is None else selected & matches
    for name in ("item_ids", "event_ids"):
        for value in params.get(name) or []:
            if not postings.get("id", {}).get(str(value), []):
                missing.append(str(value))
    return sorted(selected if selected is not None else range(index["total"])), sorted(
        set(missing)
    )


def save_local_view(api: PreparedApi, source_digest: str) -> ViewIdentity:
    key = local_signing_key()
    workspace = str(uuid5(NAMESPACE_URL, sha256(key)))
    manifest = {
        "schema_version": PREPARED_API_SCHEMA,
        "workspace_id": workspace,
        "source_manifest_sha256": source_digest,
        "methods": json_value(api.methods),
    }
    identity = ViewIdentity(
        workspace_id=workspace,
        source_snapshot_sequence=None,
        source_manifest_sha256=source_digest,
        view_snapshot_sequence=None,
        view_manifest_sha256=sha256(encoded(manifest)),
    )
    body = encoded({"manifest": manifest, "api": api})
    path = Path.home() / ".coding-trajectory" / "api-views.sqlite"
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS views (hash TEXT PRIMARY KEY, body BLOB NOT NULL)"
        )
        if len(body) > 128 * 1024 * 1024:
            raise PreparedApiError("remote_result_too_large", 413)
        db.execute(
            "INSERT OR IGNORE INTO views VALUES (?,?)",
            (identity.view_manifest_sha256, body),
        )
        while (
            db.execute("SELECT COALESCE(SUM(length(body)),0) FROM views").fetchone()[0]
            > 128 * 1024 * 1024
        ):
            db.execute("DELETE FROM views WHERE rowid=(SELECT MIN(rowid) FROM views)")
    return identity


def load_local_view(view_hash: str) -> tuple[PreparedApi, ViewIdentity]:
    path = Path.home() / ".coding-trajectory" / "api-views.sqlite"
    if not path.is_file():
        raise PreparedApiError("view_expired", 409)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM views WHERE hash=?", (view_hash,)).fetchone()
    if row is None:
        raise PreparedApiError("view_expired", 409)
    value = json.loads(row[0])
    manifest = value["manifest"]
    if (
        sha256(encoded(manifest)) != view_hash
        or manifest["schema_version"] != PREPARED_API_SCHEMA
    ):
        raise PreparedApiError("unsupported_prepared_version", 409)
    api = PreparedApi.model_validate(value["api"])
    if json_value(api.methods) != manifest["methods"]:
        raise PreparedApiError("prepared_object_corrupt", 503)
    identity = ViewIdentity(
        workspace_id=manifest["workspace_id"],
        source_snapshot_sequence=None,
        source_manifest_sha256=manifest["source_manifest_sha256"],
        view_snapshot_sequence=None,
        view_manifest_sha256=view_hash,
    )
    return api, identity


def read_prepared(
    descriptor: PreparedMethod,
    params: dict[str, Any],
    *,
    identity: ViewIdentity,
    fetch: Callable[[str], bytes],
    signing_key: bytes,
) -> dict[str, Any]:
    """Read bounded metadata + two packs; every byte is length/hash checked."""
    method = descriptor.method
    if descriptor.method_version != service_contract(method).version:
        raise PreparedApiError("unsupported_prepared_version", 409)
    if descriptor.error:
        raise PreparedApiError(descriptor.error, 413)
    if descriptor.index is None:
        raise PreparedApiError("prepared_view_unavailable", 409)
    fetched = 0
    reads = 0
    header: dict[str, Any] | None = None

    def load(ref: dict[str, Any] | PreparedObject, bound: int) -> dict[str, Any]:
        nonlocal fetched, reads
        reference = PreparedObject.model_validate(ref)
        if (
            reference.bytes > bound
            or reads >= 10
            or fetched + reference.bytes > MAX_API_FETCH_BYTES
        ):
            raise PreparedApiError("remote_result_too_large", 413)
        try:
            body = fetch(reference.sha256)
        except KeyError as exc:
            raise PreparedApiError("prepared_object_missing", 503) from exc
        reads += 1
        fetched += len(body)
        if len(body) != reference.bytes or sha256(body) != reference.sha256:
            raise PreparedApiError("prepared_object_corrupt", 503)
        try:
            value = json.loads(body)
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != PREPARED_API_SCHEMA
            ):
                raise ValueError()
            expected = header or {
                "method": method,
                "method_version": descriptor.method_version,
                "scope": descriptor.scope,
                "turn_id": descriptor.turn_id,
                "source_manifest_sha256": identity.source_manifest_sha256,
            }
            if any(value.get(key) != item for key, item in expected.items()):
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise PreparedApiError("prepared_object_corrupt", 503) from exc
        return value

    index = load(descriptor.index, MAX_API_INDEX_BYTES)
    header = {
        key: index[key]
        for key in (
            "method",
            "method_version",
            "scope",
            "turn_id",
            "source_manifest_sha256",
        )
    }
    if params.get("view_manifest_sha256") not in {None, identity.view_manifest_sha256}:
        raise PreparedApiError("view_expired", 409)
    if index["mode"] == "exact":
        return load(index["result"], MAX_API_RESPONSE_BYTES - API_ENVELOPE_RESERVE)[
            "data"
        ]
    if index["mode"] not in {"page", "page_columns"}:
        raise PreparedApiError("prepared_object_corrupt", 503)
    base = load(index["topology"], MAX_API_TOPOLOGY_BYTES)["data"]
    if index["mode"] == "page_columns":
        needed = {
            "id" if name in {"item_ids", "event_ids"} else name
            for name, value in params.items()
            if value is not None
        }
        for name, ref in index["posting_objects"].items():
            if name in needed:
                column = load(ref, MAX_API_RESPONSE_BYTES)
                if (
                    column.get("posting") != name
                    or column.get("total") != index["total"]
                ):
                    raise PreparedApiError("prepared_object_corrupt", 503)
                index["postings"][name] = column["values"]
    total = index["total"]
    if type(total) is not int or total < 0 or len(index["sizes"]) != total:
        raise PreparedApiError("prepared_object_corrupt", 503)
    pack_for = []
    previous = 0
    for pack_number, pack in enumerate(index["packs"]):
        if pack["start"] != previous or not previous < pack["end"] <= total:
            raise PreparedApiError("prepared_object_corrupt", 503)
        pack_for.extend([pack_number] * (pack["end"] - previous))
        previous = pack["end"]
    if previous != total or any(
        type(size) is not int or size < 1 for size in index["sizes"]
    ):
        raise PreparedApiError("prepared_object_corrupt", 503)
    older = index["field"] == "turns"
    usage = index["field"] in {"requests", "tool_usage"}
    positions, unresolved = selected_positions(
        index, {k: v for k, v in params.items() if not usage or k != "turn_id"}
    )
    limit = params["limit"]
    binding = {
        "workspace_id": identity.workspace_id,
        "method": method,
        "method_version": descriptor.method_version,
        "binding": cursor_binding(method, params),
        "view_manifest_sha256": identity.view_manifest_sha256,
        "source_manifest_sha256": identity.source_manifest_sha256,
        "index_sha256": descriptor.index.sha256,
    }
    position = total if older else 0
    if params.get("cursor"):
        cursor = decode_cursor(params["cursor"], signing_key)
        if (
            any(cursor.get(key) != value for key, value in binding.items())
            or not 0 <= cursor["position"] <= total
        ):
            raise PreparedApiError("invalid_cursor")
        position = cursor["position"]
    candidates = (
        [value for value in positions if value < position]
        if older
        else [value for value in positions if value >= position]
    )
    if older:
        candidates.reverse()
    selected = []
    packs: set[int] = set()
    size = 2
    # A cursor can occupy 4096 bytes. Missing-ID strings are request-sized;
    # reserve them explicitly plus bounded page keys/counters before selection.
    pagination_bytes = 4096 + len(encoded(unresolved)) + 512
    budget = (
        MAX_API_RESPONSE_BYTES
        - API_ENVELOPE_RESERVE
        - len(encoded(base))
        - pagination_bytes
    )
    if older:
        budget = min(budget, MAX_API_TURN_BYTES)
    for ordinal in candidates:
        if not 0 <= ordinal < total:
            raise PreparedApiError("prepared_object_corrupt", 503)
        pack_number = pack_for[ordinal]
        if (
            len(selected) == limit
            or size + index["sizes"][ordinal] + 1 > budget
            or len(packs | {pack_number}) > 2
            or fetched
            + sum(index["packs"][p]["object"]["bytes"] for p in packs | {pack_number})
            > MAX_API_FETCH_BYTES
        ):
            break
        packs.add(pack_number)
        selected.append(ordinal)
        size += index["sizes"][ordinal] + 1
    if candidates and not selected:
        raise PreparedApiError("remote_result_too_large", 413)
    rows_by_position = {}
    for pack_number in sorted(packs):
        pack = index["packs"][pack_number]
        value = load(pack["object"], MAX_API_PACK_BYTES)
        if (
            value["start"] != pack["start"]
            or len(value["rows"]) != pack["end"] - pack["start"]
        ):
            raise PreparedApiError("prepared_object_corrupt", 503)
        for offset, row in enumerate(value["rows"], start=pack["start"]):
            if len(encoded(row)) != index["sizes"][offset] or (
                older and row["global_ordinal"] != offset
            ):
                raise PreparedApiError("prepared_object_corrupt", 503)
            rows_by_position[offset] = row
    selected.sort()
    more = len(selected) < len(candidates)
    next_position = (
        selected[0]
        if older and selected
        else selected[-1] + 1
        if selected
        else position
    )
    cursor = (
        issue_cursor(
            {**binding, "position": next_position, "expires": int(time.time()) + 86400},
            signing_key,
        )
        if more
        else None
    )
    result = {**base, index["field"]: [rows_by_position[key] for key in selected]}
    if index["field"] == "tool_usage":
        details = result.pop("tool_usage")
        result["tool_items"] = [
            row["tool_item"] for row in details if row["tool_item"] is not None
        ]
        if result["item_real_token_costs"] is not None:
            result["item_real_token_costs"] = [
                row["item_real_token_cost"]
                for row in details
                if row["item_real_token_cost"] is not None
            ]
    if older:
        result["page"] = {
            "direction": "older",
            "requested_limit": limit,
            "start_ordinal": selected[0] if selected else 0,
            "end_ordinal_exclusive": selected[-1] + 1 if selected else 0,
            "returned": len(selected),
            "total": total,
            "has_more": more,
            "next_cursor": cursor,
        }
    else:
        result.update(
            total=len(positions),
            returned=len(selected),
            next_cursor=cursor,
            unresolved_ids=unresolved,
        )
    if "coverage" in result:
        result["coverage"] = {
            **result["coverage"],
            "trimmed": bool(
                result["coverage"].get("trimmed")
                or more
                or len(selected) < len(positions)
            ),
        }
    if len(encoded(result)) > MAX_API_RESPONSE_BYTES - API_ENVELOPE_RESERVE:
        raise PreparedApiError("remote_result_too_large", 413)
    return result
