#!/usr/bin/env python3
"""Qualify immutable artifact publication against loopback Wrangler only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from coding_trajectory.control_plane.artifact_protocol import (
    ArtifactGraphPublication,
    ArtifactObjectReference,
    ArtifactPublicationRequest,
)
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    CollectorIdentity,
    CollectorRemoteError,
    LocalCollector,
    _prepared_graph_summary,
)
from coding_trajectory.control_plane.collector_protocol import (
    ObservationRequest,
    ProjectRegistrationRequest,
    SourceRegistrationRequest,
    SourceVectorEntry,
)
from coding_trajectory.control_plane.fact_repository import (
    ArtifactReadCache,
    CloudflareArtifactRepository,
)
from coding_trajectory.control_plane.remote import (
    CloudflareRpcClient,
    RemoteControlPlaneError,
)
from coding_trajectory.control_plane.remote_inventory import (
    CloudflareProjectInventoryRepository,
)
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.service.handlers import dispatch
from coding_trajectory.service.store import IndexCache

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fact_qualification", ROOT / "scripts/qualify-cloudflare-control-plane.py"
)
QUALIFICATION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(QUALIFICATION)

URL = os.environ.get("CT_QUALIFY_URL", "http://127.0.0.1:8794")
WORKSPACE = UUID(QUALIFICATION.WORKSPACE)
AGENT = UUID(QUALIFICATION.AGENT)
TOKEN = QUALIFICATION.TOKENS["owner"]
checks = 0


def check(value: object, label: str) -> None:
    global checks
    if not value:
        raise AssertionError(label)
    checks += 1


def qualify_reader_fail_closed() -> None:
    class Client:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error

        def call(self, method, _request):
            assert method == "ct_artifact_manifest"
            if self.error is not None:
                raise self.error
            return self.response

        def close(self):
            return None

    empty = Client(
        {"workspace_id": str(WORKSPACE), "snapshot_sequence": 7, "manifests": []}
    )
    repository = CloudflareArtifactRepository(
        client=empty,
        workspace_id=WORKSPACE,
        snapshot_sequence=7,
        cache=ArtifactReadCache(),
    )
    check(
        repository.response_for("project.sessions", {}) == {"items": []},
        "empty artifact manifest produces an empty session list",
    )
    try:
        repository.store_for("graph.stats", {"root_session_id": str(uuid4())})
    except RemoteControlPlaneError as exc:
        check("no graphs" in str(exc), "empty artifact detail has no SQL fallback")
    else:
        raise AssertionError("empty artifact detail unexpectedly fell back")

    unavailable = RemoteControlPlaneError(
        "snapshot unavailable", code="artifact_snapshot_unavailable"
    )
    repository = CloudflareArtifactRepository(
        client=Client(error=unavailable),
        workspace_id=WORKSPACE,
        snapshot_sequence=7,
        cache=ArtifactReadCache(),
    )
    try:
        repository.response_for("project.sessions", {})
    except RemoteControlPlaneError as exc:
        check(exc is unavailable, "artifact snapshot unavailability propagates")
    else:
        raise AssertionError("artifact snapshot unavailability was swallowed")

    try:
        CloudflareArtifactRepository._summary_for({}, uuid4())
    except RemoteControlPlaneError as exc:
        check("missing detail" in str(exc), "missing graph detail is explicit")
    else:
        raise AssertionError("missing graph detail was accepted")


def encode(value: object) -> bytes:
    return canonical_json(value).encode()


def prepare(facts, source_id, observed_at):
    fact_bytes = encode(facts.model_dump(mode="json", exclude_none=True))
    summary = _prepared_graph_summary(facts)
    summary_bytes = encode(summary.model_dump(mode="json", exclude_none=True))
    fact_hash = hashlib.sha256(fact_bytes).hexdigest()
    summary_hash = hashlib.sha256(summary_bytes).hexdigest()
    return (
        ArtifactGraphPublication(
            graph_id=facts.graph_id,
            graph_input_sha256=facts.fact_set_digest,
            fact_set_digest=facts.fact_set_digest,
            fact_count=len(facts.rows),
            source_ids=[source_id],
            vendors=["amp"],
            observed_at=observed_at,
            facts=ArtifactObjectReference(
                kind="facts", sha256=fact_hash, bytes=len(fact_bytes)
            ),
            summary=ArtifactObjectReference(
                kind="summary", sha256=summary_hash, bytes=len(summary_bytes)
            ),
        ),
        {("facts", fact_hash): fact_bytes, ("summary", summary_hash): summary_bytes},
        summary,
    )


def checkpoint(remote, project_id, native_id, digest, sequence, observed_at):
    source = remote.register_source(
        SourceRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            project_id=project_id,
            vendor="amp",
            native_session_id=native_id,
        ),
        idempotency_key="artifact-source:" + native_id,
    )
    payload = {
        "kind": "ct.source_checkpoint.v1",
        "source_checkpoint": {"segments": [sequence + 1]},
        "session_digest": digest,
    }
    content = hashlib.sha256(encode(payload)).hexdigest()
    remote.publish_observation(
        ObservationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            source_id=source.source_id,
            source_epoch=source.source_epoch,
            source_sequence=sequence,
            event_id="checkpoint:" + content,
            parser_version="artifact-qualification.v1",
            content_sha256=content,
            observed_at=observed_at,
            payload=payload,
        ),
        idempotency_key="artifact-checkpoint:" + content,
    )
    return source, SourceVectorEntry(
        source_id=source.source_id,
        source_epoch=source.source_epoch,
        source_sequence=sequence,
        content_sha256=content,
    )


def qualify_collector(remote: CloudflareCollectorRemote, tag: str) -> None:
    """Exercise production per-graph reuse and complete-inventory semantics."""

    class CountingRemote:
        def __init__(self, authority: CloudflareCollectorRemote) -> None:
            self.authority = authority
            self.uploads: list[str] = []
            self.lose_next_upload_response = False
            self.lose_next_publication_response = False

        def __getattr__(self, name: str):
            return getattr(self.authority, name)

        def upload_artifact(self, *, kind: str, sha256: str, body: bytes) -> None:
            self.uploads.append(sha256)
            self.authority.upload_artifact(kind=kind, sha256=sha256, body=body)
            if self.lose_next_upload_response:
                self.lose_next_upload_response = False
                raise CollectorRemoteError("synthetic lost upload response")

        def publish_artifacts(self, request, *, idempotency_key: str):
            receipt = self.authority.publish_artifacts(
                request, idempotency_key=idempotency_key
            )
            if self.lose_next_publication_response:
                self.lose_next_publication_response = False
                raise CollectorRemoteError("synthetic lost publication response")
            return receipt

    counted = CountingRemote(remote)
    with tempfile.TemporaryDirectory(prefix="ct-artifact-collector-") as directory:
        root = Path(directory)
        journals = root / "journals"
        journals.mkdir()
        os.environ["CT_AMP_LOG_DIR"] = str(journals)
        stamp = "2026-09-17T00:00:00Z"
        sessions: dict[int, str] = {}

        def write(number: int, text: str) -> Path:
            session = str(
                UUID(hex=hashlib.sha256(f"{tag}:{number}".encode()).hexdigest()[:32])
            )
            sessions[number] = session
            rows = [
                {
                    "schema_version": 1,
                    "type": "thread",
                    "captured_at": stamp,
                    "payload": {
                        "id": "T-" + session,
                        "workspace_root": root.as_uri(),
                        "title": "Artifact collector",
                    },
                },
                {
                    "schema_version": 1,
                    "type": "message",
                    "captured_at": stamp,
                    "thread_id": "T-" + session,
                    "message": {
                        "id": "user-0",
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    },
                },
                {
                    "schema_version": 1,
                    "type": "observation",
                    "captured_at": stamp,
                    "thread_id": "T-" + session,
                    "event": "tool.call",
                    "tool_use_id": "tool-0",
                    "tool_name": "shell_command",
                    "input": {"command": "true"},
                },
                {
                    "schema_version": 1,
                    "type": "observation",
                    "captured_at": stamp,
                    "thread_id": "T-" + session,
                    "event": "tool.result",
                    "tool_use_id": "tool-0",
                    "tool_name": "shell_command",
                    "status": "done",
                    "output": json.dumps({"exitCode": 0, "output": "ok"}),
                },
            ]
            path = journals / f"{number}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return path

        first_path = write(1, "first")
        write(2, "second")
        project = counted.register_project(
            ProjectRegistrationRequest(
                workspace_id=WORKSPACE,
                agent_id=AGENT,
                display_name="Collector-" + tag,
            )
        )
        identity = CollectorIdentity(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            agent_instance_id=uuid4(),
            project_id=project.project_id,
            project_name="Collector-" + tag,
        )
        database = root / "collector.sqlite3"
        with LocalCollector(database_path=database, identity=identity) as collector:
            initial = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(
                initial.facts_accepted == 1,
                f"collector publishes initial artifacts: {initial!r}",
            )
            initial_prepared = {
                row["facts_sha256"]
                for row in collector._connection.execute(
                    "select facts_sha256 from prepared_graphs"
                )
            }
            check(
                len(initial_prepared) == 2,
                "collector prepares each graph independently",
            )
            check(len(counted.uploads) == 4, "initial collector uploads all objects")
            unchanged = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            unchanged_prepared = {
                row["facts_sha256"]
                for row in collector._connection.execute(
                    "select facts_sha256 from prepared_graphs"
                )
            }
            check(
                unchanged.facts_queued == 0
                and unchanged.facts_accepted == 0
                and unchanged_prepared == initial_prepared,
                "unchanged inventory reuses preparations without republishing",
            )
            check(len(counted.uploads) == 4, "unchanged collector uploads nothing")
            first_path.write_text(
                first_path.read_text().replace("first", "upload-lost")
            )
            counted.lose_next_upload_response = True
            upload_lost = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(
                upload_lost.facts_accepted == 0 and len(counted.uploads) == 5,
                "lost upload response leaves an uncommitted publication pending",
            )
            upload_retried = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(
                upload_retried.facts_accepted == 1 and len(counted.uploads) == 9,
                "absent receipt refreshes claims and uploads the complete manifest",
            )
            first_path.write_text(
                first_path.read_text().replace("upload-lost", "changed")
            )
            counted.lose_next_publication_response = True
            changed = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(
                changed.facts_accepted == 0
                and collector._connection.execute(
                    "select state from publication_outbox order by publication_sequence desc limit 1"
                ).fetchone()["state"]
                == "pending",
                "lost changed-publication response remains pending",
            )
            check(
                len(counted.uploads) == 11,
                "one changed graph normally uploads only its two new objects",
            )
            retried = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(
                retried.facts_accepted == 1
                and collector._connection.execute(
                    "select state from publication_outbox order by publication_sequence desc limit 1"
                ).fetchone()["state"]
                == "accepted",
                "lost response retry recovers the committed receipt",
            )
            check(
                len(counted.uploads) == 11,
                "committed lost response recovers without reuploading",
            )
            changed_prepared = {
                row["facts_sha256"]
                for row in collector._connection.execute(
                    "select facts_sha256 from prepared_graphs"
                )
            }
            check(
                len(initial_prepared & changed_prepared) == 1,
                "one changed graph reuses the other immutable preparation",
            )
            parent_rows = [
                json.loads(line) for line in first_path.read_text().splitlines()
            ]
            parent_rows[2]["tool_name"] = "create_thread"
            parent_rows[2]["input"] = {}
            parent_rows[3]["tool_name"] = "create_thread"
            parent_rows[3]["output"] = json.dumps(
                {"threadID": "T-" + sessions[2], "executor": "orb"}
            )
            first_path.write_text(
                "".join(json.dumps(row) + "\n" for row in parent_rows)
            )
            merged = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(merged.facts_accepted == 1, "topology merge publishes")
            check(
                collector._connection.execute(
                    "select count(*) from prepared_graphs"
                ).fetchone()[0]
                == 1,
                "topology merge prepares one canonical graph",
            )
            check(
                len(counted.uploads) == 13,
                "topology merge uploads only the new combined graph",
            )
            parent_rows[2]["tool_name"] = "shell_command"
            parent_rows[2]["input"] = {"command": "true"}
            parent_rows[3]["tool_name"] = "shell_command"
            parent_rows[3]["output"] = json.dumps({"exitCode": 0, "output": "ok"})
            first_path.write_text(
                "".join(json.dumps(row) + "\n" for row in parent_rows)
            )
            split = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(split.facts_accepted == 1, "topology split publishes")
            check(
                collector._connection.execute(
                    "select count(*) from prepared_graphs"
                ).fetchone()[0]
                == 2,
                "topology split restores two canonical graphs",
            )
            check(
                len(counted.uploads) == 17,
                "topology split restores both graph artifacts",
            )
            (journals / "2.jsonl").unlink()
            deleted = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            check(
                deleted.facts_accepted == 1, "explicit file deletion updates inventory"
            )
            before_unavailable = counted.recover(
                QUALIFICATION.CollectorRecoveryRequest(
                    workspace_id=WORKSPACE,
                    agent_id=AGENT,
                    project_id=project.project_id,
                )
            ).authority_sequence
            journals.rename(root / "unavailable-journals")
            unavailable = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=counted,
                heartbeat=False,
            )
            after_unavailable = counted.recover(
                QUALIFICATION.CollectorRecoveryRequest(
                    workspace_id=WORKSPACE,
                    agent_id=AGENT,
                    project_id=project.project_id,
                )
            ).authority_sequence
            check(
                unavailable.failed > 0
                and unavailable.facts_accepted == 0
                and before_unavailable == after_unavailable,
                "unavailable source root preserves the last completed snapshot",
            )


def main() -> None:
    parsed = urlparse(URL)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("artifact qualification refuses a non-loopback target")
    qualify_reader_fail_closed()
    observed = datetime.now(UTC).replace(microsecond=0)
    tag = uuid4().hex
    project_name = "ArtifactQualification-" + tag
    remote = CloudflareCollectorRemote(url=URL, access_token=TOKEN)
    project = remote.register_project(
        ProjectRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            display_name=project_name,
        )
    )
    first = QUALIFICATION.synthetic_fact_set(seed=tag + ":a", project=project_name)
    second = QUALIFICATION.synthetic_fact_set(seed=tag + ":b", project=project_name)
    first_source, first_vector = checkpoint(
        remote, project.project_id, tag + ":a", first.fact_set_digest, 0, observed
    )
    second_source, second_vector = checkpoint(
        remote, project.project_id, tag + ":b", second.fact_set_digest, 0, observed
    )
    first_graph, first_objects, first_summary = prepare(
        first, first_source.source_id, observed
    )
    second_graph, second_objects, _ = prepare(second, second_source.source_id, observed)

    unauthenticated = httpx.put(
        f"{URL}/v1/artifacts/facts/{first_graph.facts.sha256}",
        content=first_objects[("facts", first_graph.facts.sha256)],
    )
    check(unauthenticated.status_code == 401, "unauthorized artifact upload denied")
    malformed = httpx.put(
        f"{URL}/v1/artifacts/facts/{'0' * 64}",
        content=b"{}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    check(malformed.status_code == 400, "malformed artifact digest denied")

    for (kind, sha256), body in {**first_objects, **second_objects}.items():
        if sha256 != second_graph.summary.sha256:
            remote.upload_artifact(kind=kind, sha256=sha256, body=body)
    request = ArtifactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=project.project_id,
        publication_sequence=0,
        source_vector=[first_vector, second_vector],
        graphs=[first_graph, second_graph],
    )
    try:
        remote.publish_artifacts(request, idempotency_key="artifact:" + tag + ":0")
    except CollectorRemoteError as exc:
        check(
            exc.code == "artifact_upload_incomplete", "interrupted upload is invisible"
        )
    else:
        raise AssertionError("incomplete artifact publication was accepted")
    remote.upload_artifact(
        kind="summary",
        sha256=second_graph.summary.sha256,
        body=second_objects[("summary", second_graph.summary.sha256)],
    )
    receipt = remote.publish_artifacts(
        request, idempotency_key="artifact:" + tag + ":0"
    )
    replay = remote.publish_artifacts(request, idempotency_key="artifact:" + tag + ":0")
    check(receipt == replay, "lost publication response replays its receipt")
    initial_snapshot = receipt.committed_sequence
    assert initial_snapshot is not None

    client = CloudflareRpcClient(url=URL, access_token=QUALIFICATION.TOKENS["reader"])
    cache = ArtifactReadCache(max_bytes=2 * 1024 * 1024, max_entries=2)
    repository = CloudflareArtifactRepository(
        client=client,
        workspace_id=WORKSPACE,
        snapshot_sequence=initial_snapshot,
        cache=cache,
    )
    listed = repository.response_for("project.sessions", {"project_name": project_name})
    check(
        listed is not None and len(listed["items"]) == 2,
        "prepared list summaries route",
    )
    by_id = repository.response_for(
        "project.sessions", {"project_id": str(project.project_id)}
    )
    check(by_id == listed, "project ID and display-name lookup agree")
    check(
        repository.response_for(
            "project.sessions", {"project_name": "artifact-qualification-" + tag}
        )
        == listed,
        "normalized display-name lookup agrees",
    )
    check(
        {item["project_id"] for item in by_id["items"]} == {str(project.project_id)},
        "session cards carry authoritative manifest project ID",
    )
    check(
        repository.response_for("project.sessions", {"project_id": str(uuid4())})
        == {"items": []},
        "unknown project ID never falls back to a name",
    )
    check(
        repository.response_for(
            "project.sessions",
            {"project_id": str(project.project_id), "agent_vendor": "amp"},
        )
        == by_id,
        "prepared vendor filter uses vendors array",
    )
    check(
        repository.response_for(
            "project.sessions",
            {"project_id": str(project.project_id), "agent_vendor": "codex_cli"},
        )
        == {"items": []},
        "prepared vendor filter excludes other vendors",
    )
    inventory = CloudflareProjectInventoryRepository(
        client=client, workspace_id=WORKSPACE, snapshot_sequence=initial_snapshot
    )
    project_list = inventory.project_list({"project_id": str(project.project_id)})[
        "items"
    ]
    check(
        set(project_list) == {str(project.project_id)}
        and project_list[str(project.project_id)]["display_name"] == project_name,
        "project inventory is ID-keyed with a separate display name",
    )
    index, _ = repository.store_for(
        "session.items", {"item_ids": [str(first_summary.aliases[-1])]}
    )
    root = str(first.graph_id)
    detail = dispatch(
        "graph.stats",
        {"root_session_id": root},
        store=index,
        global_scope=True,
        current_dir=ROOT,
        discovery_note="artifact qualification",
        cache=IndexCache(),
    )
    check(detail.get("root_session_id") == root, "root and item aliases select detail")

    revised = QUALIFICATION.synthetic_fact_set(
        seed=tag + ":a", project=project_name, revised=True
    )
    _, revised_vector = checkpoint(
        remote, project.project_id, tag + ":a", revised.fact_set_digest, 1, observed
    )
    revised_graph, revised_objects, _ = prepare(
        revised, first_source.source_id, observed
    )
    for (kind, sha256), body in revised_objects.items():
        remote.upload_artifact(kind=kind, sha256=sha256, body=body)
    changed = ArtifactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=project.project_id,
        publication_sequence=1,
        source_vector=[revised_vector, second_vector],
        graphs=[revised_graph, second_graph],
    )
    changed_receipt = remote.publish_artifacts(
        changed, idempotency_key="artifact:" + tag + ":1"
    )
    check(
        changed_receipt.committed_sequence != initial_snapshot,
        "changed graph publishes a new immutable snapshot",
    )

    deletion = ArtifactPublicationRequest(
        workspace_id=WORKSPACE,
        agent_id=AGENT,
        project_id=project.project_id,
        publication_sequence=2,
        source_vector=[revised_vector],
        graphs=[revised_graph],
    )
    deletion_receipt = remote.publish_artifacts(
        deletion, idempotency_key="artifact:" + tag + ":2"
    )
    check(
        deletion_receipt.outcome == "accepted", "complete inventory deletion publishes"
    )

    # A fourth complete snapshot prunes snapshot 0 while retaining snapshots 1-3.
    final = deletion.model_copy(update={"publication_sequence": 3})
    final_receipt = remote.publish_artifacts(
        final, idempotency_key="artifact:" + tag + ":3"
    )
    check(final_receipt.outcome == "accepted", "bounded rollback publication succeeds")
    try:
        client.call(
            "ct_artifact_manifest",
            {
                "workspace_id": str(WORKSPACE),
                "snapshot_sequence": initial_snapshot,
                "project_id": str(project.project_id),
            },
        )
    except RemoteControlPlaneError as exc:
        check(
            exc.code == "artifact_snapshot_expired", "old pruned snapshot is explicit"
        )
    else:
        raise AssertionError("pruned artifact snapshot remained visible")
    try:
        client.call(
            "ct_project_inventory_snapshot",
            {
                "workspace_id": str(WORKSPACE),
                "snapshot_sequence": initial_snapshot,
            },
        )
    except RemoteControlPlaneError as exc:
        check(
            exc.code == "artifact_snapshot_expired",
            "project inventory never falls back across an expired artifact snapshot",
        )
    else:
        raise AssertionError("project inventory exposed an expired artifact snapshot")
    retained = client.call(
        "ct_artifact_manifest",
        {
            "workspace_id": str(WORKSPACE),
            "snapshot_sequence": changed_receipt.committed_sequence,
            "project_id": str(project.project_id),
        },
    )
    check(
        len(retained["manifests"]) == 1, "retained rollback snapshot remains readable"
    )

    collision = remote.register_project(
        ProjectRegistrationRequest(
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            display_name="artifact-qualification-" + tag,
        )
    )
    collision_inventory = CloudflareProjectInventoryRepository(
        client=client,
        workspace_id=WORKSPACE,
        snapshot_sequence=collision.committed_sequence,
    )
    check(
        project.project_id != collision.project_id,
        "distinct registered IDs survive normalized-name collision",
    )
    try:
        collision_inventory.resolve_name(project_name)
    except ValueError as error:
        check("ambiguous" in str(error), "ambiguous remote name requires project ID")
    else:
        raise AssertionError("ambiguous remote project name selected an arbitrary ID")
    check(
        set(
            collision_inventory.project_list({"project_id": str(project.project_id)})[
                "items"
            ]
        )
        == {str(project.project_id)},
        "ID lookup remains exact despite normalized-name collision",
    )

    tiny = ArtifactReadCache(max_bytes=8, max_entries=1)
    tiny.put(("w", "summary", "a"), "a", 4)
    tiny.put(("w", "summary", "b"), "b", 4)
    check(tiny.get(("w", "summary", "a")) is None, "entry eviction is bounded")
    tiny.put(("w", "facts", "large"), "large", 9)
    check(tiny.get(("w", "facts", "large")) is None, "byte eviction is bounded")
    qualify_collector(remote, tag)
    repository.close()
    remote.close()
    print(json.dumps({"status": "ok", "checks": checks, "snapshots": 4}))


if __name__ == "__main__":
    main()
