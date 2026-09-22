"""Deployment-gated cleanup of retired fact-table storage."""

from __future__ import annotations

from typing import Any

from shared import State, js_get, require_that, rows

TABLES = (
    "fact_rows",
    "fact_schema",
    "staged_fact_rows",
    "staged_fact_items",
    "staged_fact_generations",
    "validated_fact_graphs",
)


def legacy_fact_tables(state: State) -> dict[str, Any]:
    existing = {
        str(js_get(row, "name"))
        for row in rows(
            state.sql.exec("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }
    tables = {
        table: int(
            js_get(
                state.sql.exec(f"SELECT COUNT(*) AS count FROM {table}").one(), "count"
            )
        )
        for table in TABLES
        if table in existing
    }
    publications = int(
        js_get(
            state.sql.exec(
                "SELECT COUNT(*) AS count FROM records WHERE kind IN ('graph_publication','publisher')"
            ).one(),
            "count",
        )
    )
    return {"tables": tables, "legacy_publication_records": publications}


def drop_empty_legacy_fact_tables(state: State) -> None:
    inventory = legacy_fact_tables(state)
    require_that(
        inventory["legacy_publication_records"] == 0, "legacy_cleanup_data_present", 409
    )
    for table, count in inventory["tables"].items():
        if table == "fact_schema":
            schema_rows = rows(state.sql.exec("SELECT id,version FROM fact_schema"))
            require_that(
                not schema_rows
                or (
                    len(schema_rows) == 1
                    and js_get(schema_rows[0], "id") == 1
                    and js_get(schema_rows[0], "version") == 1
                ),
                "legacy_cleanup_schema_unexpected",
                409,
            )
        else:
            require_that(count == 0, "legacy_cleanup_data_present", 409)
    for table in TABLES:
        state.sql.exec(f"DROP TABLE IF EXISTS {table}")
