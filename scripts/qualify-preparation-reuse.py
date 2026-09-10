"""Qualify normalized-input reuse through real journals and persistent SQLite."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.collector import CollectorIdentity, LocalCollector


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
                    str(row.artifact.graph.root_session_id): row.artifact.digest()
                    for row in collector.prepared_sources
                }
                return collector.normalization_cache_hits, digests

        first_hits, first = collect()
        reused_hits, reused = collect()
        assert first_hits == 0 and reused_hits == 2 and first == reused
        path = journals / "1.jsonl"
        # Same-length in-place edit must invalidate the prefix fingerprint.
        path.write_text(path.read_text().replace("reuse evidence", "other evidence"))
        changed_hits, changed = collect()
        assert changed_hits == 1 and changed != first
        assert changed[str(UUID(int=2))] == first[str(UUID(int=2))]
        _, replay = collect()
        assert replay == changed
        with LocalCollector(
            database_path=root / "capture.sqlite3", identity=identity
        ) as collector:
            collector._connection.execute(
                "UPDATE normalization_cache SET body=?", (b"corrupt",)
            )
            collector._connection.commit()
        repaired_hits, repaired = collect()
        assert repaired_hits == 0 and repaired == changed
        print(
            json.dumps(
                {
                    "passed": 6,
                    "unchanged_reused": reused_hits,
                    "changed_pass_reused": changed_hits,
                    "network_requests": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
