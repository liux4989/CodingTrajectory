"""Explicit, workspace-scoped authority replacement."""

from __future__ import annotations

from typing import Any

from shared import State, js_get, js_value, require_that, rows

RESET_R2_PAGES = 4
PREVIEW_R2_PAGES = 10
TABLES = (
    "sequence",
    "records",
    "resources",
    "staged_fact_rows",
    "fact_rows",
    "fact_schema",
    "staged_fact_items",
    "staged_fact_generations",
    "validated_fact_graphs",
    "artifact_manifests",
    "artifact_cleanup",
    "artifact_upload_claims",
    "workspace_replacement",
)


def initialize_replacement(state: State) -> None:
    state.sql.exec(
        """CREATE TABLE IF NOT EXISTS workspace_replacement (
        id INTEGER PRIMARY KEY CHECK (id=1), workspace_id TEXT NOT NULL,
        export_sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('incomplete','complete'))
        )"""
    )


def workspace_replacement(state: State) -> dict[str, Any] | None:
    result = rows(
        state.sql.exec(
            "SELECT workspace_id,export_sha256,status FROM workspace_replacement WHERE id=1"
        )
    )
    if not result:
        return None
    row = result[0]
    return {
        key: js_get(row, key) for key in ("workspace_id", "export_sha256", "status")
    }


def mark_workspace_replacement(
    state: State, workspace_id: str, export_sha256: str, status: str
) -> None:
    state.sql.exec(
        """INSERT INTO workspace_replacement VALUES(1,?,?,?)
        ON CONFLICT(id) DO UPDATE SET workspace_id=excluded.workspace_id,
        export_sha256=excluded.export_sha256,status=excluded.status""",
        workspace_id,
        export_sha256,
        status,
    )


def replacement_prefix(workspace_id: str) -> str:
    return f"workspaces/{workspace_id}/artifacts/"


async def inspect_prefix(bucket: Any, prefix: str, max_pages: int) -> dict[str, Any]:
    cursor = None
    count = 0
    byte_count = 0
    for _page in range(max_pages):
        options: dict[str, Any] = {"prefix": prefix, "limit": 1000}
        if cursor:
            options["cursor"] = cursor
        result = await bucket.list(js_value(options))
        objects = rows(js_get(result, "objects", []))
        count += len(objects)
        byte_count += sum(int(js_get(item, "size", 0)) for item in objects)
        if not js_get(result, "truncated", False) or not js_get(result, "cursor"):
            return {
                "prefix": prefix,
                "objects": count,
                "bytes": byte_count,
                "truncated": False,
            }
        cursor = js_get(result, "cursor")
    return {"prefix": prefix, "objects": count, "bytes": byte_count, "truncated": True}


async def preview_workspace_replacement(
    state: State, env: Any, workspace_id: str, export_sha256: str
) -> dict[str, Any]:
    tables: dict[str, int] = {}
    existing = {
        str(js_get(row, "name"))
        for row in rows(
            state.sql.exec("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }
    for table in TABLES:
        if table in existing:
            tables[table] = int(
                js_get(
                    state.sql.exec(f"SELECT COUNT(*) AS count FROM {table}").one(),
                    "count",
                )
            )
    record_kinds = {
        str(js_get(row, "kind")): int(js_get(row, "count"))
        for row in rows(
            state.sql.exec(
                "SELECT kind,COUNT(*) AS count FROM records GROUP BY kind ORDER BY kind"
            )
        )
    }
    objects = await inspect_prefix(
        env.ARTIFACTS, replacement_prefix(workspace_id), PREVIEW_R2_PAGES
    )
    return {
        "mode": "preview",
        "workspace_id": workspace_id,
        "expected_export_sha256": export_sha256,
        "sql": {"delete_all": True, "tables": tables, "record_kinds": record_kinds},
        "replacement": workspace_replacement(state),
        "r2": objects,
        "preserved": [
            "CT_PRINCIPALS",
            "CT_CURSOR_KEY",
            "WORKSPACES binding",
            "ARTIFACTS binding",
            "other workspace Durable Objects",
            "other R2 prefixes",
        ],
        "quota": "reset does not restore Cloudflare daily counters",
    }


async def delete_workspace_artifact_prefix(
    env: Any, workspace_id: str
) -> dict[str, Any]:
    prefix = replacement_prefix(workspace_id)
    deleted = 0
    for _page in range(RESET_R2_PAGES):
        result = await env.ARTIFACTS.list(js_value({"prefix": prefix, "limit": 1000}))
        objects = rows(js_get(result, "objects", []))
        if not objects:
            return {"prefix": prefix, "deleted": deleted, "complete": True}
        keys = [str(js_get(item, "key")) for item in objects]
        require_that(
            all(key.startswith(prefix) for key in keys),
            "replacement_prefix_escape",
            500,
        )
        await env.ARTIFACTS.delete(js_value(keys))
        deleted += len(keys)
    remaining = await env.ARTIFACTS.list(js_value({"prefix": prefix, "limit": 1}))
    return {
        "prefix": prefix,
        "deleted": deleted,
        "complete": len(rows(js_get(remaining, "objects", []))) == 0,
    }
