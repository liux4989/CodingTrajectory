"""Durable living observations and lease-filtered remote reads."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from prepared_api import read_cursor, sign_cursor
from shared import (
    State,
    integer,
    js_get,
    parse_timestamp,
    require_that,
    rows,
    stable,
    timestamp,
    validate_contract,
)

Json = dict[str, Any]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def living_write(state: State, method: str, request: Json) -> Json:
    instance = request["agent_instance_id"]
    heartbeat = method == "ct_collector_heartbeat"
    head = state.get("living_head", instance)
    require_that(
        not head or head["agent_id"] == request["agent_id"],
        "agent_instance_conflict",
        403,
    )
    if not heartbeat:
        require_that(state.get("lease", instance), "agent_lease_required", 403)
        request["payload"] = validate_contract(
            "living_events_change"
            if request["kind"] == "living.events"
            else "living_sessions_change",
            request["payload"],
        )
    key = f"{instance}:{request['observation_sequence']}"
    prior = state.get("living", key)
    if prior:
        require_that(
            prior["method"] == method and stable(prior["request"]) == stable(request),
            "living_identity_conflict",
            409,
        )
        return prior["receipt"]
    require_that(
        request["observation_sequence"]
        > (head.get("observation_sequence", 0) if head else 0),
        "living_sequence_conflict",
        409,
    )
    sequence = state.next()
    result: Json = {"committed_sequence": sequence}
    if heartbeat:
        expires = (
            datetime.fromtimestamp(time.time() + request["lease_seconds"], UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        result["lease_expires_at"] = expires
        state.put(
            "lease",
            instance,
            {"agent_id": request["agent_id"], "lease_expires_at": expires},
            sequence,
        )
    state.put(
        "living",
        key,
        {"method": method, "request": request, "receipt": result, "sequence": sequence},
        sequence,
    )
    state.put(
        "living_head",
        instance,
        {
            "agent_id": request["agent_id"],
            "observation_sequence": request["observation_sequence"],
        },
        sequence,
    )
    return result


async def living_read(state: State, request: Json, secret: str) -> Json:
    async def cursor(value: Json) -> str:
        return await sign_cursor(
            {
                **value,
                "position": value.get("position_sequence", 0),
                "expires": int(time.time()) + 86400,
            },
            secret,
        )

    async def parse(value: str) -> Json:
        result = await read_cursor(value, secret)
        require_that(
            result.get("version") == 2
            and result.get("kind") in {"watermark", "snapshot", "delta"},
            "invalid_cursor",
        )
        return result

    require_that(
        isinstance(request.get("calls"), list) and 0 < len(request["calls"]) <= 100,
        "invalid_living_batch",
    )
    sequence = state.pin(request.get("snapshot_sequence"))
    evaluated_at = _now_iso()
    through: Json | None = None
    for call in request["calls"]:
        require_that(
            call.get("method") in {"living.events", "living.sessions"},
            "invalid_living_method",
        )
        call["params"] = validate_contract(
            "living_events_request"
            if call["method"] == "living.events"
            else "living_sessions_request",
            call["params"],
        )
        if call["params"].get("through"):
            value = await parse(call["params"]["through"])
            require_that(value["kind"] == "watermark", "invalid_through_cursor")
            require_that(
                value["workspace_id"] == request["workspace_id"]
                and value["method"] == call["method"],
                "cursor_scope_conflict",
            )
            if through:
                require_that(
                    value["sequence"] == through["sequence"]
                    and value["evaluated_at"] == through["evaluated_at"],
                    "batch_snapshot_conflict",
                )
            through = value
    if through:
        sequence = state.pin(through["sequence"])
        require_that(
            request.get("snapshot_sequence") is None
            or request["snapshot_sequence"] == sequence,
            "snapshot_conflict",
        )
        evaluated_at = timestamp(through["evaluated_at"])
        require_that(
            parse_timestamp(evaluated_at).timestamp() <= time.time(), "future_cursor"
        )
    freshness = """WITH instances AS (
      SELECT key FROM records WHERE kind='living_head' AND sequence<=? GROUP BY key
    ), leases AS (
      SELECT key, (SELECT payload FROM records lease WHERE lease.kind='lease'
        AND lease.key=instances.key AND lease.sequence<=? ORDER BY lease.sequence DESC LIMIT 1) AS payload
      FROM instances
    ), freshness AS (
      SELECT key, COALESCE(julianday(json_extract(payload, '$.lease_expires_at')) > julianday(?), 0) AS fresh FROM leases
    )"""
    freshness_args = [sequence, sequence, evaluated_at]
    coverage_row = state.sql.exec(
        f"""{freshness}
        SELECT COALESCE(SUM(fresh), 0) AS fresh, COUNT(*) - COALESCE(SUM(fresh), 0) AS unknown FROM freshness""",
        *freshness_args,
    ).one()
    coverage = {
        "fresh": int(js_get(coverage_row, "fresh", 0)),
        "unknown": int(js_get(coverage_row, "unknown", 0)),
    }
    results: list[Json] = []
    for call in request["calls"]:
        params = call["params"]
        scope = (
            {
                key: value
                for key, value in (params.get("scope") or {}).items()
                if value is not None
            }
            if call["method"] == "living.events"
            else {}
        )
        binding = {
            "version": 2,
            "workspace_id": request["workspace_id"],
            "method": call["method"],
            "scope": stable(scope),
        }

        def matches(value: Json, binding: Json = binding) -> None:
            require_that(
                all(value.get(key) == item for key, item in binding.items()),
                "cursor_scope_conflict",
            )

        if params.get("through"):
            value = await parse(params["through"])
            matches(value)
        watermark = await cursor(
            {
                **binding,
                "kind": "watermark",
                "sequence": sequence,
                "evaluated_at": evaluated_at,
            }
        )
        page_kind = "snapshot"
        base = 0
        position = 0
        if params.get("after"):
            after = await parse(params["after"])
            matches(after)
            if after["kind"] == "watermark":
                page_kind = "delta"
                base = integer(after["sequence"], 0, sequence)
            else:
                require_that(
                    params.get("through")
                    and after["through_sequence"] == sequence
                    and after["evaluated_at"] == evaluated_at,
                    "cursor_snapshot_conflict",
                )
                page_kind = after["kind"]
                base = (
                    integer(after["base_sequence"], 0, sequence)
                    if page_kind == "delta"
                    else 0
                )
                position = integer(after["position_sequence"], 0, sequence)
        canonical = f"""{freshness}, canonical AS (
          SELECT r.key, r.sequence,
            json_extract(r.payload, '$.request.agent_instance_id') AS instance,
            json_extract(r.payload, '$.request.payload.resource_kind') AS resource_kind,
            json_extract(r.payload, '$.request.payload.operation') AS operation,
            (SELECT json_group_object(key, value) FROM
              (SELECT key, value FROM json_each(r.payload, '$.request.payload.path') ORDER BY key)) AS path
          FROM records r WHERE r.kind='living' AND r.sequence<=?
            AND json_extract(r.payload, '$.request.kind')=?
            AND NOT EXISTS (SELECT 1 FROM json_each(?) scope
              WHERE json_extract(r.payload, '$.request.payload.path.' || scope.key) IS NOT scope.value)
        )"""
        canonical_args: list[Any] = [
            *freshness_args,
            sequence,
            call["method"],
            stable(scope),
        ]
        canonical_count = int(
            js_get(
                state.sql.exec(
                    f"{canonical} SELECT COUNT(*) AS count FROM canonical",
                    *canonical_args,
                ).one(),
                "count",
            )
        )
        eligible = f"""{canonical}, eligible AS (
          SELECT c.* FROM canonical c JOIN freshness f ON f.key=c.instance WHERE f.fresh=1
        )"""
        limit = integer(params.get("limit", 50), 1, 200)
        if page_kind == "snapshot":
            selection = f"""{eligible}, ranked AS (
              SELECT *, ROW_NUMBER() OVER (PARTITION BY instance, resource_kind, path
                ORDER BY sequence DESC) AS rank FROM eligible
            ) SELECT key, sequence FROM ranked WHERE rank=1 AND operation!='remove' AND sequence>?"""
        else:
            selection = (
                f"{eligible} SELECT key, sequence FROM eligible WHERE sequence>?"
            )
        page_rows = [
            json.loads(js_get(row, "payload"))
            for row in rows(
                state.sql.exec(
                    f"""SELECT r.payload FROM
                    ({selection} ORDER BY sequence LIMIT ?) page JOIN records r
                    ON r.kind='living' AND r.key=page.key AND r.sequence=page.sequence ORDER BY page.sequence""",
                    *canonical_args,
                    max(position, base),
                    limit + 1,
                )
            )
        ]
        changes: list[Json] = []
        size = 0
        for row in page_rows[:limit]:
            change = {
                **row["request"]["payload"],
                "revision": row["sequence"],
                "cursor": await cursor(
                    {
                        **binding,
                        "kind": page_kind,
                        "base_sequence": base,
                        "through_sequence": sequence,
                        "evaluated_at": evaluated_at,
                        "position_sequence": row["sequence"],
                    }
                ),
            }
            size += len(stable(change).encode("utf-8")) + 1
            if size > 432 * 1024:
                break
            changes.append(change)
        require_that(not page_rows or changes, "remote_result_too_large", 413)
        has_more = len(page_rows) > len(changes)
        unknown = coverage["unknown"]
        issues: list[Json] = [
            {
                "severity": "warning",
                "code": "remote_living.observation_only",
                "message": "Remote living authority exposes durable canonical observations only.",
            }
        ]
        if unknown:
            issues.append(
                {
                    "severity": "warning",
                    "code": "remote_living.heartbeat_unknown",
                    "message": f"{unknown} expired or missing agent leases; their state is unknown and their resources were omitted.",
                }
            )
        if not canonical_count:
            issues.append(
                {
                    "severity": "warning",
                    "code": "remote_living.no_canonical_observations",
                    "message": "No canonical observations cover this method at the pinned sequence.",
                }
            )
        result = {
            "schema_version": "ct.living_events.v1"
            if call["method"] == "living.events"
            else "ct.living_sessions.v2",
            "mode": params.get("mode", "view")
            if call["method"] == "living.events"
            else "view",
            "page_kind": page_kind,
            "through": watermark,
            "next_cursor": changes[-1]["cursor"] if has_more else None,
            "has_more": has_more,
            "changes": changes,
            "issues": issues,
            "coverage": {
                "source": "ct_living_observations",
                "snapshot_sequence": sequence,
                "evaluated_at": evaluated_at,
                "fresh_agent_instances": coverage["fresh"],
                "unknown_agent_instances": unknown,
                "canonical_observations": canonical_count,
                "completeness": "heartbeat_only"
                if not canonical_count
                else "partial"
                if unknown
                else "canonical_observations",
            },
        }
        result = validate_contract(
            "living_events_response"
            if call["method"] == "living.events"
            else "living_sessions_response",
            result,
        )
        results.append({"method": call["method"], "result": result})
    return {
        "workspace_id": request["workspace_id"],
        "snapshot_sequence": sequence,
        "evaluated_at": evaluated_at,
        "results": results,
    }
