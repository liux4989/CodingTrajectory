"""Qualify deterministic collector preparation through real journals and SQLite."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.collector import CollectorIdentity, LocalCollector
from coding_trajectory.control_plane.collector_protocol import (
    CollectorRecoveryResponse,
    ObservationReceipt,
    ObservationRequest,
    SourceRegistrationRequest,
    SourceRegistrationResponse,
)
from coding_trajectory.ingestion.common import canonical_json


class CheckpointRemote:
    def __init__(self) -> None:
        self.registrations: list[SourceRegistrationRequest] = []
        self.requests: list[ObservationRequest] = []

    def recover(self, _request) -> CollectorRecoveryResponse:
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
            outcome="rejected",
        )


def main():
    with tempfile.TemporaryDirectory(prefix="ct-preparation-reuse-") as directory:
        root = Path(directory)
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
        print(
            json.dumps(
                {
                    "passed": 5,
                    "deterministic_reparse": first == replay,
                    "checkpoint_requests": len(remote.requests),
                    "clean_rollover": True,
                    "network_requests": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
