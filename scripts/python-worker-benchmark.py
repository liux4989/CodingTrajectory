"""Isolated instrumentation entrypoint for Python Worker integration harnesses."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from artifact_manifest import expand_manifest
from index import Default as ProductionDefault
from shared import bytes_from_buffer, js_get, py_value, rows
from workers import Response
from workspace import Workspace as ProductionWorkspace

R2_METRICS: dict[str, Any] = {"calls": {}, "uploadedBytes": 0}
R2_DELAY_MS = 0
LIST_GATE: dict[str, bool] | None = None
OBJECT_GATE: dict[str, Any] | None = None
INVOCATIONS: dict[str, int] = {}


async def _wait(milliseconds: int) -> None:
    try:
        from js import scheduler  # type: ignore[import-not-found]

        await scheduler.wait(milliseconds)
    except ImportError:
        await asyncio.sleep(milliseconds / 1000)


def _counter(value: Any, name: str) -> int:
    try:
        return int(getattr(value, name))
    except (AttributeError, TypeError, ValueError):
        return 0


class TracedCursor:
    def __init__(self, cursor: Any, group: dict[str, int]) -> None:
        self._cursor = cursor
        self._group = group
        self._read = 0
        self._written = 0
        self._capture()

    def _capture(self) -> None:
        read = _counter(self._cursor, "rowsRead")
        written = _counter(self._cursor, "rowsWritten")
        self._group["rowsRead"] += read - self._read
        self._group["rowsWritten"] += written - self._written
        self._read = read
        self._written = written

    @property
    def rowsRead(self) -> int:
        self._capture()
        return _counter(self._cursor, "rowsRead")

    @property
    def rowsWritten(self) -> int:
        self._capture()
        return _counter(self._cursor, "rowsWritten")

    def one(self) -> Any:
        try:
            return self._cursor.one()
        finally:
            self._capture()

    def toArray(self) -> Any:
        try:
            return self._cursor.toArray()
        finally:
            self._capture()

    def raw(self, *args: Any) -> TracedCursor:
        try:
            cursor = self._cursor.raw(*args)
        finally:
            self._capture()
        return TracedCursor(cursor, self._group)

    def __iter__(self):
        try:
            yield from self._cursor
        finally:
            self._capture()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class TracedSql:
    def __init__(self, sql: Any, groups: dict[str, dict[str, int]]) -> None:
        self._sql = sql
        self._groups = groups

    def exec(self, query: str, *bindings: Any) -> TracedCursor:
        cursor = self._sql.exec(query, *bindings)
        stripped = query.strip()
        match = re.match(
            r"^(?:INSERT(?: OR \w+)? INTO|DELETE FROM|UPDATE)\s+(\w+)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            label = f"{stripped.split()[0].upper()} {match.group(1)}"
        elif query.startswith("SELECT sha256,completion"):
            label = "publication claim lookups"
        elif query.startswith("SELECT descriptor FROM api_methods"):
            label = "publication retained index lookups"
        else:
            label = "queries"
        group = self._groups.setdefault(
            label, {"calls": 0, "rowsRead": 0, "rowsWritten": 0}
        )
        group["calls"] += 1
        return TracedCursor(cursor, group)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sql, name)


def _plain(value: Any) -> Any:
    value = py_value(value)
    if isinstance(value, dict):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


class TracedBucket:
    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    async def _call(self, operation: str, *args: Any) -> Any:
        calls = R2_METRICS["calls"]
        calls[operation] = calls.get(operation, 0) + 1
        if operation == "put":
            try:
                R2_METRICS["uploadedBytes"] += len(bytes_from_buffer(args[1]))
            except (TypeError, ValueError):
                if isinstance(args[1], str):
                    R2_METRICS["uploadedBytes"] += len(args[1].encode())
        if operation == "list" and LIST_GATE is not None and not LIST_GATE["entered"]:
            LIST_GATE["entered"] = True
            while not LIST_GATE["released"]:
                await _wait(1)
        gate = OBJECT_GATE
        if (
            gate is not None
            and operation == gate["operation"]
            and args
            and args[0] == gate["key"]
            and not gate["entered"]
        ):
            result = (
                None if gate["fail"] else await getattr(self._bucket, operation)(*args)
            )
            gate["entered"] = True
            while not gate["released"]:
                await _wait(1)
            if gate["fail"]:
                raise RuntimeError("local injected R2 failure")
            return result
        if R2_DELAY_MS:
            await _wait(R2_DELAY_MS)
        return await getattr(self._bucket, operation)(*args)

    async def get(self, *args: Any) -> Any:
        return await self._call("get", *args)

    async def head(self, *args: Any) -> Any:
        return await self._call("head", *args)

    async def put(self, *args: Any) -> Any:
        return await self._call("put", *args)

    async def list(self, *args: Any) -> Any:
        return await self._call("list", *args)

    async def delete(self, *args: Any) -> Any:
        return await self._call("delete", *args)


class BenchmarkEnv:
    def __init__(self, env: Any) -> None:
        self._env = env
        self.ARTIFACTS = TracedBucket(env.ARTIFACTS)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)


class Workspace(ProductionWorkspace):
    def __init__(self, ctx: Any, env: Any) -> None:
        super().__init__(ctx, BenchmarkEnv(env))

    async def invoke(self, method: str, envelope_json: str, principal_json: str) -> str:
        INVOCATIONS[method] = INVOCATIONS.get(method, 0) + 1
        return await super().invoke(method, envelope_json, principal_json)

    async def rpc(
        self, method: str, envelope: dict[str, Any], principal: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "ct_internal_artifact_reject":
            raise RuntimeError("local rejected invocation")
        sql = self.state.sql
        groups: dict[str, dict[str, int]] = {}
        before = int(js_get(sql.exec("SELECT total_changes() AS n").one(), "n"))
        self.state.sql = TracedSql(sql, groups)
        try:
            if method == "ct_collector_publish_artifacts" and getattr(
                self.env, "LOCAL_PUBLICATION_INPUT_GATE", False
            ):
                response = await self.ctx.blockConcurrencyWhile(
                    lambda: super(Workspace, self).rpc(method, envelope, principal)
                )
            else:
                response = await super().rpc(method, envelope, principal)
            changes = (
                int(js_get(sql.exec("SELECT total_changes() AS n").one(), "n")) - before
            )
            response["body"]["__benchmark"] = {
                "groups": groups,
                "logical_changes": changes,
                "database_bytes": int(sql.databaseSize),
            }
            return response
        finally:
            self.state.sql = sql

    async def set_artifact_claim_expiry(
        self, kind: str, sha256: str, expires_at: int
    ) -> int:
        return int(
            self.ctx.storage.sql.exec(
                "UPDATE artifact_upload_claims SET expires_at=? WHERE kind=? AND sha256=?",
                expires_at,
                kind,
                sha256,
            ).rowsWritten
        )

    async def artifact_claim_probe(self, raw: Any) -> list[Any]:
        value = _plain(raw)
        sql = self.ctx.storage.sql
        action = value.get("action")
        if action == "index-facts":
            prior = rows(
                sql.exec(
                    "SELECT descriptor FROM api_methods WHERE json_extract(descriptor,'$.index.sha256')=?",
                    value["sha256"],
                )
            )
            sql.exec(
                "UPDATE api_methods SET descriptor=json_set(descriptor,'$.publication_index',json(?)) "
                "WHERE json_extract(descriptor,'$.index.sha256')=?",
                json.dumps(value.get("completion"), separators=(",", ":")),
                value["sha256"],
            )
            return [
                json.loads(js_get(row, "descriptor")).get("publication_index")
                for row in prior
            ]
        if action == "legacy-manifests":
            for row in rows(
                sql.exec(
                    "SELECT project_id,publication_sequence,manifest FROM artifact_manifests"
                )
            ):
                manifest = expand_manifest(json.loads(js_get(row, "manifest")))
                sql.exec(
                    "UPDATE artifact_manifests SET manifest=? WHERE project_id=? AND publication_sequence=?",
                    json.dumps(manifest, separators=(",", ":")),
                    js_get(row, "project_id"),
                    js_get(row, "publication_sequence"),
                )
        elif action == "legacy":
            sql.exec(
                "DROP TABLE artifact_upload_claims;"
                "CREATE TABLE artifact_upload_claims(kind TEXT NOT NULL,sha256 TEXT NOT NULL,expires_at INTEGER NOT NULL,"
                "token TEXT,completion TEXT,PRIMARY KEY(kind,sha256));"
                "INSERT INTO artifact_upload_claims VALUES(?,?,4102444800,NULL,NULL)",
                value["kind"],
                value["sha256"],
            )
        elif action == "delete":
            sql.exec(
                "DELETE FROM artifact_upload_claims WHERE kind=? AND sha256=?",
                value["kind"],
                value["sha256"],
            )
        elif action == "completion":
            completion = value.get("completion")
            sql.exec(
                "UPDATE artifact_upload_claims SET completion=? WHERE kind=? AND sha256=?",
                None
                if completion is None
                else json.dumps(completion, separators=(",", ":")),
                value["kind"],
                value["sha256"],
            )
        elif action == "noise":
            for number in range(1, 1001):
                sql.exec(
                    "INSERT INTO artifact_upload_claims VALUES('facts',?,4102444800,'noise',NULL)",
                    f"{number:064x}",
                )
        elif action == "clear-noise":
            sql.exec("DELETE FROM artifact_upload_claims WHERE token='noise'")
        return [
            {
                "kind": str(js_get(row, "kind")),
                "sha256": str(js_get(row, "sha256")),
                "expires_at": int(js_get(row, "expires_at")),
                "token": js_get(row, "token"),
                "completion": js_get(row, "completion"),
            }
            for row in rows(
                sql.exec(
                    "SELECT * FROM artifact_upload_claims WHERE kind=? AND sha256=?",
                    value["kind"],
                    value["sha256"],
                )
            )
        ]


class Default(ProductionDefault):
    def __init__(self, ctx: Any, env: Any) -> None:
        super().__init__(ctx, BenchmarkEnv(env))

    async def fetch(self, request: Any) -> Response:
        global R2_METRICS, R2_DELAY_MS, LIST_GATE, OBJECT_GATE
        parsed = urlsplit(request.url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/__benchmark/invocations":
            return Response.from_json(INVOCATIONS)
        if parsed.path == "/__benchmark/r2":
            result = json.loads(json.dumps(R2_METRICS))
            if "reset" in query:
                R2_METRICS = {"calls": {}, "uploadedBytes": 0}
            if "delay" in query:
                R2_DELAY_MS = int(query["delay"][0])
            return Response.from_json(result)
        if parsed.path == "/__benchmark/object-gate":
            if request.method == "POST":
                value = _plain(await request.json())
                OBJECT_GATE = {**value, "entered": False, "released": False}
            elif request.method == "DELETE" and OBJECT_GATE is not None:
                OBJECT_GATE["released"] = True
            return Response.from_json(
                {"entered": bool(OBJECT_GATE and OBJECT_GATE["entered"])}
            )
        if parsed.path == "/__benchmark/list-gate":
            if request.method == "POST":
                LIST_GATE = {"entered": False, "released": False}
            elif request.method == "DELETE" and LIST_GATE is not None:
                LIST_GATE["released"] = True
            return Response.from_json(
                {"entered": bool(LIST_GATE and LIST_GATE["entered"])}
            )
        stub = self.env.WORKSPACES.getByName("00000000-0000-0000-0000-000000000001")
        if parsed.path == "/__benchmark/claim-probe":
            return Response.from_json(
                await stub.artifact_claim_probe(await request.json())
            )
        if parsed.path == "/__benchmark/claim-expiry" and request.method == "POST":
            value = _plain(await request.json())
            written = await stub.set_artifact_claim_expiry(
                value["kind"], value["sha256"], value["expiresAt"]
            )
            return Response.from_json({"rowsWritten": written})
        if parsed.path == "/__benchmark/internal":
            value = _plain(await request.json())
            principal = {
                "workspace_id": value["request"]["workspace_id"],
                "agent_id": "00000000-0000-0000-0000-000000000002",
                "roles": ["owner"],
            }
            if value["method"] == "ct_internal_artifact_reject":
                try:
                    await stub.invoke(
                        value["method"],
                        json.dumps({"request": value["request"]}),
                        json.dumps(principal),
                    )
                except Exception:  # noqa: BLE001 -- qualification requires rejected RPC
                    return Response.from_json({"rejected": True})
                raise RuntimeError("expected invocation rejection")
            return Response(
                await stub.invoke(
                    value["method"],
                    json.dumps({"request": value["request"]}),
                    json.dumps(principal),
                )
            )
        return await super().fetch(request)
