"""Executable release qualification: real local children, synthetic remote outcomes."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx
from coding_trajectory.control_plane.connections import (
    CollectorCredentialProfile,
    CollectorCredentials,
)


def blocked(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("unsafe operation accepted")


def main():
    spec = importlib.util.spec_from_file_location(
        "deploy_release", Path(__file__).with_name("deploy-release.py")
    )
    release = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = release
    spec.loader.exec_module(release)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="ct-release-qualification-") as temporary:
        root = Path(temporary)
        with release.Release(root / "phases") as job:
            blocked(lambda: release.Release(job.directory).__enter__())
            artifact = job.directory / "artifact"
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('artifact').write_text('frozen'); Path('STOP').touch()",
            ]
            job.phase("qualification", command, cwd=job.directory, outputs=(artifact,))
            assert artifact.read_text() == "frozen"
            blocked(
                lambda: job.phase(
                    "next", [sys.executable, "-c", "raise Exception('must not run')"]
                )
            )
            (job.directory / "STOP").unlink()
            before = (job.directory / "audit.jsonl").read_bytes()
            job.phase("qualification", ["must-not-execute"])
            assert (job.directory / "audit.jsonl").read_bytes() == before
            artifact.write_text("changed")
            blocked(lambda: job.phase("qualification", ["must-not-execute"]))
            artifact.write_text("frozen")
            body = (job.directory / "qualification.json").read_bytes()
            (job.directory / "qualification.json").write_bytes(body + b" ")
            blocked(lambda: job.read("qualification.json"))

        remote = {"deployment": str(UUID(int=1)), "worker_version": str(UUID(int=2))}
        workspace = {
            "profile": "synthetic",
            "workspace": str(UUID(int=3)),
            "manifest_sha256": "a" * 64,
        }
        bundle = {"entry": "worker/index.js", "files": {"worker/index.js": "b" * 64}}
        for outcome in ("success", "lost-response", "not-committed"):
            with release.Release(root / outcome) as job:
                job.put("plan.json", {"source": "c" * 40})
                calls = []
                annotations = {}

                def remote_output(
                    command, calls=calls, annotations=annotations, **kwargs
                ):
                    assert command[1:3] == ["versions", "view"]
                    calls.append("read")
                    return json.dumps({"annotations": annotations})

                def deploy_phase(
                    name,
                    command,
                    calls=calls,
                    annotations=annotations,
                    outcome=outcome,
                    **kwargs,
                ):
                    assert name == "deployment"
                    assert (
                        release.events(job.directory)[-1]["event"]
                        == "deployment_started"
                    )
                    calls.append("write")
                    if outcome != "not-committed":
                        annotations.update(
                            {"workers/message": command[-1], "workers/tag": command[-3]}
                        )
                    if outcome != "success":
                        raise ValueError("unknown outcome")

                with (
                    patch.object(job, "verify", return_value=bundle),
                    patch.object(job, "remote_status", return_value=remote),
                    patch.object(job, "live", return_value=[workspace]),
                    patch.object(job, "phase", side_effect=deploy_phase),
                    patch.object(release, "output", side_effect=remote_output),
                ):
                    approved = job.preflight(["synthetic"])["activation_sha256"]
                    with patch.object(
                        job,
                        "live",
                        return_value=[{**workspace, "manifest_sha256": "d" * 64}],
                    ):
                        blocked(lambda approved=approved: job.deploy(approved))
                    assert calls == []
                    if outcome == "success":
                        assert job.deploy(approved)["state"] == "code_active"
                    else:
                        blocked(lambda approved=approved: job.deploy(approved))
                    blocked(lambda approved=approved: job.deploy(approved))
                    result = job.reconcile()
                    assert result["state"] == (
                        "unknown" if outcome == "not-committed" else "code_active"
                    )
                    assert calls.count("write") == 1
                    assert (
                        result["publication_attempts"] == result["remote_writes"] == 0
                    )
                    if outcome != "not-committed":
                        receipt = (job.directory / "receipt.json").read_bytes()
                        job.reconcile()
                        assert (job.directory / "receipt.json").read_bytes() == receipt

        ref = {"kind": "api", "sha256": "a" * 64, "bytes": 23}
        manifest = {
            "schema_version": "ct.artifact-manifest.v2",
            "preparation_version": "ct.graph-preparation.v6",
            "workspace_id": str(UUID(int=3)),
            "project_id": str(UUID(int=4)),
            "publisher_agent_id": str(UUID(int=5)),
            "publication_sequence": 2,
            "snapshot_sequence": 7,
            "published_at": "2026-09-20T00:00:00Z",
            "inventory_state": "complete",
            "graphs": [
                {
                    "graph_id": str(UUID(int=6)),
                    "fact_set_digest": "f" * 64,
                    "fact_count": 1,
                    "observed_at": "2026-09-20T00:00:00Z",
                    "vendors": ["amp"],
                    "facts": {**ref, "kind": "facts"},
                    "summary": {**ref, "kind": "summary"},
                    "api_objects": [ref],
                    "api_methods": [
                        {
                            "method": "session.overview",
                            "method_version": 5,
                            "scope": str(UUID(int=6)),
                            "index": ref,
                        }
                    ],
                }
            ],
        }
        response = {"workspace_id": str(UUID(int=3)), "manifests": [manifest]}
        assert release.compatible(response) == {
            "projects": 1,
            "graphs": 1,
            "methods": 1,
        }
        old = copy.deepcopy(response)
        old["manifests"][0]["graphs"][0]["api_methods"][0]["method_version"] = 4
        blocked(lambda: release.compatible(old))
        wrong = {**response, "workspace_id": str(UUID(int=9))}
        blocked(lambda: release.compatible(wrong))
        blocked(lambda: release.compatible({**response, "manifests": []}))
        credentials = CollectorCredentials(
            profile=CollectorCredentialProfile(
                cloudflare_url="http://localhost",
                workspace_id=UUID(int=3),
                role="reader",
                token_env="SYNTHETIC_TOKEN",
            ),
            access_token="synthetic-private-token",
        )
        expected_version = str(UUID(int=55))
        observed_version = expected_version
        requests = []

        def authority(request):
            envelope = json.loads(request.content)
            assert envelope["method"] == "ct_artifact_manifest"
            assert envelope["params"] == {"workspace_id": str(UUID(int=3))}
            requests.append(envelope["method"])
            return httpx.Response(
                200,
                headers={"X-CT-Worker-Version": observed_version},
                json={"ok": True, "data": response},
            )

        client = httpx.Client
        with (
            release.Release(root / "live") as job,
            patch.object(release, "load_profile_credentials", return_value=credentials),
            patch.object(
                httpx,
                "Client",
                side_effect=lambda **kw: client(
                    transport=httpx.MockTransport(authority), **kw
                ),
            ),
        ):
            assert job.live(["synthetic"], expected_version)[0]["methods"] == 1
            observed_version = str(UUID(int=56))
            blocked(lambda: job.live(["synthetic"], expected_version))
            assert requests == ["ct_artifact_manifest", "ct_artifact_manifest"]
            assert not (job.directory / "audit.jsonl").exists()
    print(
        "PASS release: exclusive owner, drained stop, cached resume, artifact/receipt tampering, stale approval, lost response reconciliation, no deploy replay, v4 rejection/v5 acceptance, workspace isolation"
    )


if __name__ == "__main__":
    main()
