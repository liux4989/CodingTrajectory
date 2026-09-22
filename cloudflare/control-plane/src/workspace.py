"""Workspace Durable Object: authorization, transactions, and revision boundary."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from artifact_manifest import expand_manifest
from artifacts import (
    artifact_manifests,
    artifact_read_locator,
    artifact_readiness,
    claim_artifact_upload,
    cleanup_artifact_objects,
    commit_artifact_publication,
    complete_artifact_upload,
    initialize_artifacts,
    prepare_artifact_publication,
    prune_artifact_receipts,
)
from collector import checkpoint, recovery, register_project, register_source
from legacy_cleanup import drop_empty_legacy_fact_tables, legacy_fact_tables
from living import living_read, living_write
from prepared_api import api_locator, initialize_api
from replacement import (
    delete_workspace_artifact_prefix,
    initialize_replacement,
    mark_workspace_replacement,
    preview_workspace_replacement,
    replacement_prefix,
    workspace_replacement,
)
from shared import (
    Fault,
    State,
    authority_failure,
    digest,
    js_get,
    parse_timestamp,
    require_that,
    rows,
    stable,
    validate_contract,
)
from workers import DurableObject

Json = dict[str, Any]
Principal = dict[str, Any]
REPLACEMENT_MUTATIONS = {
    "ct_project_register",
    "ct_collector_register_source",
    "ct_collector_publish_observation",
    "ct_collector_publish_artifacts",
    "ct_collector_heartbeat",
    "ct_collector_publish_living_observation",
    "ct_internal_artifact_claim",
    "ct_internal_artifact_complete",
}


class Workspace(DurableObject):
    def __init__(self, ctx: Any, env: Any) -> None:
        super().__init__(ctx, env)
        self.state = State(ctx.storage.sql)
        self._invocation_lock = asyncio.Lock()
        self._initialize()

    def _initialize(self) -> None:
        def initialize() -> None:
            initialize_artifacts(self.state)
            initialize_api(self.state)
            initialize_replacement(self.state)
            target = getattr(self.env, "CT_LEGACY_FACT_CLEANUP_WORKSPACE_ID", None)
            if target:
                expected = self.env.WORKSPACES.idFromName(target).toString()
                if self.ctx.id.toString() == expected:
                    drop_empty_legacy_fact_tables(self.state)

        self.ctx.storage.transactionSync(initialize)

    async def fetch(self, request: Any):
        from http_handler import HttpHandler

        return await HttpHandler(_HttpEnvironment(self)).fetch(request)

    async def invoke(self, method: str, envelope_json: str, principal_json: str) -> str:
        envelope = json.loads(envelope_json)
        principal = json.loads(principal_json)
        # A DO input gate does not preserve an operation across R2 awaits. Keep
        # source/claim/publication/cleanup calls ordered for this instance.
        async with self._invocation_lock:
            result = await self.rpc(method, envelope, principal)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    async def _execute_replacement(self, request: Json) -> Json:
        prior = workspace_replacement(self.state)
        same = bool(
            prior
            and prior["workspace_id"] == request["workspace_id"]
            and prior["export_sha256"] == request["expected_export_sha256"]
        )
        if prior and same and prior["status"] == "complete":
            return {
                "status": 200,
                "body": {
                    "workspace_id": request["workspace_id"],
                    "sql_reset": False,
                    "prefix": replacement_prefix(request["workspace_id"]),
                    "deleted": 0,
                    "complete": True,
                    "already_complete": True,
                },
            }
        reset_sql = not same
        if not same:
            await self.ctx.storage.deleteAll()
            self.state = State(self.ctx.storage.sql)
            self._initialize()
            mark_workspace_replacement(
                self.state,
                request["workspace_id"],
                request["expected_export_sha256"],
                "incomplete",
            )
        deletion = await delete_workspace_artifact_prefix(
            self.env, request["workspace_id"]
        )
        mark_workspace_replacement(
            self.state,
            request["workspace_id"],
            request["expected_export_sha256"],
            "complete" if deletion["complete"] else "incomplete",
        )
        return {
            "status": 200,
            "body": {
                "workspace_id": request["workspace_id"],
                "sql_reset": reset_sql,
                **deletion,
                "already_complete": False,
            },
        }

    async def rpc(self, method: str, envelope: Json, principal: Principal) -> Json:
        try:
            request = envelope["request"]
            require_that(
                request["workspace_id"] == principal["workspace_id"],
                "workspace_denied",
                403,
            )
            if method == "ct_workspace_replace":
                require_that("owner" in principal["roles"], "capability_required", 403)
                replacement_workspace = getattr(
                    self.env, "CT_REPLACEMENT_WORKSPACE_ID", None
                )
                replacement_export = getattr(
                    self.env, "CT_REPLACEMENT_EXPORT_SHA256", None
                )
                require_that(
                    replacement_workspace
                    and request["workspace_id"] == replacement_workspace,
                    "workspace_replacement_target_denied",
                    403,
                )
                require_that(
                    replacement_export
                    and request["expected_export_sha256"] == replacement_export,
                    "workspace_replacement_export_denied",
                    403,
                )
                require_that(
                    request.get("confirmation")
                    == f"{request.get('mode')}:{request['workspace_id']}:{request['expected_export_sha256']}",
                    "workspace_replacement_confirmation_required",
                    403,
                )
                expected_object = self.env.WORKSPACES.idFromName(
                    request["workspace_id"]
                ).toString()
                require_that(
                    self.ctx.id.toString() == expected_object,
                    "workspace_replacement_target_denied",
                    403,
                )
                if request.get("mode") == "preview":
                    return {
                        "status": 200,
                        "body": await preview_workspace_replacement(
                            self.state,
                            self.env,
                            request["workspace_id"],
                            request["expected_export_sha256"],
                        ),
                    }
                require_that(
                    request.get("mode") == "execute",
                    "workspace_replacement_mode_invalid",
                )
                return await self.ctx.blockConcurrencyWhile(
                    lambda: self._execute_replacement(request)
                )

            replacement = workspace_replacement(self.state)
            require_that(
                not replacement
                or replacement["status"] != "incomplete"
                or method not in REPLACEMENT_MUTATIONS,
                "workspace_replacement_incomplete",
                409,
            )
            if method in {
                "ct_internal_artifact_claim",
                "ct_internal_artifact_complete",
            }:
                require_that(
                    "collect" in principal["roles"] or "owner" in principal["roles"],
                    "capability_required",
                    403,
                )
                require_that(
                    request.get("kind") in {"facts", "summary", "api"}
                    and isinstance(request.get("sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", request["sha256"]),
                    "invalid_artifact_claim",
                )
                if method == "ct_internal_artifact_claim":
                    token = self.ctx.storage.transactionSync(
                        lambda: claim_artifact_upload(
                            self.state, request["kind"], request["sha256"]
                        )
                    )
                    return {"status": 200, "body": {"token": token}}
                self.ctx.storage.transactionSync(
                    lambda: complete_artifact_upload(self.state, request)
                )
                return {"status": 200, "body": {}}
            if method == "ct_internal_api_locator":
                require_that(
                    "read" in principal["roles"] or "owner" in principal["roles"],
                    "capability_required",
                    403,
                )
                return {"status": 200, "body": api_locator(self.state, request)}

            # The idempotency identity covers raw JSON before defaults and typed
            # normalization alter its representation.
            identity = await digest(stable(request))
            if (
                method == "ct_collector_publish_artifacts"
                and request.get("schema_version") == "ct.artifact-manifest.v3"
            ):
                request = validate_contract("compact_publication", request)
                request = expand_manifest(request)
            request = validate_contract(method, request)
            if method == "ct_collector_artifact_readiness":
                require_that(
                    "collect" in principal["roles"] or "owner" in principal["roles"],
                    "capability_required",
                    403,
                )
                return {"status": 200, "body": artifact_readiness(self.state, request)}
            if method == "ct_artifact_read":
                return {
                    "status": 200,
                    "body": artifact_read_locator(self.state, request),
                }
            if method == "ct_remote_living":
                return {
                    "status": 200,
                    "body": await living_read(
                        self.state, request, self.env.CT_CURSOR_KEY
                    ),
                }
            if method == "ct_collector_publish_artifacts":
                key = (
                    stable([principal["agent_id"], method, envelope["idempotency_key"]])
                    if envelope.get("idempotency_key")
                    else None
                )
                prior = self.state.get("receipt", key) if key else None
                if prior:
                    require_that(
                        prior["identity"] == identity, "idempotency_conflict", 409
                    )
                    return {"status": 200, "body": prior["result"]}
                plan = await prepare_artifact_publication(self.state, self.env, request)

                def commit() -> Json:
                    concurrent = self.state.get("receipt", key) if key else None
                    if concurrent:
                        require_that(
                            concurrent["identity"] == identity,
                            "idempotency_conflict",
                            409,
                        )
                        return concurrent["result"]
                    result = commit_artifact_publication(self.state, request, plan)
                    if key:
                        self.state.put(
                            "receipt",
                            key,
                            {"identity": identity, "result": result},
                            self.state.head(),
                        )
                    prune_artifact_receipts(self.state)
                    return result

                body = self.ctx.storage.transactionSync(commit)
                try:
                    await cleanup_artifact_objects(
                        self.state, self.env, request["workspace_id"]
                    )
                except Exception:  # noqa: BLE001 -- cleanup is explicitly best-effort
                    print(
                        json.dumps({"event": "artifact_cleanup_deferred"}),
                        file=sys.stderr,
                    )
                return {"status": 200, "body": body}
            if method == "ct_collector_publish_observation":
                source_checkpoint = request["payload"].get("source_checkpoint")
                segments = (
                    source_checkpoint.get("segments", []) if source_checkpoint else []
                )
                require_that(
                    all(
                        isinstance(offset, int)
                        and not isinstance(offset, bool)
                        and offset > 0
                        for offset in segments
                    ),
                    "invalid_checkpoint_offsets",
                )
                require_that(
                    await digest(stable(request["payload"]))
                    == request["content_sha256"]
                    and request["event_id"]
                    == f"checkpoint:{request['content_sha256']}",
                    "checkpoint_digest_mismatch",
                )

            def dispatch_transaction() -> Json:
                key = (
                    stable([principal["agent_id"], method, envelope["idempotency_key"]])
                    if envelope.get("idempotency_key")
                    else None
                )
                prior = self.state.get("receipt", key) if key else None
                if prior:
                    require_that(
                        prior["identity"] == identity, "idempotency_conflict", 409
                    )
                    return prior["result"]
                result = self.dispatch(method, request, principal)
                if key:
                    self.state.put(
                        "receipt",
                        key,
                        {"identity": identity, "result": result},
                        self.state.head(),
                    )
                return result

            body = self.ctx.storage.transactionSync(dispatch_transaction)
            return {"status": 200, "body": body}
        except Exception as error:  # noqa: BLE001 -- sanitize every platform boundary failure
            failure = authority_failure(error, "workspace")
            return {"status": failure.status, "body": {"error": {"code": failure.code}}}

    def dispatch(self, method: str, request: Json, principal: Principal) -> Json:
        if method == "ct_project_register":
            return register_project(self.state, request)
        if method == "ct_collector_register_source":
            return register_source(self.state, request)
        if method == "ct_collector_recover":
            return recovery(self.state, request)
        if method == "ct_collector_publish_observation":
            request["payload"] = validate_contract("checkpoint", request["payload"])
            return checkpoint(self.state, request)
        if method in {
            "ct_collector_heartbeat",
            "ct_collector_publish_living_observation",
        }:
            return living_write(self.state, method, request)
        if method == "ct_workspace_snapshot":
            return {
                "workspace_id": request["workspace_id"],
                "snapshot_sequence": self.state.pin(request.get("snapshot_sequence")),
            }
        if method == "ct_legacy_fact_cleanup_status":
            return {
                "workspace_id": request["workspace_id"],
                "snapshot_sequence": self.state.head(),
                **legacy_fact_tables(self.state),
            }
        if method == "ct_artifact_manifest":
            return artifact_manifests(self.state, request)
        if method == "ct_project_inventory_snapshot":
            sequence = self.state.pin(request.get("snapshot_sequence"))
            artifact_rows = rows(
                self.state.sql.exec(
                    """SELECT project_id,manifest FROM artifact_manifests current WHERE workspace_sequence=(
                    SELECT MAX(candidate.workspace_sequence) FROM artifact_manifests candidate
                    WHERE candidate.project_id=current.project_id AND candidate.workspace_sequence<=?)""",
                    sequence,
                )
            )
            expected = [
                row
                for row in self.state.all("artifact_project_publisher", sequence)
                if row["first_sequence"] <= sequence
            ]
            if len(artifact_rows) != len(expected):
                raise Fault(410, "artifact_snapshot_expired")
            artifact_graphs: list[Json] = []
            for row in artifact_rows:
                for graph in json.loads(js_get(row, "manifest"))["graphs"]:
                    artifact_graphs.append(
                        {
                            "project_id": js_get(row, "project_id"),
                            "vendors": graph["vendors"],
                        }
                    )
            modified_since = (
                parse_timestamp(request["modified_since"]).timestamp()
                if request.get("modified_since")
                else None
            )
            projects = []
            for row in self.state.all("project", sequence):
                if (
                    modified_since is not None
                    and parse_timestamp(row["modified_at"]).timestamp() < modified_since
                ):
                    continue
                vendors = sorted(
                    {
                        vendor
                        for graph in artifact_graphs
                        if graph["project_id"] == row["project_id"]
                        for vendor in graph["vendors"]
                    }
                )
                project = {**row, "vendors": vendors}
                if (
                    request.get("agent_vendor")
                    and request["agent_vendor"] not in vendors
                ):
                    continue
                projects.append(project)
            projects.sort(key=lambda item: item["display_name"])
            return {
                "workspace_id": request["workspace_id"],
                "snapshot_sequence": sequence,
                "projects": projects,
            }
        raise Fault(404, "not_found")


class _LocalWorkspaceNamespace:
    """Resolve HTTP operations to this object without a second RPC hop."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def getByName(self, name: str) -> Workspace:
        expected = self.workspace.env.WORKSPACES.idFromName(name).toString()
        require_that(
            expected == self.workspace.ctx.id.toString(), "workspace_denied", 403
        )
        return self.workspace


class _HttpEnvironment:
    def __init__(self, workspace: Workspace) -> None:
        self._env = workspace.env
        self.WORKSPACES = _LocalWorkspaceNamespace(workspace)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)
