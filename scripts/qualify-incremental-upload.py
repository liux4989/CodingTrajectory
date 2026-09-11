#!/usr/bin/env python3
"""Real workerd + SQLite + hard-exit qualification, using synthetic Amp journals.

Start the same loopback authority/principals as qualify-cloudflare-control-plane.py.
No provider accounts, production databases, user logs, or unit-test mocks are used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from coding_trajectory.control_plane.catalog_protocol import CatalogReadResponse
from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    CollectorIdentity,
    CollectorRemoteError,
    _project_session_list_variants,
)
from coding_trajectory.control_plane.upload_chunks import build_chunks, chunk_batches
from coding_trajectory.control_plane.upload_service import UploadService
from coding_trajectory.control_plane.upload_state import UploadStateError
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch

URL = "http://127.0.0.1:8794"
TOKEN = "local-qualification-owner-token-0000000001"
WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")
AGENT = UUID("00000000-0000-0000-0000-000000000003")
checks = 0


def check(condition, label):
    global checks
    if not condition:
        raise AssertionError(label)
    checks += 1


def rejected(remote, method, request, label):
    try:
        remote._rpc(method, request)
    except CollectorRemoteError as error:
        check(error.status_code in {400, 403, 413}, label)
    else:
        raise AssertionError(label)


def identity(root):
    return CollectorIdentity(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        agent_instance_id=AGENT,
        project_name="Upload-" + root.name,
    )


def child(root, action, boundary):
    os.environ["CT_AMP_LOG_DIR"] = str(root / "journals")

    def crash(point):
        if boundary == point:
            os._exit(91)

    class Remote(CloudflareCollectorRemote):
        def _rpc(self, name, request, **kwargs):
            result = super()._rpc(name, request, **kwargs)
            with (root / "transport.jsonl").open("a") as output:
                output.write(
                    json.dumps(
                        {"method": name, "bytes": len(canonical_json(request).encode())}
                    )
                    + "\n"
                )
            if name == "ct_collector_upload_chunks":
                crash("after_chunk_send")
            return result

    service = UploadService(root / "sync.sqlite3", identity(root), crash_hook=crash)
    if action == "prepare":
        service.prepare(current_dir=root, agent_vendor="amp")
    else:
        remote = Remote(url=URL, access_token=TOKEN, chunked=True)
        service.publish(remote)
        remote.close()
    service.close()


def invoke(root, action, boundary="", expected=0):
    result = subprocess.run(
        [sys.executable, __file__, "--child", str(root), action, boundary],
        capture_output=True,
        text=True,
        check=False,
    )
    check(
        result.returncode == expected, f"{action}/{boundary}: {result.stderr[-1200:]}"
    )


def append(root, session, start, count):
    with (root / "journals/session.jsonl").open("a") as output:
        for n in range(start, start + count):
            for role in ("user", "assistant"):
                output.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "type": "message",
                            "captured_at": datetime.now(UTC).isoformat(),
                            "thread_id": "T-" + session,
                            "message": {
                                "id": f"{role}-{n}",
                                "role": role,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"Synthetic {role} observation {n}",
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )


def main():
    with tempfile.TemporaryDirectory(prefix="ct-upload-") as directory:
        root = Path(directory)
        (root / "journals").mkdir()
        session = str(uuid4())
        (root / "journals/session.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "thread",
                    "captured_at": datetime.now(UTC).isoformat(),
                    "payload": {
                        "id": "T-" + session,
                        "workspace_root": root.as_uri(),
                        "title": "Synthetic upload qualification",
                    },
                }
            )
            + "\n"
        )
        append(root, session, 0, 40)
        invoke(root, "prepare", "before_prepare_commit", 91)
        service = UploadService(root / "sync.sqlite3", identity(root))
        check(
            service.status()["consumed_cursor"] is None,
            "precommit crash did not consume source",
        )
        service.close()
        invoke(root, "prepare", "after_prepare_commit", 91)
        service = UploadService(root / "sync.sqlite3", identity(root))
        check(
            service.status()["batches"].get("prepared") == 1,
            "postcommit crash retained batch",
        )
        check(
            service.status()["acknowledged_cursor"] is None,
            "offline prepare has no remote acknowledgement",
        )
        body = service._batch_body(
            service.db.execute("SELECT * FROM sync_batches").fetchone()
        )
        service.close()
        invoke(root, "prepare")
        remote = CloudflareCollectorRemote(url=URL, access_token=TOKEN, chunked=True)
        base = {"workspace_id": str(WORKSPACE)}
        before = remote._rpc("ct_publication_watermark", base)["published_sequence"]
        invoke(root, "publish", "after_chunk_send", 91)
        check(
            remote._rpc("ct_publication_watermark", base)["published_sequence"]
            == before,
            "chunk upload leaves publication watermark unchanged",
        )
        invoke(root, "publish", "after_publication_send", 91)
        service = UploadService(root / "sync.sqlite3", identity(root))
        check(
            service.status()["batches"].get("awaiting_commit") == 1,
            "lost publication acknowledgement retained frozen batch",
        )
        frozen = service.db.execute(
            "SELECT id,plan FROM sync_batches WHERE state='awaiting_commit'"
        ).fetchone()
        service.close()
        manifest = remote._rpc(
            "ct_artifact_chunk_manifest", {**base, "artifact_id": session}
        )
        check(
            manifest["transport"] == "ct.canonical_chunks.v1",
            "committed chunk manifest visible",
        )
        check(
            manifest["published_sequence"] > before,
            "only committed publication advanced watermark",
        )
        first_revision = manifest["revision"]
        first_root, first_chunks = build_chunks(body["artifact"])
        check(
            first_root == manifest["root_sha256"],
            "manifest root matches durable canonical capture",
        )
        root_node = remote._rpc(
            "ct_artifact_chunks",
            {
                **base,
                "artifact_id": session,
                "snapshot_sequence": manifest["snapshot_sequence"],
                "digests": [first_root],
            },
        )
        check(
            root_node["chunks"][0]["node"] == first_chunks[first_root],
            "lazy committed root read",
        )
        rejected(
            remote,
            "ct_artifact_chunks",
            {**base, "artifact_id": session, "digests": ["0" * 64]},
            "nonmember chunk cannot be read through published artifact",
        )
        upload_identity = {**base, "agent_id": str(AGENT)}
        rejected(
            remote,
            "ct_collector_upload_chunks",
            {
                **upload_identity,
                "chunks": [
                    {
                        "content_sha256": "0" * 64,
                        "node": {"kind": "json", "fragment": "null"},
                    }
                ],
            },
            "incorrect chunk digest is rejected",
        )
        rejected(
            remote,
            "ct_collector_upload_chunks",
            {
                **upload_identity,
                "chunks": [
                    {
                        "content_sha256": "0" * 64,
                        "node": {"kind": "json", "fragment": "x" * (256 * 1024)},
                    }
                ],
            },
            "oversized chunk request is rejected",
        )
        reader = CloudflareCollectorRemote(
            url=URL,
            access_token="local-qualification-reader-token-000000001",
            chunked=True,
        )
        rejected(
            reader,
            "ct_collector_upload_chunks",
            {
                **upload_identity,
                "chunks": [
                    {"content_sha256": first_root, "node": first_chunks[first_root]}
                ],
            },
            "reader principal cannot upload chunks",
        )
        reader.close()
        # Existing readers still replay the exact artifact; directly inspect the
        # transport bytes to avoid depending on current repository constructor API.
        import base64
        import gzip

        historical = remote._rpc(
            "ct_historical_artifacts", {**base, "resource_ids": [session]}
        )
        decoded = gzip.decompress(
            base64.b64decode(historical["artifacts"][0]["payload_base64"])
        )
        check(
            decoded == canonical_json(body["artifact"]).encode(),
            "legacy gzip is byte-exact canonical replay",
        )
        store = DocumentStore.from_session_graphs(
            [ChronicleGraphArtifact.model_validate(body["artifact"]).to_session_graph()]
        )

        def canonical(method, params):
            return dispatch(
                method,
                params,
                store=store,
                global_scope=True,
                current_dir=Path("/"),
                discovery_note="qualification",
                cache=IndexCache(),
            )

        def resource(kind, ids, **options):
            response = remote._rpc(
                "ct_catalog_read_v2",
                {
                    **base,
                    "kind": "resources",
                    "resource_kind": kind,
                    "resource_ids": ids,
                    **options,
                },
            )
            CatalogReadResponse.model_validate(response)
            return response

        tree = resource("tree", [session])
        selection = tree["selection"]["token"]
        check(
            tree["items"][0]["payload"]
            == canonical("session.tree", {"session_id": session}),
            "independent tree canonical parity",
        )
        graph = resource("graph", [session], selection=selection)["items"][0]["payload"]
        for part, include in (
            ("overview", []),
            ("stats", ["session_composition"]),
            ("usage", []),
        ):
            params = {"root_session_id": session}
            if include:
                params["include"] = include
            check(
                graph[part] == canonical("graph." + part, params),
                "independent graph " + part + " parity",
            )
        ids = [str(item) for item in store.items]
        selected_ids = [ids[-1], ids[0]]
        items = resource("item", selected_ids, selection=selection, limit=1)
        expected_items = canonical(
            "session.items", {"item_ids": selected_ids, "include_content": False}
        )
        expected_by_id = {item["item_id"]: item for item in expected_items}
        check(
            items["items"][0]["payload"] == expected_by_id[selected_ids[0]],
            "last metadata item directly readable",
        )
        check(
            resource("item", [str(uuid4())], selection=selection)["items"][0][
                "coverage"
            ]
            == "not_found",
            "unknown resource distinguished from unavailable",
        )
        for invalid_field, invalid_value in (
            ("turn_id", str(uuid4())),
            ("content", "synthetic forbidden body"),
        ):
            projections = _project_session_list_variants(
                ChronicleGraphArtifact.model_validate(body["artifact"])
            )
            bad_item = next(
                row
                for row in projections["resources"]["rows"]
                if row["resource_kind"] == "item"
            )
            bad_item["payload"][invalid_field] = invalid_value
            rejected(
                remote,
                "ct_collector_stage_artifact_payload",
                {
                    **base,
                    "agent_id": str(AGENT),
                    "schema_version": body["artifact"]["schema_version"],
                    "content_sha256": historical["artifacts"][0]["content_sha256"],
                    "encoding": "gzip",
                    "uncompressed_bytes": len(decoded),
                    "compressed_bytes": len(
                        base64.b64decode(historical["artifacts"][0]["payload_base64"])
                    ),
                    "payload_base64": historical["artifacts"][0]["payload_base64"],
                    "projections": projections,
                },
                "invalid resource " + invalid_field + " rejected before staging",
            )
        check(
            resource("item", selected_ids[:1], selection=selection)["items"][0][
                "payload"
            ]
            == expected_by_id[selected_ids[0]],
            "invalid stage cannot replace committed projection",
        )
        append(root, session, 40, 1)
        invoke(root, "prepare")
        service = UploadService(root / "sync.sqlite3", identity(root))
        check(
            service.status()["batches"].get("prepared") == 1
            and service.status()["batches"].get("awaiting_commit") == 1,
            "new observations prepare beside frozen upload",
        )
        check(
            tuple(
                service.db.execute(
                    "SELECT id,plan FROM sync_batches WHERE state='awaiting_commit'"
                ).fetchone()
            )
            == tuple(frozen),
            "in-flight identity and payload immutable",
        )
        next_body = service._batch_body(
            service.db.execute(
                "SELECT * FROM sync_batches WHERE state='prepared'"
            ).fetchone()
        )
        _, next_chunks = build_chunks(next_body["artifact"])
        reused = set(first_chunks) & set(next_chunks)
        check(
            len(reused) > len(first_chunks) * 0.8,
            "append reuses unchanged canonical resources",
        )
        check(
            all(
                len(canonical_json(batch).encode()) < 256 * 1024
                for batch in chunk_batches(next_chunks)
            ),
            "bounded initial and incremental batches",
        )
        service.close()
        invoke(root, "publish")
        final = remote._rpc(
            "ct_artifact_chunk_manifest", {**base, "artifact_id": session}
        )
        check(
            final["revision"] == first_revision + 1,
            "lost acknowledgement retry did not duplicate revision",
        )
        continued = resource("item", selected_ids, cursor=items["next_cursor"], limit=1)
        check(
            continued["selection"]["token"] == selection
            and continued["items"][0]["payload"] == expected_by_id[selected_ids[1]],
            "item continuation preserves selection across publication",
        )
        check(
            resource("tree", [session], selection=selection)["items"] == tree["items"],
            "parent tree remains frozen after publication",
        )
        old = remote._rpc(
            "ct_artifact_chunk_manifest",
            {
                **base,
                "artifact_id": session,
                "snapshot_sequence": manifest["snapshot_sequence"],
            },
        )
        check(
            old["root_sha256"] == first_root, "pinned earlier revision remains readable"
        )
        service = UploadService(root / "sync.sqlite3", identity(root))
        check(
            service.status()["consumed_cursor"]
            == service.status()["acknowledged_cursor"],
            "ack cursor catches up only after all prepared revisions",
        )
        check(service.status()["pending_bytes"] == 0, "restart drained pending outbox")
        check(
            not service.should_flush(), "manual mode never schedules automatic delivery"
        )
        service.configure(mode="automatic", paused=True)
        service.close()
        service = UploadService(root / "sync.sqlite3", identity(root))
        check(
            service.policy.mode == "automatic" and service.policy.paused,
            "policy survives restart",
        )
        check(not service.should_flush(), "paused automatic policy suppresses delivery")
        other = UploadService(root / "sync.sqlite3", identity(root))
        with service.owner_lock("delivery"):
            try:
                with other.owner_lock("delivery"):
                    raise AssertionError("concurrent delivery lock was accepted")
            except UploadStateError as error:
                check(
                    error.code == "delivery_owner_busy",
                    "single delivery owner across connections",
                )
        other.close()
        service.configure(mode="manual", paused=False)
        service.close()
        append(root, session, 41, 1)
        invoke(root, "prepare")
        service = UploadService(root / "sync.sqlite3", identity(root))
        pending = tuple(
            service.db.execute(
                "SELECT id,body FROM sync_batches WHERE state='prepared'"
            ).fetchone()
        )
        invalid = CloudflareCollectorRemote(
            url=URL, access_token="invalid-qualification-token", chunked=True
        )
        denied = service.publish(invalid)
        invalid.close()
        check(
            denied["published"] == 0 and denied["authority_error"],
            "invalid credentials produce bounded durable authority error",
        )
        check(
            tuple(
                service.db.execute(
                    "SELECT id,body FROM sync_batches WHERE state='prepared'"
                ).fetchone()
            )
            == pending,
            "credential failure preserves prepared identity and bytes",
        )
        service.close()
        invoke(root, "publish", "after_publication_send", 91)
        service = UploadService(root / "sync.sqlite3", identity(root))
        watermark = remote._rpc("ct_publication_watermark", base)["published_sequence"]
        reconciled = service.reconcile_remote(remote)
        check(
            reconciled["pending_bytes"] == 0,
            "explicit recovery resolves exact committed receipt after lost ACK",
        )
        check(
            remote._rpc("ct_publication_watermark", base)["published_sequence"]
            == watermark,
            "reconciliation does not create a new publication",
        )
        check(
            reconciled["consumed_cursor"] == reconciled["acknowledged_cursor"],
            "recovery advances contiguous acknowledgement cursor",
        )
        service.close()
        calls = [
            json.loads(line)
            for line in (root / "transport.jsonl").read_text().splitlines()
        ]
        uploads = [
            row["bytes"]
            for row in calls
            if row["method"] == "ct_collector_upload_chunks"
        ]
        check(uploads and max(uploads) <= 256 * 1024, "real network requests bounded")
        remote.close()
        print(
            json.dumps(
                {
                    "status": "passed",
                    "checks": checks,
                    "reused_chunks": len(reused),
                    "initial_chunks": len(first_chunks),
                    "upload_requests": len(uploads),
                    "max_upload_bytes": max(uploads),
                }
            )
        )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        main()
