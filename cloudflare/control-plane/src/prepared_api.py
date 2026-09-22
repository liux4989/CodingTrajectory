"""Prepared API publication, lookup, and bounded serving for the Python Worker."""

from __future__ import annotations

import hmac
import inspect
import json
import math
import re
import time
from datetime import datetime
from typing import Any

from coding_trajectory.contracts.prepared_api import REMOTE_API_METHODS
from coding_trajectory.contracts.registry import SERVICE_CONTRACTS
from js import Object, Uint8Array
from pyodide.ffi import to_js
from shared import (
    Fault,
    State,
    decode,
    digest,
    encode,
    js_get,
    require_that,
    stable,
    validate_contract,
)
from shared import rows as sql_rows

API_VERSIONS: dict[str, int] = {
    name: SERVICE_CONTRACTS[name].version for name in REMOTE_API_METHODS
}

SCHEMA = "ct.prepared-api.v1"
POSTING_COLUMNS = ("id", "item_id", "turn_id", "types", "status", "tool_name")
PAGE_FIELDS = ("turns", "items", "events", "requests", "tool_usage")
MAX_SAFE_INTEGER = 2**53 - 1


def _safe_integer(value: Any) -> bool:
    """Match Number.isSafeInteger, including JSON values written as 1.0."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and float(value).is_integer()
        and abs(value) <= MAX_SAFE_INTEGER
    )


def _json_bytes(value: Any) -> bytes:
    # JSON.stringify emits compact UTF-8. Key order does not affect the bounds
    # that use this helper, but insertion order is retained for wire parity.
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


async def _digest(value: str | bytes) -> str:
    """Accept either a synchronous or asynchronous shared digest helper."""
    result = digest(value)
    if inspect.isawaitable(result):
        return await result
    return result


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _project_key(value: str) -> str:
    result = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value.strip())
    result = re.sub(r"[^a-zA-Z0-9]+", "-", result)
    return result.strip("-").lower()


def _artifact_key(workspace_id: str, kind: str, sha256: str) -> str:
    # Kept local to avoid the artifacts <-> prepared_api import cycle.
    return f"workspaces/{workspace_id}/artifacts/{kind}/{sha256}"


def _js_options(value: dict[str, Any]) -> Any:
    return to_js(value, dict_converter=Object.fromEntries)


def _buffer_bytes(value: Any) -> bytes:
    return Uint8Array.new(value).to_bytes()


def _date_parse(value: Any) -> float:
    """Parse the ISO timestamps accepted by the API contracts like Date.parse."""
    if not isinstance(value, str):
        return math.nan
    try:
        stamp = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(stamp).timestamp() * 1000
    except (OverflowError, ValueError):
        return math.nan


def publication_index(value: dict[str, Any], length: int) -> dict[str, Any] | None:
    """Validate index semantics once and retain only publication dependencies."""
    if not isinstance(value, dict) or ("mode" not in value and "posting" not in value):
        return None

    def valid(condition: Any) -> None:
        require_that(condition, "invalid_prepared_reference")

    valid(length <= (448 if "posting" in value else 64) * 1024)
    valid(value.get("schema_version") == SCHEMA)
    valid(
        _safe_integer(value.get("method_version"))
        and API_VERSIONS.get(value.get("method")) == value["method_version"]
        and isinstance(value.get("scope"), str)
        and "turn_id" in value
        and (value["turn_id"] is None or isinstance(value["turn_id"], str))
        and isinstance(value.get("source_manifest_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["source_manifest_sha256"]) is not None
    )

    if "posting" in value:
        postings = value.get("values")
        valid(
            value.get("posting") in POSTING_COLUMNS
            and _safe_integer(value.get("total"))
            and value["total"] >= 0
            and isinstance(postings, dict)
        )
        for positions in postings.values():
            valid(
                isinstance(positions, list)
                and all(
                    _safe_integer(position)
                    and position >= 0
                    and position < value["total"]
                    and (index == 0 or positions[index - 1] < position)
                    for index, position in enumerate(positions)
                )
            )
        return None

    def reference(candidate: Any, bound: int) -> dict[str, Any]:
        valid(
            isinstance(candidate, dict)
            and candidate.get("kind") == "api"
            and isinstance(candidate.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", candidate["sha256"]) is not None
            and _safe_integer(candidate.get("bytes"))
            and candidate["bytes"] > 0
            and candidate["bytes"] <= bound
        )
        return {
            "kind": "api",
            "sha256": candidate["sha256"],
            "bytes": candidate["bytes"],
        }

    references: list[dict[str, Any]]
    if value.get("mode") == "exact":
        references = [reference(value.get("result"), 440 * 1024)]
    else:
        valid(value.get("mode") in ("page", "page_columns"))
        valid(value.get("field") in PAGE_FIELDS)
        sizes = value.get("sizes")
        valid(
            _safe_integer(value.get("total"))
            and value["total"] >= 0
            and isinstance(sizes, list)
            and len(sizes) == value["total"]
            and all(_safe_integer(size) and size > 0 for size in sizes)
        )
        packs = value.get("packs")
        valid(isinstance(packs, list))
        references = [reference(value.get("topology"), 128 * 1024)]
        end = 0
        for pack in packs:
            valid(
                isinstance(pack, dict)
                and _safe_integer(pack.get("start"))
                and pack.get("start") == end
                and _safe_integer(pack.get("end"))
                and pack["end"] > end
                and pack["end"] <= value["total"]
            )
            end = int(pack["end"])
            references.append(reference(pack.get("object"), 256 * 1024))
        valid(end == value["total"])

        if value["mode"] == "page_columns":
            posting_objects = value.get("posting_objects")
            valid(
                value.get("field") in ("items", "events")
                and isinstance(posting_objects, dict)
                and all(name in POSTING_COLUMNS for name in posting_objects)
            )
            for candidate in posting_objects.values():
                references.append(reference(candidate, 448 * 1024))
            valid(isinstance(value.get("postings"), dict) and not value["postings"])

        postings = value.get("postings")
        valid(isinstance(postings, dict))
        for entries in postings.values():
            valid(isinstance(entries, dict))
            for positions in entries.values():
                valid(
                    isinstance(positions, list)
                    and all(
                        _safe_integer(position)
                        and position >= 0
                        and position < value["total"]
                        and (index == 0 or positions[index - 1] < position)
                        for index, position in enumerate(positions)
                    )
                )

    return {
        "source_manifest_sha256": value["source_manifest_sha256"],
        "method": value["method"],
        "method_version": value["method_version"],
        "scope": value["scope"],
        "turn_id": value["turn_id"],
        "references": references,
    }


def _b64(value: bytes) -> str:
    return encode(value).replace("+", "-").replace("/", "_").rstrip("=")


def _un64(value: str) -> bytes:
    padded = value.replace("-", "+").replace("_", "/")
    return decode(padded + "=" * ((4 - len(value) % 4) % 4))


def _cursor_key(secret: str) -> bytes:
    require_that(bool(secret) and len(secret) >= 32, "cursor_key_unavailable", 503)
    return secret.encode()


async def read_cursor(token: str, secret: str) -> dict[str, Any]:
    try:
        require_that(isinstance(token, str) and len(token) <= 4096, "invalid_cursor")
        parts = token.split(".")
        require_that(len(parts) == 2, "invalid_cursor")
        signature = _un64(parts[1])
        expected = hmac.digest(_cursor_key(secret), parts[0].encode(), "sha256")
        require_that(hmac.compare_digest(signature, expected), "invalid_cursor")
        value = json.loads(_un64(parts[0]).decode("utf-8"))
        require_that(
            isinstance(value, dict)
            and _safe_integer(value.get("expires"))
            and _safe_integer(value.get("position")),
            "invalid_cursor",
        )
        require_that(value["expires"] > time.time(), "view_expired", 409)
        return value
    except Fault:
        raise
    except (UnicodeError, ValueError, TypeError) as error:
        raise Fault(400, "invalid_cursor") from error


async def sign_cursor(value: dict[str, Any], secret: str) -> str:
    body = _b64(stable(value).encode())
    signature = hmac.digest(_cursor_key(secret), body.encode(), "sha256")
    token = body + "." + _b64(signature)
    require_that(len(token) <= 4096, "remote_result_too_large", 413)
    return token


def initialize_api(state: State) -> None:
    state.sql.exec(
        """CREATE TABLE IF NOT EXISTS api_views (
        hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, sequence INTEGER NOT NULL, identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS api_methods (
        view_hash TEXT NOT NULL, method TEXT NOT NULL, scope TEXT NOT NULL, turn_id TEXT NOT NULL,
        descriptor TEXT NOT NULL, PRIMARY KEY(view_hash,method,scope,turn_id));
        CREATE INDEX IF NOT EXISTS api_scope ON api_methods(method,scope,turn_id);
        CREATE INDEX IF NOT EXISTS api_method_object ON api_methods(json_extract(descriptor,'$.index.sha256'));
        CREATE TABLE IF NOT EXISTS api_inventory_cards (project_id TEXT PRIMARY KEY, cards TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS api_inventory_objects (view_hash TEXT NOT NULL, hash TEXT NOT NULL,
        PRIMARY KEY(view_hash,hash));"""
    )


async def api_view(
    workspace: str,
    source: str,
    methods: list[dict[str, Any]],
    project: str = "workspace",
) -> dict[str, Any]:
    manifest = {
        "schema_version": SCHEMA,
        "workspace_id": workspace,
        "project_id": project,
        "source_manifest_sha256": source,
        "methods": methods,
    }
    return {
        "workspace_id": workspace,
        "source_manifest_sha256": source,
        "view_manifest_sha256": await _digest(stable(manifest)),
    }


def commit_api_view(
    state: State,
    project: str,
    sequence: int,
    identity: dict[str, Any],
    methods: list[dict[str, Any]],
    indexes: dict[str, dict[str, Any]] | None = None,
) -> None:
    view_hash = identity["view_manifest_sha256"]
    state.sql.exec(
        "INSERT OR REPLACE INTO api_views VALUES(?,?,?,?)",
        view_hash,
        project,
        sequence,
        stable(
            {
                **identity,
                "source_snapshot_sequence": sequence,
                "view_snapshot_sequence": sequence,
            }
        ),
    )
    for method in methods:
        descriptor = method
        if indexes is not None and method.get("index") is not None:
            descriptor = {
                **method,
                "publication_index": indexes.get(method["index"]["sha256"]),
            }
        state.sql.exec(
            "INSERT OR REPLACE INTO api_methods VALUES(?,?,?,?,?)",
            view_hash,
            method["method"],
            method["scope"],
            method.get("turn_id") or "",
            stable(descriptor),
        )


def api_locator(state: State, request: dict[str, Any]) -> dict[str, Any]:
    params = request["params"]
    scope = _coalesce(
        params.get("session_id"), params.get("root_session_id"), "workspace"
    )
    turn = (
        ""
        if request["method"] in ("session.items", "session.events")
        else _coalesce(params.get("turn_id"), "")
    )
    requested_hash = _coalesce(
        request.get("view_manifest_sha256"), params.get("view_manifest_sha256")
    )
    if requested_hash:
        query = """SELECT v.identity,m.descriptor,v.project_id
        FROM api_methods m JOIN api_views v ON v.hash=m.view_hash
        WHERE m.method=? AND m.scope=? AND m.turn_id=? AND v.hash=?
        ORDER BY v.sequence DESC LIMIT 1"""
        values = (request["method"], scope, turn, requested_hash)
    else:
        query = """SELECT v.identity,m.descriptor,v.project_id
        FROM api_methods m JOIN api_views v ON v.hash=m.view_hash
        WHERE m.method=? AND m.scope=? AND m.turn_id=?
        AND (v.project_id='workspace' OR v.sequence=(SELECT MAX(workspace_sequence) FROM artifact_manifests WHERE project_id=v.project_id))
        ORDER BY v.sequence DESC LIMIT 1"""
        values = (request["method"], scope, turn)
    found = sql_rows(state.sql.exec(query, *values))
    require_that(
        bool(found),
        "view_expired" if requested_hash else "prepared_view_unavailable",
        409,
    )
    row = found[0]
    return {
        "identity": json.loads(js_get(row, "identity")),
        "descriptor": json.loads(js_get(row, "descriptor")),
        "project_id": js_get(row, "project_id"),
    }


def prune_api(state: State) -> None:
    state.sql.exec(
        """DELETE FROM api_views WHERE project_id<>'workspace' AND sequence NOT IN
        (SELECT workspace_sequence FROM artifact_manifests);
        DELETE FROM api_views WHERE project_id='workspace' AND sequence NOT IN
        (SELECT sequence FROM api_views WHERE project_id='workspace' ORDER BY sequence DESC LIMIT 3);
        DELETE FROM api_methods WHERE view_hash NOT IN (SELECT hash FROM api_views);
        DELETE FROM api_inventory_objects WHERE view_hash NOT IN (SELECT hash FROM api_views);"""
    )


async def prepare_inventory(
    env: Any,
    workspace: str,
    projects: list[dict[str, Any]],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build inventory only at publication; reads never enumerate graphs."""
    projects.sort(key=lambda value: value["project_id"])
    cards.sort(key=lambda value: value["root_session_id"])
    source = await _digest(stable({"projects": projects, "sessions": cards}))
    methods: list[dict[str, Any]] = []
    objects: list[str] = []

    async def put(value: dict[str, Any], bound: int) -> dict[str, Any]:
        body = stable(value).encode()
        require_that(len(body) <= bound, "remote_result_too_large", 413)
        sha256 = await _digest(body)
        await env.ARTIFACTS.put(
            _artifact_key(workspace, "api", sha256),
            body,
            _js_options(
                {
                    "customMetadata": {
                        "workspace_id": workspace,
                        "kind": "api",
                        "sha256": sha256,
                    }
                }
            ),
        )
        objects.append(sha256)
        return {"kind": "api", "sha256": sha256, "bytes": len(body)}

    for method, rows in (
        ("project.list", projects),
        ("project.sessions", cards),
    ):
        header = {
            "schema_version": SCHEMA,
            "method": method,
            "method_version": API_VERSIONS[method],
            "scope": "workspace",
            "turn_id": None,
            "source_manifest_sha256": source,
        }
        descriptor: dict[str, Any] = {
            "method": method,
            "method_version": API_VERSIONS[method],
            "scope": "workspace",
            "turn_id": None,
            "index": None,
            "error": None,
        }
        try:
            topology = await put({**header, "data": {}}, 128 * 1024)
            sizes = [len(stable(row).encode()) for row in rows]
            packs: list[dict[str, Any]] = []
            postings: dict[str, dict[str, list[int]]] = {}
            for position, row in enumerate(rows):
                values = {
                    "project_id": [row["project_id"]],
                    "project_name": [
                        _project_key(
                            _coalesce(row.get("project"), row.get("display_name"), "")
                        )
                    ],
                    "modified": [
                        _coalesce(row.get("modified"), row.get("modified_at"))
                    ],
                    "agent_vendor": row.get("vendors") or [],
                }
                for key, entries in values.items():
                    for entry in entries:
                        if entry is None:
                            continue
                        postings.setdefault(key, {}).setdefault(str(entry), []).append(
                            position
                        )
            start = 0
            while start < len(rows):
                end = start
                size = len(stable({**header, "start": len(rows), "rows": []}).encode())
                while end < len(rows) and size + sizes[end] + 1 <= 256 * 1024:
                    size += sizes[end] + 1
                    end += 1
                require_that(end > start, "remote_result_too_large", 413)
                packs.append(
                    {
                        "start": start,
                        "end": end,
                        "object": await put(
                            {**header, "start": start, "rows": rows[start:end]},
                            256 * 1024,
                        ),
                    }
                )
                start = end
            descriptor["index"] = await put(
                {
                    **header,
                    "mode": "page",
                    "field": "items",
                    "topology": topology,
                    "total": len(rows),
                    "sizes": sizes,
                    "packs": packs,
                    "postings": postings,
                },
                64 * 1024,
            )
        except Fault as error:
            if error.code != "remote_result_too_large":
                raise
            descriptor["error"] = error.code
        methods.append(descriptor)

    return {
        "identity": await api_view(workspace, source, methods),
        "methods": methods,
        "objects": objects,
    }


async def serve_prepared(
    env: Any,
    locator: dict[str, Any],
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    identity = locator["identity"]
    descriptor = locator["descriptor"]
    require_that(
        descriptor.get("method_version") == API_VERSIONS.get(method),
        "unsupported_prepared_version",
        409,
    )
    require_that(
        not descriptor.get("error"),
        descriptor.get("error") or "remote_result_too_large",
        413,
    )
    reads = 0
    fetched = 0
    header = {
        "schema_version": SCHEMA,
        "method": method,
        "method_version": descriptor["method_version"],
        "scope": descriptor["scope"],
        "turn_id": descriptor.get("turn_id"),
        "source_manifest_sha256": identity["source_manifest_sha256"],
    }

    async def load(reference: Any, bound: int) -> dict[str, Any]:
        nonlocal reads, fetched
        require_that(
            isinstance(reference, dict)
            and reference.get("kind") == "api"
            and isinstance(reference.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is not None
            and _safe_integer(reference.get("bytes"))
            and reference["bytes"] > 0,
            "prepared_object_corrupt",
            503,
        )
        reads += 1
        require_that(
            reference["bytes"] <= bound
            and reads <= 10
            and fetched + reference["bytes"] <= 768 * 1024,
            "remote_result_too_large",
            413,
        )
        stored = await env.ARTIFACTS.get(
            _artifact_key(identity["workspace_id"], "api", reference["sha256"])
        )
        require_that(stored is not None, "prepared_object_missing", 503)
        require_that(stored.size == reference["bytes"], "prepared_object_corrupt", 503)
        body = _buffer_bytes(await stored.arrayBuffer())
        fetched += len(body)
        require_that(
            len(body) == reference["bytes"]
            and await _digest(body) == reference["sha256"],
            "prepared_object_corrupt",
            503,
        )
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as error:
            raise Fault(503, "prepared_object_corrupt") from error
        require_that(
            isinstance(value, dict)
            and all(
                key in value and value[key] == item for key, item in header.items()
            ),
            "prepared_object_corrupt",
            503,
        )
        return value

    index = await load(descriptor.get("index"), 64 * 1024)
    if index.get("mode") == "exact":
        return (await load(index.get("result"), 440 * 1024)).get("data")
    require_that(
        index.get("mode") in ("page", "page_columns")
        and index.get("field") in PAGE_FIELDS,
        "prepared_object_corrupt",
        503,
    )
    require_that(
        isinstance(index.get("packs"), list)
        and isinstance(index.get("postings"), dict),
        "prepared_object_corrupt",
        503,
    )
    base = (await load(index.get("topology"), 128 * 1024)).get("data")
    require_that(isinstance(base, dict), "prepared_object_corrupt", 503)
    if index["mode"] == "page_columns":
        needed = {
            "id" if name in ("item_ids", "event_ids") else name
            for name, value in params.items()
            if value is not None
        }
        posting_objects = index.get("posting_objects")
        require_that(isinstance(posting_objects, dict), "prepared_object_corrupt", 503)
        for name, reference in posting_objects.items():
            if name not in needed:
                continue
            column = await load(reference, 448 * 1024)
            require_that(
                column.get("posting") == name
                and column.get("total") == index.get("total"),
                "prepared_object_corrupt",
                503,
            )
            index["postings"][name] = column.get("values")

    if index["field"] == "turns":
        require_that(
            isinstance(base.get("project"), dict), "prepared_object_corrupt", 503
        )
        base["project"] = {**base["project"], "project_id": locator["project_id"]}

    total = index.get("total")
    sizes = index.get("sizes")
    require_that(
        _safe_integer(total)
        and total >= 0
        and isinstance(sizes, list)
        and len(sizes) == total
        and all(_safe_integer(size) and size > 0 for size in sizes),
        "prepared_object_corrupt",
        503,
    )
    total = int(total)
    pack_for: list[int] = []
    for number, pack in enumerate(index["packs"]):
        require_that(
            isinstance(pack, dict)
            and _safe_integer(pack.get("start"))
            and pack.get("start") == len(pack_for)
            and _safe_integer(pack.get("end"))
            and pack["end"] > pack["start"]
            and pack["end"] <= total,
            "prepared_object_corrupt",
            503,
        )
        pack_for.extend([number] * (int(pack["end"]) - len(pack_for)))
    require_that(len(pack_for) == total, "prepared_object_corrupt", 503)

    normalized = {
        key: value
        for key, value in params.items()
        if key not in ("cursor", "view_manifest_sha256") and value is not None
    }
    for key in ("item_ids", "event_ids", "types"):
        if key in normalized:
            normalized[key] = sorted(set(normalized[key]))
    binding = {
        "workspace_id": identity["workspace_id"],
        "method": method,
        "method_version": descriptor["method_version"],
        "binding": await _digest(stable({"method": method, "params": normalized})),
        "view_manifest_sha256": identity["view_manifest_sha256"],
        "source_manifest_sha256": identity["source_manifest_sha256"],
        "index_sha256": descriptor["index"]["sha256"],
    }
    older = index["field"] == "turns"
    position = total if older else 0
    if params.get("cursor"):
        cursor = await read_cursor(params["cursor"], env.CT_CURSOR_KEY)
        require_that(
            all(cursor.get(key) == value for key, value in binding.items())
            and cursor["position"] >= 0
            and cursor["position"] <= total,
            "invalid_cursor",
        )
        position = int(cursor["position"])

    selected: set[int] | None = None
    missing: set[str] = set()
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
        "modified_since",
    ):
        if params.get(name) is None:
            continue
        if name == "turn_id" and index["field"] in ("requests", "tool_usage"):
            continue
        key = "id" if name in ("item_ids", "event_ids") else name
        values = params[name] if isinstance(params[name], list) else [params[name]]
        matches: set[int] = set()
        if name == "modified_since":
            cutoff = _date_parse(params[name])
            for stamp, positions in index["postings"].get("modified", {}).items():
                if _date_parse(stamp) >= cutoff:
                    matches.update(int(item) for item in positions)
        else:
            for value in values:
                if name == "project_name":
                    value = _project_key(value)
                found = index["postings"].get(key, {}).get(str(value), [])
                matches.update(int(item) for item in found)
        if name == "project_name":
            overlap = sum(
                any(int(position) in matches for position in entries)
                for entries in index["postings"].get("project_id", {}).values()
            )
            require_that(overlap <= 1, "ambiguous_project_name")
        selected = matches if selected is None else selected & matches

    for name in ("item_ids", "event_ids"):
        for identifier in params.get(name) or []:
            if not index["postings"].get("id", {}).get(identifier, []):
                missing.add(identifier)

    positions = sorted(selected if selected is not None else range(total))
    require_that(
        all(_safe_integer(value) and 0 <= value < total for value in positions),
        "prepared_object_corrupt",
        503,
    )
    candidates = (
        [value for value in positions if value < position]
        if older
        else [value for value in positions if value >= position]
    )
    if older:
        candidates.reverse()

    chosen: list[int] = []
    selected_packs: set[int] = set()
    pack_order: list[int] = []
    size = 2
    pagination_bytes = 4096 + len(_json_bytes(list(missing))) + 512
    budget = 440 * 1024 - len(_json_bytes(base)) - pagination_bytes
    if older:
        budget = min(budget, 320 * 1024)
    for candidate in candidates:
        pack_number = pack_for[candidate]
        next_packs = selected_packs | {pack_number}
        if (
            len(chosen) == params["limit"]
            or size + sizes[candidate] + 1 > budget
            or len(next_packs) > 2
            or fetched
            + sum(index["packs"][number]["object"]["bytes"] for number in next_packs)
            > 768 * 1024
        ):
            break
        chosen.append(candidate)
        if pack_number not in selected_packs:
            selected_packs.add(pack_number)
            pack_order.append(pack_number)
        size += int(sizes[candidate]) + 1
    require_that(not candidates or bool(chosen), "remote_result_too_large", 413)

    rows: dict[int, Any] = {}
    for number in pack_order:
        pack = index["packs"][number]
        value = await load(pack["object"], 256 * 1024)
        require_that(
            _safe_integer(value.get("start"))
            and value.get("start") == pack["start"]
            and isinstance(value.get("rows"), list)
            and len(value["rows"]) == pack["end"] - pack["start"],
            "prepared_object_corrupt",
            503,
        )
        for offset, row in enumerate(value["rows"]):
            ordinal = int(pack["start"]) + offset
            require_that(
                not older
                or (
                    isinstance(row, dict)
                    and _safe_integer(row.get("global_ordinal"))
                    and row.get("global_ordinal") == ordinal
                ),
                "prepared_object_corrupt",
                503,
            )
            rows[ordinal] = row

    chosen.sort()
    more = len(chosen) < len(candidates)
    next_cursor = None
    if more:
        next_position = chosen[0] if older else chosen[-1] + 1
        next_cursor = await sign_cursor(
            {**binding, "position": next_position, "expires": int(time.time()) + 86400},
            env.CT_CURSOR_KEY,
        )
    result = {**base, index["field"]: [rows.get(position) for position in chosen]}
    if index["field"] == "tool_usage":
        details = result.pop("tool_usage")
        result["tool_items"] = [
            row["tool_item"] for row in details if row.get("tool_item") is not None
        ]
        if result.get("item_real_token_costs") is not None:
            result["item_real_token_costs"] = [
                row["item_real_token_cost"]
                for row in details
                if row.get("item_real_token_cost") is not None
            ]
    if older:
        result["page"] = {
            "direction": "older",
            "requested_limit": params["limit"],
            "start_ordinal": chosen[0] if chosen else 0,
            "end_ordinal_exclusive": chosen[-1] + 1 if chosen else 0,
            "returned": len(chosen),
            "total": total,
            "has_more": more,
            "next_cursor": next_cursor,
        }
    else:
        result.update(
            total=len(positions),
            returned=len(chosen),
            next_cursor=next_cursor,
            unresolved_ids=sorted(missing),
        )
    if "coverage" in result and result["coverage"] is not None:
        result["coverage"]["trimmed"] = bool(
            result["coverage"].get("trimmed") or more or len(chosen) < len(positions)
        )
    require_that(
        len(_json_bytes(result)) <= 440 * 1024,
        "remote_result_too_large",
        413,
    )
    return result


def _add_defaults(value: Any, normalized: Any) -> None:
    """Apply Pydantic defaults without rewriting already supplied JSON values."""
    if isinstance(value, dict) and isinstance(normalized, dict):
        for key, item in normalized.items():
            if key not in value:
                value[key] = item
            else:
                _add_defaults(value[key], item)
    elif isinstance(value, list) and isinstance(normalized, list):
        for original, item in zip(value, normalized, strict=False):
            _add_defaults(original, item)


def _apply_validation(name: str, value: dict[str, Any]) -> None:
    normalized = validate_contract(name, value)
    if normalized is not None and normalized is not value:
        _add_defaults(value, normalized)


def validate_api(message: dict[str, Any]) -> None:
    require_that(message.get("protocol") == "ct.api.v1", "unsupported_version")
    require_that(
        message.get("method") not in ("session.search", "living.events"),
        "method_unavailable",
        501,
    )
    require_that(
        API_VERSIONS.get(message.get("method")) == message.get("method_version"),
        "unsupported_version",
    )
    _apply_validation("api_request", message)
    method = message["method"]
    contract = f"api_method:{method}"
    _apply_validation(contract, message["params"])
    params = message["params"]
    require_that(
        not (params.get("project_id") and params.get("project_name")),
        "invalid_contract",
    )
    if params.get("project_id") is not None:
        require_that(
            isinstance(params["project_id"], str)
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                params["project_id"],
                flags=re.IGNORECASE,
            )
            is not None,
            "invalid_contract",
        )
        params["project_id"] = params["project_id"].lower()
