"""Exercise release migration, durable canary and verification on loopback workerd."""

from __future__ import annotations

import base64
import gzip
import json
import os
import runpy
import sqlite3
import tempfile
from argparse import Namespace
from pathlib import Path
from uuid import UUID

from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.collector import _project_session_list_variants
from coding_trajectory.control_plane.connections import configure_profile


def main():
    module = runpy.run_path(str(Path(__file__).with_name("release-cloudflare.py")))
    with tempfile.TemporaryDirectory(prefix="ct-release-qualification-") as directory:
        root = Path(directory)
        os.environ["CT_CONNECTION_DIR"] = str(root)
        os.environ["CT_FIXTURE_OWNER"] = "local-qualification-owner-token-0000000001"
        os.environ["CT_FIXTURE_READER"] = "local-qualification-reader-token-000000001"
        os.environ["CT_FIXTURE_HUB"] = "http://127.0.0.1:8794"
        os.environ["CT_FIXTURE_ACCESS_ID"] = "fixture-id"
        os.environ["CT_FIXTURE_ACCESS_SECRET"] = "fixture-secret"
        workspace = UUID("00000000-0000-0000-0000-000000000001")
        agent = UUID("00000000-0000-0000-0000-000000000003")
        for role in ("collector", "reader"):
            configure_profile(
                profile_name=role,
                role=role,
                cloudflare_url="http://127.0.0.1:8794",
                workspace_id=workspace,
                agent_id=agent if role == "collector" else None,
                project_id=None,
                token=None,
                token_env="CT_FIXTURE_OWNER"
                if role == "collector"
                else "CT_FIXTURE_READER",
            )
        target = module["Target"](
            collector_profile="collector",
            reader_profile="reader",
            datahub_url_env="CT_FIXTURE_HUB",
            access_client_id_env="CT_FIXTURE_ACCESS_ID",
            access_client_secret_env="CT_FIXTURE_ACCESS_SECRET",
        )
        # The CLI never enables this option; the fixture alone allows 127.0.0.1.
        connection = module["Connections"](target, allow_loopback=True)
        try:
            migrations = []
            connection.migrate(migrations.append)
            assert migrations
            commit, _clean = module["source"]()
            state = root / "release.canary.sqlite3"
            try:
                session = connection.canary(state, commit)
            except module["ReleaseError"]:
                with sqlite3.connect(state) as db:
                    phases = db.execute(
                        "SELECT state,phase,error_code FROM sync_batches"
                    ).fetchall()
                    authority = db.execute(
                        "SELECT value FROM sync_meta WHERE key='authority_error'"
                    ).fetchall()
                raise AssertionError(
                    {"canary_phases": phases, "authority": authority}
                ) from None
            before = connection.core("ct_publication_watermark", {}, reader=True)[1][
                "data"
            ]["published_sequence"]
            assert connection.canary(state, commit) == session
            after = connection.core("ct_publication_watermark", {}, reader=True)[1][
                "data"
            ]["published_sequence"]
            assert after == before
            versions = {
                "authority": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                "datahub": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            }
            checks = connection.verify(versions, session)
            assert all(checks.values())
            historical = connection.core(
                "ct_historical_artifacts", {"resource_ids": [str(session)]}, reader=True
            )[1]["data"]["artifacts"][0]
            canonical = gzip.decompress(base64.b64decode(historical["payload_base64"]))
            artifact = ChronicleGraphArtifact.model_validate_json(canonical)
            projections = _project_session_list_variants(artifact)
            assert projections["canonical"]["trees"]
            projections["canonical"]["trees"] = {}
            # A valid-schema conflicting projection must keep its explicit 409
            # across the private DO RPC boundary and leave the publication intact.
            compressed = gzip.compress(canonical, mtime=0)
            status, conflict, _ = connection.core(
                "ct_collector_stage_artifact_payload",
                {
                    "agent_id": str(agent),
                    "schema_version": artifact.schema_version,
                    "content_sha256": artifact.digest(),
                    "encoding": "gzip",
                    "uncompressed_bytes": len(canonical),
                    "compressed_bytes": len(compressed),
                    "payload_base64": base64.b64encode(compressed).decode(),
                    "projections": projections,
                },
            )
            assert status == 409 and conflict["error"]["code"] == "projection_conflict"
            assert all(connection.verify(versions, session).values())
            try:
                connection.verify({**versions, "authority": UUID(int=1)}, session)
            except module["ReleaseError"]:
                pass
            else:
                raise AssertionError("mismatched deployed version accepted")
            receipt = module["Receipt"](
                commit=commit,
                clean_source=False,
                environment="staging",
                source_digest=module["source_digest"](),
                versions=versions,
                canary_session=session,
                checks=checks,
                status="verified",
            )
            path = root / "receipt.json"
            module["persist"](path, receipt)
            encoded = path.read_text()
            assert (
                "fixture-secret" not in encoded and "local-qualification" not in encoded
            )
            assert (
                module["Receipt"].model_validate_json(encoded).canary_session == session
            )
            for environment in ("staging", "production"):
                module["configurations"](environment)
            # Real persisted receipts must fail closed before deployment or
            # credentials when source or qualification evidence disagrees.
            invalid = receipt.model_copy(update={"source_digest": "0" * 64})
            module["persist"](path, invalid)
            args = Namespace(
                environment="staging",
                action="verify",
                receipt=path,
                staging_receipt=None,
            )
            for invalid_receipt, action, expected in (
                (invalid, "verify", "receipt_source_changed_use_new_receipt"),
                (receipt, "deploy", "deployment_requires_committed_clean_source"),
                (
                    receipt.model_copy(update={"commit": "0" * 40}),
                    "verify",
                    "receipt_source_or_environment_mismatch",
                ),
            ):
                module["persist"](path, invalid_receipt)
                args.action = action
                try:
                    module["execute"](args)
                except module["ReleaseError"] as error:
                    assert str(error) == expected
                else:
                    raise AssertionError("invalid release evidence was accepted")
            print(
                json.dumps(
                    {
                        "passed": 18,
                        "canary_replay_new_publications": after - before,
                        "deployed": False,
                        "remote_data_used": False,
                    }
                )
            )
        finally:
            connection.close()


if __name__ == "__main__":
    main()
