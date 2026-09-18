"""Qualify deterministic collector preparation through real journals and SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane import graph_preparation
from coding_trajectory.control_plane.collector import (
    CollectorIdentity,
    CollectorRemoteError,
    LocalCollector,
)
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryResponse,
    ObservationReceipt,
    ObservationRequest,
    SourceRegistrationRequest,
    SourceRegistrationResponse,
)
from coding_trajectory.control_plane.fact_projection import build_published_fact_set
from coding_trajectory.control_plane.fact_repository import LocalPublishedFactRepository
from coding_trajectory.control_plane.graph_preparation import (
    graph_input_digest,
    prepare_graph,
)
from coding_trajectory.discovery import discover_store_from_files
from coding_trajectory.ingestion.common import canonical_json
from coding_trajectory.project_identity import local_project_id
from coding_trajectory.runtime import ServiceRuntime
from coding_trajectory.service.store import IndexCache


class CheckpointRemote:
    def __init__(self) -> None:
        self.registrations: list[SourceRegistrationRequest] = []
        self.requests: list[ObservationRequest] = []
        self.recoveries = 0
        self.uploads = 0
        self.publications = 0

    def recover(self, _request) -> CollectorRecoveryResponse:
        self.recoveries += 1
        return CollectorRecoveryResponse(next_publication_sequence=0)

    def register_source(
        self, request: SourceRegistrationRequest, *, idempotency_key: str
    ) -> SourceRegistrationResponse:
        assert idempotency_key
        self.registrations.append(request)
        return SourceRegistrationResponse(
            source_id=UUID(int=100 + len(self.registrations)),
            source_epoch=request.source_epoch,
        )

    def publish_observation(
        self, request: ObservationRequest, *, idempotency_key: str
    ) -> ObservationReceipt:
        assert idempotency_key
        self.requests.append(ObservationRequest.model_validate(request.model_dump()))
        return ObservationReceipt(
            receipt_id=UUID(int=200 + len(self.requests)),
            outcome="accepted",
        )

    def upload_artifact(self, *, kind: str, sha256: str, body: bytes) -> None:
        assert kind in {"facts", "summary"} and len(sha256) == 64 and body
        self.uploads += 1

    def publish_artifacts(self, request, *, idempotency_key: str) -> ObservationReceipt:
        assert request.graphs and idempotency_key
        self.publications += 1
        if self.publications == 1:
            raise CollectorRemoteError("synthetic uncertain artifact response")
        return ObservationReceipt(
            receipt_id=UUID(int=300 + self.publications), outcome="accepted"
        )


def main():
    with tempfile.TemporaryDirectory(prefix="ct-preparation-reuse-") as directory:
        root = Path(directory)
        os.environ["HOME"] = str(root / "home")
        journals = root / "journals"
        journals.mkdir()
        os.environ["CT_AMP_LOG_DIR"] = str(journals)
        stamp = "2026-09-10T00:00:00Z"
        for number in (1, 2):
            session = str(UUID(int=number))
            events = [
                {
                    "schema_version": 1,
                    "type": "thread",
                    "captured_at": stamp,
                    "payload": {
                        "id": "T-" + session,
                        "workspace_root": root.as_uri(),
                        "title": "Synthetic reuse",
                    },
                }
            ]
            for role in ("user", "assistant"):
                events.append(
                    {
                        "schema_version": 1,
                        "type": "message",
                        "captured_at": stamp,
                        "thread_id": "T-" + session,
                        "message": {
                            "id": role + "-0",
                            "role": role,
                            "content": [
                                {"type": "text", "text": "Synthetic reuse evidence"}
                            ],
                        },
                    }
                )
            (journals / f"{number}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in events)
            )
        identity = CollectorIdentity(
            workspace_id=UUID(int=1),
            agent_id=UUID(int=2),
            agent_instance_id=UUID(int=3),
            project_name="Reuse",
        )

        def collect():
            with LocalCollector(
                database_path=root / "capture.sqlite3", identity=identity
            ) as collector:
                result = collector.collect(
                    current_dir=root, agent_vendor="amp", heartbeat=False
                )
                assert result.failed == 0 and len(collector.prepared_sources) == 2
                digests = {
                    str(row.session.session_id): canonical_json(
                        row.session.model_dump(mode="json", exclude_none=True)
                    )
                    for row in collector.prepared_sources
                }
                return digests

        first = collect()
        replay = collect()
        assert first == replay

        paths = sorted(journals.glob("*.jsonl"))
        projection_calls = 0
        original_projection = graph_preparation.build_fact_rows

        def measured_projection(graph):
            nonlocal projection_calls
            projection_calls += 1
            return original_projection(graph)

        graph_preparation.build_fact_rows = measured_projection
        store = discover_store_from_files(paths).store
        graphs = list(store.session_graphs.values())
        prepared = [prepare_graph(graph) for graph in graphs]
        assert all(
            value.publication() == build_published_fact_set(graph)
            for graph, value in zip(graphs, prepared, strict=True)
        )
        assert all(len(value.summary.project_sessions) == 1 for value in prepared)
        cache_path = Path.home() / ".coding-trajectory/prepared-graphs.sqlite"
        with sqlite3.connect(cache_path) as db:
            before = db.execute(
                "SELECT key, body FROM prepared ORDER BY key"
            ).fetchall()
        assert len(before) == 2
        assert [prepare_graph(graph) for graph in graphs] == prepared
        assert projection_calls == 2
        with sqlite3.connect(cache_path) as db:
            assert (
                db.execute("SELECT key, body FROM prepared ORDER BY key").fetchall()
                == before
            )
        resolve_calls = 0

        def measured_resolve(*args, **kwargs):
            nonlocal resolve_calls
            resolve_calls += 1
            return discover_store_from_files(paths).store, "qualification"

        repo = LocalPublishedFactRepository(
            global_scope=True,
            current_dir=root,
            cache=IndexCache(),
            resolve=measured_resolve,
        )
        project_id = local_project_id(root, fallback="ignored display name")
        assert project_id == local_project_id(root, fallback="RenamedProject")
        assert project_id != local_project_id(root / "other", fallback="RenamedProject")
        listed = repo.response_for("project.sessions", {"project_id": project_id})
        assert len(listed["items"]) == 2
        assert {item["project_id"] for item in listed["items"]} == {project_id}
        assert repo.response_for(
            "project.sessions", {"project_id": str(UUID(int=999))}
        ) == {"items": []}
        changed = json.loads(paths[0].read_text().splitlines()[-1])
        changed["message"]["id"] = "assistant-1"
        changed["message"]["content"] = [
            {"type": "text", "text": "New unpublished local evidence"}
        ]
        with paths[0].open("a") as stream:
            stream.write(json.dumps(changed) + "\n")
        fresh_graphs = list(
            discover_store_from_files(paths).store.session_graphs.values()
        )
        assert (
            sum(
                graph_input_digest(a) != graph_input_digest(b)
                for a, b in zip(graphs, fresh_graphs, strict=True)
            )
            == 1
        )
        repo.response_for("project.sessions", {"project_id": project_id})
        with sqlite3.connect(cache_path) as db:
            assert db.execute("SELECT COUNT(*) FROM prepared").fetchone()[0] == 3
        index, _ = repo.store_for("graph.stats", {})
        assert (
            sum(
                row.kind == "item"
                for graph_id in index.graph_ids
                for row in index.rows_for_graph(graph_id)
            )
            == 3
        )
        assert projection_calls == 3
        graph_preparation.build_fact_rows = original_projection
        requests = [
            {
                "method": "graph.stats",
                "params": {"root_session_id": str(graph.root_session_id)},
            }
            for graph in graphs
        ]
        before_batch = resolve_calls
        with ServiceRuntime(
            global_scope=True, current_dir=root, historical_repository=repo
        ) as runtime:
            assert all(item["ok"] for item in runtime.batch(requests)["items"])
            assert resolve_calls == before_batch + 1
            assert runtime.execute(requests[0])["ok"]
            assert resolve_calls == before_batch + 2

        # Two real journal locations with the same display name must round-trip
        # independently through the public runtime, not merge under that name.
        other_path = root / "other" / root.name
        rows = [json.loads(line) for line in paths[1].read_text().splitlines()]
        rows[0]["payload"]["workspace_root"] = other_path.as_uri()
        paths[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
        with ServiceRuntime(global_scope=True, current_dir=root) as runtime:
            inventory = runtime.call("project.list", {})["items"]
            assert len(inventory) == 2
            assert len({entry["display_name"] for entry in inventory.values()}) == 1
            for selected_id in inventory:
                cards = runtime.call("project.sessions", {"project_id": selected_id})[
                    "items"
                ]
                assert len(cards) == 1 and cards[0]["project_id"] == selected_id
            try:
                runtime.call("project.sessions", {"project_name": root.name})
            except ValueError as error:
                assert "ambiguous" in str(error)
            else:
                raise AssertionError("same-name projects were silently merged")

        checkpoint_journals = root / "checkpoint-journals"
        checkpoint_journals.mkdir()
        os.environ["CT_AMP_LOG_DIR"] = str(checkpoint_journals)
        checkpoint_session = str(UUID(int=10))
        checkpoint_events = [
            {
                "schema_version": 1,
                "type": "thread",
                "captured_at": stamp,
                "payload": {
                    "id": "T-" + checkpoint_session,
                    "workspace_root": root.as_uri(),
                    "title": "Synthetic checkpoint",
                },
            },
            {
                "schema_version": 1,
                "type": "message",
                "captured_at": stamp,
                "thread_id": "T-" + checkpoint_session,
                "message": {
                    "id": "assistant-0",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Checkpoint evidence"}],
                },
            },
        ]
        (checkpoint_journals / "checkpoint.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in checkpoint_events)
        )
        remote = CheckpointRemote()
        checkpoint_identity = CollectorIdentity(
            workspace_id=UUID(int=1),
            agent_id=UUID(int=2),
            agent_instance_id=UUID(int=3),
            project_id=UUID(int=4),
            project_name="Checkpoint",
        )
        with LocalCollector(
            database_path=root / "checkpoint.sqlite3", identity=checkpoint_identity
        ) as collector:
            first_checkpoint = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=remote,
                heartbeat=False,
            )
            assert first_checkpoint.failed == 0 and len(remote.requests) == 1
            assert set(remote.requests[0].payload) == {
                "kind",
                "source_checkpoint",
                "session_digest",
            }
            collector._connection.execute(
                "UPDATE logical_sources SET snapshot_schema_version=?",
                ("ct.source_checkpoint.v1:ct-local-collector-v10",),
            )
            collector._connection.commit()
            rollover = collector.collect(
                current_dir=root,
                agent_vendor="amp",
                remote=remote,
                heartbeat=False,
            )
            assert rollover.failed == 0 and len(remote.requests) == 2
            assert remote.registrations[-1].rollover is True
            assert remote.registrations[-1].source_epoch == 2
            assert remote.requests[-1].source_epoch == 2
            assert remote.requests[-1].source_sequence == 0
            assert remote.publications >= 2
            assert collector._connection.execute(
                "SELECT COUNT(*) FROM publication_outbox WHERE state = 'accepted'"
            ).fetchone()[0]

        # A retired SQL publication must be an explicit local migration stop,
        # never silently discarded, rewritten, or sent to any remote endpoint.
        legacy_path = root / "legacy.sqlite3"
        with LocalCollector(
            database_path=legacy_path, identity=checkpoint_identity
        ) as collector:
            legacy_row = (
                "legacy-key",
                str(checkpoint_identity.project_id),
                7,
                "a" * 64,
                json.dumps({"publication": {"version": 1}, "fact_sets": []}),
                "pending",
                2,
                "legacy retry",
                stamp,
            )
            collector._connection.execute(
                "INSERT INTO publication_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                legacy_row,
            )
            collector._connection.commit()
        calls_before = (
            remote.recoveries,
            len(remote.registrations),
            len(remote.requests),
            remote.uploads,
            remote.publications,
        )
        with LocalCollector(
            database_path=legacy_path, identity=checkpoint_identity
        ) as collector:
            try:
                collector.collect(
                    current_dir=root,
                    agent_vendor="amp",
                    remote=remote,
                    heartbeat=False,
                )
            except RuntimeError as error:
                assert "legacy SQL publication" in str(error)
                assert "legacy-key" in str(error)
            else:
                raise AssertionError("pending legacy SQL publication was not rejected")
            preserved = tuple(
                collector._connection.execute(
                    "SELECT * FROM publication_outbox WHERE idempotency_key = ?",
                    ("legacy-key",),
                ).fetchone()
            )
            assert preserved == legacy_row
            for state in ("in_flight", "rejected"):
                collector._connection.execute(
                    "UPDATE publication_outbox SET state=?, last_error='conflict'",
                    (state,),
                )
                collector._connection.commit()
                before = tuple(
                    collector._connection.execute(
                        "SELECT * FROM publication_outbox"
                    ).fetchone()
                )
                try:
                    collector.flush(remote)
                except RuntimeError as error:
                    assert "legacy SQL publication" in str(error)
                else:
                    raise AssertionError("legacy retry was not rejected")
                assert (
                    tuple(
                        collector._connection.execute(
                            "SELECT * FROM publication_outbox"
                        ).fetchone()
                    )
                    == before
                )
        assert calls_before == (
            remote.recoveries,
            len(remote.registrations),
            len(remote.requests),
            remote.uploads,
            remote.publications,
        )
        print(
            json.dumps(
                {
                    "passed": 30,
                    "batch_resolves_once_then_refreshes": True,
                    "projection_calls_initial_replay_change": [2, 0, 1],
                    "same_name_projects_isolated": True,
                    "shared_preparation": True,
                    "unchanged_cache_entries": 2,
                    "changed_graph_cache_entries": 3,
                    "local_id_selection": True,
                    "deterministic_reparse": first == replay,
                    "checkpoint_requests": len(remote.requests),
                    "clean_rollover": True,
                    "artifact_retry_recovered": True,
                    "legacy_pending_preserved": True,
                    "legacy_pending_remote_calls": 0,
                    "network_requests": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
