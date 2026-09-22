"""Immutable R2 artifact claims, publication, manifests, and cleanup."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from artifact_manifest import compact_graph, expand_manifest
from prepared_api import api_view, commit_api_view, prepare_inventory, prune_api
from shared import (
    Fault,
    State,
    bytes_from_buffer,
    digest,
    js_get,
    js_value,
    receipt,
    require_that,
    rows,
    stable,
    validate_contract,
)

Json = dict[str, Any]
MANIFEST_SCHEMA = "ct.artifact-manifest.v3"
RETAINED_MANIFESTS = 3
CLEANUP_PAGES_PER_PUBLICATION = 4
UPLOAD_CLAIM_SECONDS = 7 * 24 * 60 * 60


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def artifact_key(workspace_id: str, kind: str, sha256: str) -> str:
    return f"workspaces/{workspace_id}/artifacts/{kind}/{sha256}"


def initialize_artifacts(state: State) -> None:
    state.sql.exec(
        """CREATE TABLE IF NOT EXISTS artifact_manifests (
        project_id TEXT NOT NULL, publication_sequence INTEGER NOT NULL,
        workspace_sequence INTEGER NOT NULL UNIQUE, agent_id TEXT NOT NULL,
        manifest TEXT NOT NULL,
        PRIMARY KEY(project_id, publication_sequence));
        CREATE INDEX IF NOT EXISTS artifact_manifests_snapshot
          ON artifact_manifests(workspace_sequence);
        CREATE TABLE IF NOT EXISTS artifact_cleanup (
          workspace_id TEXT PRIMARY KEY, cursor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifact_upload_claims (
          kind TEXT NOT NULL, sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL,
          PRIMARY KEY(kind, sha256));"""
    )
    columns = rows(state.sql.exec("PRAGMA table_info(artifact_upload_claims)"))
    if not any(js_get(column, "name") == "token" for column in columns):
        state.sql.exec(
            """ALTER TABLE artifact_upload_claims ADD COLUMN token TEXT;
            ALTER TABLE artifact_upload_claims ADD COLUMN completion TEXT;"""
        )


def claim_artifact_upload(state: State, kind: str, sha256: str) -> str:
    now = int(time.time())
    row = state.sql.exec(
        """INSERT INTO artifact_upload_claims(kind,sha256,expires_at,token)
        VALUES(?,?,?,?) ON CONFLICT(kind,sha256) DO UPDATE SET
        token=CASE WHEN expires_at<=? OR token IS NULL THEN excluded.token ELSE token END,
        completion=CASE WHEN expires_at<=? THEN NULL ELSE completion END,
        expires_at=excluded.expires_at RETURNING token""",
        kind,
        sha256,
        now + UPLOAD_CLAIM_SECONDS,
        str(uuid.uuid4()),
        now,
        now,
    ).one()
    return str(js_get(row, "token"))


def complete_artifact_upload(state: State, request: Json) -> None:
    updated = state.sql.exec(
        """UPDATE artifact_upload_claims SET completion=?
        WHERE kind=? AND sha256=? AND token=? AND expires_at>?""",
        stable(
            {
                "workspace_id": request["workspace_id"],
                "bytes": request["bytes"],
                "index": request.get("index"),
            }
        ),
        request["kind"],
        request["sha256"],
        request["token"],
        int(time.time()),
    )
    require_that(js_get(updated, "rowsWritten", 0) == 1, "artifact_claim_expired", 409)


def _retained_objects(state: State) -> dict[str, int]:
    retained: dict[str, int] = {}
    for row in rows(state.sql.exec("SELECT manifest FROM artifact_manifests")):
        manifest = expand_manifest(json.loads(js_get(row, "manifest")))
        for graph in manifest["graphs"]:
            for reference in [
                graph["facts"],
                graph["summary"],
                *graph.get("api_objects", []),
            ]:
                retained[f"{reference['kind']}:{reference['sha256']}"] = reference[
                    "bytes"
                ]
    return retained


def artifact_readiness(state: State, request: Json) -> Json:
    completed: dict[str, Json] = {}
    now = int(time.time())
    for kind in ("facts", "summary", "api"):
        hashes = list(
            dict.fromkeys(
                reference["sha256"]
                for reference in request["objects"]
                if reference["kind"] == kind
            )
        )
        for offset in range(0, len(hashes), 50):
            batch = hashes[offset : offset + 50]
            placeholders = ",".join("?" for _ in batch)
            query = (
                f"SELECT sha256,completion FROM artifact_upload_claims WHERE kind=? "
                f"AND sha256 IN ({placeholders}) AND expires_at>? AND completion IS NOT NULL"
            )
            for row in rows(state.sql.exec(query, kind, *batch, now)):
                completion = json.loads(js_get(row, "completion"))
                if completion["workspace_id"] == request["workspace_id"]:
                    completed[f"{kind}:{js_get(row, 'sha256')}"] = completion
    ready = []
    for reference in request["objects"]:
        completion = completed.get(f"{reference['kind']}:{reference['sha256']}")
        ready.append(
            bool(
                completion
                and completion["bytes"] == reference["bytes"]
                and (not reference.get("requires_index") or completion.get("index"))
            )
        )
    if all(ready):
        return {"ready": ready}
    retained = _retained_objects(state)
    indexes: set[str] = set()
    hashes = [
        reference["sha256"]
        for index, reference in enumerate(request["objects"])
        if not ready[index]
        and reference.get("requires_index")
        and retained.get(f"{reference['kind']}:{reference['sha256']}")
        == reference["bytes"]
    ]
    for offset in range(0, len(hashes), 50):
        batch = hashes[offset : offset + 50]
        placeholders = ",".join("?" for _ in batch)
        for row in rows(
            state.sql.exec(
                f"SELECT descriptor FROM api_methods WHERE json_extract(descriptor,'$.index.sha256') IN ({placeholders})",
                *batch,
            )
        ):
            descriptor = json.loads(js_get(row, "descriptor"))
            if descriptor.get("publication_index"):
                indexes.add(descriptor["index"]["sha256"])
    return {
        "ready": [
            ready[index]
            or (
                retained.get(f"{reference['kind']}:{reference['sha256']}")
                == reference["bytes"]
                and (
                    not reference.get("requires_index")
                    or reference["sha256"] in indexes
                )
            )
            for index, reference in enumerate(request["objects"])
        ]
    }


async def prepare_artifact_publication(state: State, env: Any, request: Json) -> Json:
    request = validate_contract("ct_collector_publish_artifacts", request)
    require_that(
        request["inventory_state"] == "complete", "artifact_inventory_incomplete"
    )
    require_that(state.get("project", request["project_id"]), "project_not_found", 404)
    owner = state.get("artifact_project_publisher", request["project_id"])
    require_that(
        not owner or owner["agent_id"] == request["agent_id"],
        "project_publisher_conflict",
        409,
    )
    vector = {entry["source_id"]: entry for entry in request["source_vector"]}
    require_that(
        len(vector) == len(request["source_vector"]), "duplicate_source_vector"
    )
    for entry in vector.values():
        source = state.get("source", entry["source_id"])
        checkpoint_record = state.get(
            "checkpoint",
            f"{entry['source_id']}:{entry['source_epoch']}:{entry['source_sequence']}",
        )
        require_that(
            source
            and source["agent_id"] == request["agent_id"]
            and source.get("project_id") == request["project_id"]
            and source["source_epoch"] == entry["source_epoch"]
            and source["committed_source_sequence"] == entry["source_sequence"]
            and checkpoint_record
            and checkpoint_record["content_sha256"] == entry["content_sha256"],
            "source_vector_requires_accepted_project_checkpoints",
        )
    retained = _retained_objects(state)
    completed: dict[str, Json] = {}
    indexes: dict[str, Json] = {}
    now = int(time.time())
    for kind in ("facts", "summary", "api"):
        hashes = list(
            dict.fromkeys(
                reference["sha256"]
                for graph in request["graphs"]
                for reference in [
                    graph["facts"],
                    graph["summary"],
                    *graph["api_objects"],
                ]
                if reference["kind"] == kind
            )
        )
        for offset in range(0, len(hashes), 50):
            batch = hashes[offset : offset + 50]
            placeholders = ",".join("?" for _ in batch)
            for row in rows(
                state.sql.exec(
                    f"SELECT sha256,completion FROM artifact_upload_claims WHERE kind=? AND sha256 IN ({placeholders}) AND expires_at>? AND completion IS NOT NULL",
                    kind,
                    *batch,
                    now,
                )
            ):
                completion = json.loads(js_get(row, "completion"))
                if completion["workspace_id"] != request["workspace_id"]:
                    continue
                sha256 = str(js_get(row, "sha256"))
                completed[f"{kind}:{sha256}"] = completion
                if completion.get("index"):
                    indexes[sha256] = completion["index"]
    index_hashes = list(
        dict.fromkeys(
            method["index"]["sha256"]
            for graph in request["graphs"]
            for method in graph["api_methods"]
            if method.get("index") and method["index"]["sha256"] not in indexes
        )
    )
    for offset in range(0, len(index_hashes), 50):
        batch = index_hashes[offset : offset + 50]
        placeholders = ",".join("?" for _ in batch)
        for row in rows(
            state.sql.exec(
                f"SELECT descriptor FROM api_methods WHERE json_extract(descriptor,'$.index.sha256') IN ({placeholders})",
                *batch,
            )
        ):
            descriptor = json.loads(js_get(row, "descriptor"))
            if descriptor.get("publication_index"):
                indexes[descriptor["index"]["sha256"]] = descriptor["publication_index"]
    release_claims: list[Json] = []
    views: list[Json] = []
    cards: list[Json] = []
    graph_ids: set[str] = set()
    for graph in request["graphs"]:
        require_that(graph["graph_id"] not in graph_ids, "duplicate_graph_publication")
        graph_ids.add(graph["graph_id"])
        require_that(
            all(source_id in vector for source_id in graph["source_ids"]),
            "invalid_graph_sources",
        )
        keys: set[str] = set()
        references = {
            reference["sha256"]: reference["bytes"]
            for reference in graph["api_objects"]
        }
        for method in graph["api_methods"]:
            key = stable([method["method"], method["scope"], method.get("turn_id")])
            require_that(
                key not in keys
                and bool(method.get("index")) != bool(method.get("error")),
                "invalid_prepared_method",
            )
            keys.add(key)
            require_that(
                not method.get("index")
                or references.get(method["index"]["sha256"])
                == method["index"]["bytes"],
                "invalid_prepared_reference",
            )
            if method.get("index"):
                require_that(
                    method["index"]["bytes"] <= 64 * 1024, "invalid_prepared_reference"
                )
                index = indexes.get(method["index"]["sha256"])
                require_that(index, "artifact_upload_incomplete", 409)
                require_that(
                    index["source_manifest_sha256"] == graph["fact_set_digest"]
                    and index["method"] == method["method"]
                    and index["method_version"] == method["method_version"]
                    and index["scope"] == method["scope"]
                    and index.get("turn_id") == method.get("turn_id"),
                    "invalid_prepared_reference",
                )
                require_that(
                    all(
                        references.get(reference["sha256"]) == reference["bytes"]
                        for reference in index["references"]
                    ),
                    "invalid_prepared_reference",
                )
        for reference in [graph["facts"], graph["summary"], *graph["api_objects"]]:
            key = f"{reference['kind']}:{reference['sha256']}"
            completion = completed.get(key)
            require_that(
                retained.get(key) == reference["bytes"]
                or (completion and completion["bytes"] == reference["bytes"]),
                "artifact_upload_incomplete",
                409,
            )
            if completion:
                release_claims.append(
                    {"kind": reference["kind"], "sha256": reference["sha256"]}
                )
        summary_object = await env.ARTIFACTS.get(
            artifact_key(request["workspace_id"], "summary", graph["summary"]["sha256"])
        )
        require_that(
            summary_object
            and js_get(summary_object, "size") == graph["summary"]["bytes"],
            "artifact_upload_incomplete",
            409,
        )
        body = bytes_from_buffer(await summary_object.arrayBuffer())
        require_that(
            await digest(body) == graph["summary"]["sha256"],
            "artifact_object_corrupt",
            503,
        )
        try:
            summary = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Fault(409, "unsupported_prepared_version") from exc
        require_that(
            summary.get("schema_version") == "ct.prepared-summary.v2"
            and summary.get("fact_set_digest") == graph["fact_set_digest"]
            and summary.get("graph_id") == graph["graph_id"]
            and isinstance(summary.get("project_sessions"), list),
            "unsupported_prepared_version",
            409,
        )
        view = await api_view(
            request["workspace_id"],
            graph["fact_set_digest"],
            graph["api_methods"],
            request["project_id"],
        )
        cards.extend(
            {
                **card,
                "project_id": request["project_id"],
                "view_manifest_sha256": view["view_manifest_sha256"],
            }
            for card in summary["project_sessions"]
        )
        views.append(view)
    all_cards = []
    for row in rows(
        state.sql.exec(
            "SELECT cards FROM api_inventory_cards WHERE project_id<>?",
            request["project_id"],
        )
    ):
        all_cards.extend(json.loads(js_get(row, "cards")))
    all_cards.extend(cards)
    projects = []
    for project in state.all("project"):
        project_cards = [
            card for card in all_cards if card["project_id"] == project["project_id"]
        ]
        modified = sorted(
            card["modified"] for card in project_cards if card.get("modified")
        )
        projects.append(
            {
                "project_id": project["project_id"],
                "display_name": project["display_name"],
                "vendors": sorted(
                    {
                        vendor
                        for card in project_cards
                        for vendor in card.get("vendors", [])
                    }
                ),
                "modified": modified[-1] if modified else None,
            }
        )
    inventory = await prepare_inventory(
        env, request["workspace_id"], projects, all_cards
    )
    return {
        "complete": True,
        "releaseClaims": release_claims,
        "views": views,
        "cards": cards,
        "indexes": indexes,
        "inventory": inventory,
    }


def commit_artifact_publication(state: State, request: Json, plan: Json) -> Json:
    require_that(plan["complete"], "artifact_upload_incomplete", 409)
    publisher = state.get("artifact_project_publisher", request["project_id"])
    current = publisher.get("publication_sequence", -1) if publisher else -1
    if request["publication_sequence"] <= current:
        return receipt(
            "conflict",
            publisher.get("committed_sequence") if publisher else None,
            {"reason": "stale_publication_sequence"},
        )
    require_that(
        request["publication_sequence"] == current + 1, "publication_sequence_gap", 409
    )
    sequence = state.next()
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "preparation_version": request["preparation_version"],
        "workspace_id": request["workspace_id"],
        "project_id": request["project_id"],
        "publisher_agent_id": request["agent_id"],
        "publication_sequence": request["publication_sequence"],
        "snapshot_sequence": sequence,
        "published_at": _now_iso(),
        "inventory_state": "complete",
        "graphs": [
            compact_graph(
                {
                    key: graph[key]
                    for key in (
                        "graph_id",
                        "fact_set_digest",
                        "fact_count",
                        "observed_at",
                        "vendors",
                        "facts",
                        "summary",
                        "api_methods",
                        "api_objects",
                    )
                }
            )
            for graph in request["graphs"]
        ],
    }
    stored = stable(manifest)
    require_that(
        len(stored.encode("utf-8")) <= 2 * 1024 * 1024 - 4096,
        "artifact_manifest_too_large",
        413,
    )
    state.sql.exec(
        "INSERT INTO artifact_manifests VALUES(?,?,?,?,?)",
        request["project_id"],
        request["publication_sequence"],
        sequence,
        request["agent_id"],
        stored,
    )
    for index, graph in enumerate(request["graphs"]):
        commit_api_view(
            state,
            request["project_id"],
            sequence,
            plan["views"][index],
            graph["api_methods"],
            plan["indexes"],
        )
    state.sql.exec(
        "INSERT OR REPLACE INTO api_inventory_cards VALUES(?,?)",
        request["project_id"],
        stable(plan["cards"]),
    )
    commit_api_view(
        state,
        "workspace",
        sequence,
        plan["inventory"]["identity"],
        plan["inventory"]["methods"],
    )
    for sha256 in plan["inventory"]["objects"]:
        state.sql.exec(
            "INSERT OR IGNORE INTO api_inventory_objects VALUES(?,?)",
            plan["inventory"]["identity"]["view_manifest_sha256"],
            sha256,
        )
    state.sql.exec(
        """DELETE FROM artifact_manifests WHERE project_id=? AND publication_sequence NOT IN (
        SELECT publication_sequence FROM artifact_manifests WHERE project_id=?
        ORDER BY publication_sequence DESC LIMIT ?)""",
        request["project_id"],
        request["project_id"],
        RETAINED_MANIFESTS,
    )
    state.put(
        "artifact_project_publisher",
        request["project_id"],
        {
            "project_id": request["project_id"],
            "agent_id": request["agent_id"],
            "publication_sequence": request["publication_sequence"],
            "committed_sequence": sequence,
            "first_sequence": publisher.get("first_sequence", sequence)
            if publisher
            else sequence,
        },
        sequence,
    )
    state.sql.exec(
        """DELETE FROM records WHERE kind='artifact_project_publisher' AND key=?
        AND sequence NOT IN (
          SELECT MIN(sequence) FROM records WHERE kind='artifact_project_publisher' AND key=?
          UNION SELECT workspace_sequence FROM artifact_manifests WHERE project_id=?)""",
        request["project_id"],
        request["project_id"],
        request["project_id"],
    )
    prune_api(state)
    for kind in ("facts", "summary", "api"):
        hashes = [
            claim["sha256"] for claim in plan["releaseClaims"] if claim["kind"] == kind
        ]
        for offset in range(0, len(hashes), 50):
            batch = hashes[offset : offset + 50]
            placeholders = ",".join("?" for _ in batch)
            state.sql.exec(
                f"DELETE FROM artifact_upload_claims WHERE completion IS NOT NULL AND kind=? AND sha256 IN ({placeholders})",
                kind,
                *batch,
            )
    return receipt(
        "accepted",
        sequence,
        {
            "publication_outcome": "published",
            "graphs_published": len(request["graphs"]),
            "retention": RETAINED_MANIFESTS,
        },
    )


def prune_artifact_receipts(state: State) -> None:
    state.sql.exec(
        """DELETE FROM records WHERE kind='receipt'
        AND json_extract(payload, '$.result.details.retention')=?
        AND json_extract(payload, '$.result.committed_sequence') NOT IN (
          SELECT workspace_sequence FROM artifact_manifests)""",
        RETAINED_MANIFESTS,
    )


def artifact_manifests(state: State, request: Json) -> Json:
    request = validate_contract("ct_artifact_manifest", request)
    sequence = state.pin(request.get("snapshot_sequence"))
    arguments: list[Any] = [sequence]
    project_filter = ""
    if request.get("project_id"):
        project_filter = " AND project_id=?"
        arguments.append(request["project_id"])
    result = rows(
        state.sql.exec(
            f"""SELECT manifest FROM artifact_manifests current
            WHERE workspace_sequence=(SELECT MAX(candidate.workspace_sequence) FROM artifact_manifests candidate
              WHERE candidate.project_id=current.project_id AND candidate.workspace_sequence<=?){project_filter}
            ORDER BY project_id""",
            *arguments,
        )
    )
    expected = [
        row
        for row in state.all("artifact_project_publisher", sequence)
        if row["first_sequence"] <= sequence
        and (
            not request.get("project_id") or row["project_id"] == request["project_id"]
        )
    ]
    if len(result) != len(expected):
        raise Fault(410, "artifact_snapshot_expired")
    if not result:
        raise Fault(404, "artifact_snapshot_unavailable")
    return {
        "workspace_id": request["workspace_id"],
        "snapshot_sequence": sequence,
        "manifests": [json.loads(js_get(row, "manifest")) for row in result],
    }


def artifact_read_locator(state: State, request: Json) -> Json:
    request = validate_contract("ct_artifact_read", request)
    manifests = artifact_manifests(
        state,
        {
            "workspace_id": request["workspace_id"],
            "snapshot_sequence": request.get("snapshot_sequence"),
        },
    )["manifests"]
    referenced = any(
        (graph["facts"] if request["kind"] == "facts" else graph["summary"])["sha256"]
        == request["sha256"]
        for manifest in manifests
        for graph in manifest["graphs"]
    )
    require_that(referenced, "artifact_not_in_snapshot", 404)
    return {
        "__artifact_key": artifact_key(
            request["workspace_id"], request["kind"], request["sha256"]
        ),
        "sha256": request["sha256"],
        "kind": request["kind"],
    }


async def cleanup_artifact_objects(state: State, env: Any, workspace_id: str) -> None:
    referenced: set[str] = set()
    for row in rows(state.sql.exec("SELECT manifest FROM artifact_manifests")):
        manifest = expand_manifest(json.loads(js_get(row, "manifest")))
        for graph in manifest["graphs"]:
            referenced.add(
                artifact_key(workspace_id, "facts", graph["facts"]["sha256"])
            )
            referenced.add(
                artifact_key(workspace_id, "summary", graph["summary"]["sha256"])
            )
            for reference in graph.get("api_objects", []):
                referenced.add(artifact_key(workspace_id, "api", reference["sha256"]))
    for row in rows(state.sql.exec("SELECT hash FROM api_inventory_objects")):
        referenced.add(artifact_key(workspace_id, "api", str(js_get(row, "hash"))))
    now = int(time.time())
    state.sql.exec("DELETE FROM artifact_upload_claims WHERE expires_at<=?", now)
    for row in rows(state.sql.exec("SELECT kind,sha256 FROM artifact_upload_claims")):
        referenced.add(
            artifact_key(
                workspace_id, str(js_get(row, "kind")), str(js_get(row, "sha256"))
            )
        )
    cursor_rows = rows(
        state.sql.exec(
            "SELECT cursor FROM artifact_cleanup WHERE workspace_id=?", workspace_id
        )
    )
    cursor = js_get(cursor_rows[0], "cursor") if cursor_rows else None
    for _page in range(CLEANUP_PAGES_PER_PUBLICATION):
        options: Json = {
            "prefix": f"workspaces/{workspace_id}/artifacts/",
            "limit": 1000,
        }
        if cursor:
            options["cursor"] = cursor
        page = await env.ARTIFACTS.list(js_value(options))
        objects = rows(js_get(page, "objects", []))
        stale = [
            str(js_get(item, "key"))
            for item in objects
            if str(js_get(item, "key")) not in referenced
        ]
        if stale:
            await env.ARTIFACTS.delete(js_value(stale))
        if not js_get(page, "truncated", False) or not js_get(page, "cursor"):
            state.sql.exec(
                "DELETE FROM artifact_cleanup WHERE workspace_id=?", workspace_id
            )
            return
        cursor = js_get(page, "cursor")
    state.sql.exec(
        "INSERT INTO artifact_cleanup VALUES(?,?) ON CONFLICT(workspace_id) DO UPDATE SET cursor=excluded.cursor",
        workspace_id,
        cursor,
    )
