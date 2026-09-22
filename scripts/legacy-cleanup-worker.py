"""Disposable Python Durable Object entrypoint for legacy cleanup qualification."""

from __future__ import annotations

from typing import Any

from legacy_cleanup import TABLES, drop_empty_legacy_fact_tables, legacy_fact_tables
from shared import Fault, State, js_get, py_value, rows, transaction_sync
from workers import DurableObject, Response, WorkerEntrypoint


class Workspace(DurableObject):
    def __init__(self, ctx: Any, env: Any) -> None:
        super().__init__(ctx, env)
        self.state = State(ctx.storage.sql)

        def initialize() -> None:
            target = getattr(env, "CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID", None)
            if (
                target
                and ctx.id.toString() == env.WORKSPACES.idFromName(target).toString()
            ):
                drop_empty_legacy_fact_tables(self.state)

        transaction_sync(ctx.storage, initialize)

    async def probe(self, raw: Any) -> dict[str, Any]:
        value = py_value(raw)
        action = value.get("action")
        sql = self.ctx.storage.sql
        if action == "seed":
            for table in TABLES:
                sql.exec(
                    "CREATE TABLE fact_schema(id INTEGER, version INTEGER)"
                    if table == "fact_schema"
                    else f"CREATE TABLE {table}(value TEXT)"
                )
            sql.exec("INSERT INTO fact_schema VALUES(1,1)")
            sql.exec("UPDATE sequence SET value=36")
            self.state.put("project", "preserve", {"display_name": "preserve"}, 36)
            if value.get("nonempty"):
                sql.exec(f"INSERT INTO {value['nonempty']} VALUES('preserve')")
            if value.get("schema"):
                sql.exec("UPDATE fact_schema SET version=99")
            if value.get("publication"):
                self.state.put(
                    "graph_publication", "preserve", {"project_id": "preserve"}, 36
                )
        error = None
        if action == "cleanup":
            try:
                transaction_sync(
                    self.ctx.storage,
                    lambda: drop_empty_legacy_fact_tables(self.state),
                )
            except Fault as failure:
                error = failure.code
        return {
            **legacy_fact_tables(self.state),
            "error": error,
            "snapshot": self.state.head(),
            "project": self.state.get("project", "preserve"),
            "all_tables": [
                {"name": str(js_get(row, "name"))}
                for row in rows(
                    sql.exec(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                )
            ],
        }


class Default(WorkerEntrypoint):
    async def fetch(self, request: Any) -> Response:
        return Response("qualification only")
