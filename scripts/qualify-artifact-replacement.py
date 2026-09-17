#!/usr/bin/env python3
"""Qualify targeted artifact replacement against disposable loopback Wrangler.

Run ``prepare`` before starting Wrangler so its exact replacement digest can be
bound to ``CT_REPLACEMENT_EXPORT_SHA256``. Run ``qualify`` only against a fresh,
disposable local persistence directory. This script refuses non-loopback URLs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid5

import httpx
from coding_trajectory.control_plane.artifact_replacement import (
    import_replacement,
    load_replacement,
    replacement_digest,
    verify_replacement,
)
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    _prepared_graph_summary,
)
from coding_trajectory.control_plane.collector_protocol import (
    ProjectRegistrationRequest,
)
from coding_trajectory.ingestion.common import canonical_json

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "control_plane_qualification", ROOT / "scripts/qualify-cloudflare-control-plane.py"
)
BASE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BASE)

WORKSPACE = UUID(BASE.WORKSPACE)
AGENT = UUID(BASE.AGENT)
OTHER_WORKSPACE = UUID(BASE.SECOND_WORKSPACE)
OTHER_AGENT = UUID(BASE.SECOND_AGENT)
TOKENS = BASE.TOKENS
STAMP = datetime(2026, 9, 17, 12, tzinfo=UTC)
checks = 0


def check(value: object, label: str) -> None:
    global checks
    if not value:
        raise AssertionError(label)
    checks += 1


def make_bundle(
    root: Path, *, workspace_id: UUID, agent_id: UUID, seed: str, project: str
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    facts = BASE.synthetic_fact_set(seed=seed, project=project, include_unknown=False)
    summary = _prepared_graph_summary(facts)
    objects: dict[str, tuple[bytes, str]] = {}
    for kind, value in (("facts", facts), ("summary", summary)):
        body = canonical_json(value.model_dump(mode="json", exclude_none=True)).encode()
        sha256 = hashlib.sha256(body).hexdigest()
        path = root / "objects" / kind / f"{sha256}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        objects[kind] = (body, sha256)
    source_key = uuid5(workspace_id, seed + ":source")
    raw = {
        "schema_version": "ct.artifact-replacement.v1",
        "privacy_contract": "ct.published-facts-only.v1",
        "raw_sources_included": False,
        "workspace_id": str(workspace_id),
        "agent_id": str(agent_id),
        "generated_at": STAMP.isoformat().replace("+00:00", "Z"),
        "since_days": 7,
        "project_name": project,
        "repository_identity": f"https://example.invalid/{seed}.git",
        "project_aliases": [f"{project}-alias"],
        "sources": [
            {
                "source_key": str(source_key),
                "vendor": "amp",
                "observed_at": STAMP.isoformat().replace("+00:00", "Z"),
                "parser_version": "artifact-replacement-qualification.v1",
                "checkpoint_segments": [4],
                "session_digest": hashlib.sha256(seed.encode()).hexdigest(),
            }
        ],
        "graphs": [
            {
                "graph_id": str(facts.graph_id),
                "graph_input_sha256": facts.fact_set_digest,
                "fact_set_digest": facts.fact_set_digest,
                "fact_count": len(facts.rows),
                "source_keys": [str(source_key)],
                "vendors": ["amp"],
                "observed_at": STAMP.isoformat().replace("+00:00", "Z"),
                "facts": {
                    "kind": "facts",
                    "sha256": objects["facts"][1],
                    "bytes": len(objects["facts"][0]),
                },
                "summary": {
                    "kind": "summary",
                    "sha256": objects["summary"][1],
                    "bytes": len(objects["summary"][0]),
                },
            }
        ],
        "object_count": 2,
        "total_bytes": sum(len(item[0]) for item in objects.values()),
    }
    raw["replacement_sha256"] = replacement_digest(raw)
    (root / "replacement.json").write_text(canonical_json(raw) + "\n", encoding="utf-8")
    load_replacement(root)
    return root


def rpc(
    url: str,
    token: str,
    method: str,
    params: dict[str, object],
    *,
    status: int = 200,
) -> dict[str, object]:
    response = httpx.post(
        url + "/v1/core",
        headers={"Authorization": "Bearer " + token},
        json={
            "protocol": "ct.core.v1",
            "id": None,
            "method": method,
            "params": params,
        },
        timeout=60,
    )
    check(response.status_code == status, f"{method} returned {response.status_code}")
    return response.json()


def replacement_request(bundle, mode: str) -> dict[str, object]:
    workspace = str(bundle.manifest.workspace_id)
    digest = bundle.manifest.replacement_sha256
    return {
        "workspace_id": workspace,
        "mode": mode,
        "expected_export_sha256": digest,
        "confirmation": f"{mode}:{workspace}:{digest}",
    }


async def seed_orphans(url: str, count: int) -> None:
    semaphore = asyncio.Semaphore(64)
    headers = {"Authorization": "Bearer " + TOKENS["owner"]}

    async def put(index: int, client: httpx.AsyncClient) -> None:
        body = canonical_json(
            {"schema_version": "ct.prepared-summary.v1", "orphan": index}
        ).encode()
        sha256 = hashlib.sha256(body).hexdigest()
        async with semaphore:
            response = await client.put(
                f"{url}/v1/artifacts/summary/{sha256}", content=body, headers=headers
            )
        if response.status_code != 200:
            raise AssertionError(f"orphan upload {index}: {response.status_code}")

    async with httpx.AsyncClient(timeout=90) as client:
        await asyncio.gather(*(put(index, client) for index in range(count)))


def reject_private_bundle(source: Path, target: Path) -> None:
    shutil.copytree(source, target)
    manifest_path = target / "replacement.json"
    manifest = json.loads(manifest_path.read_text())
    reference = manifest["graphs"][0]["summary"]
    old_path = target / "objects" / "summary" / f"{reference['sha256']}.json"
    value = json.loads(old_path.read_text())
    value["raw_log"] = "must never leave the source host"
    body = canonical_json(value).encode()
    sha256 = hashlib.sha256(body).hexdigest()
    new_path = target / "objects" / "summary" / f"{sha256}.json"
    old_path.unlink()
    new_path.write_bytes(body)
    reference.update({"sha256": sha256, "bytes": len(body)})
    manifest["total_bytes"] = sum(
        path.stat().st_size for path in (target / "objects").glob("*/*.json")
    )
    manifest["replacement_sha256"] = replacement_digest(manifest)
    manifest_path.write_text(canonical_json(manifest) + "\n")


def qualify(root: Path, url: str) -> dict[str, object]:
    target = load_replacement(root / "target")
    old = load_replacement(root / "old")
    other = load_replacement(root / "other")
    check(target.manifest.workspace_id == WORKSPACE, "target workspace is exact")
    check(target.manifest.since_days == 7, "replacement window is exactly seven days")

    try:
        load_replacement(root / "private")
    except ValueError as exc:
        check("forbidden private field" in str(exc), "private raw field is rejected")
    else:
        raise AssertionError("private replacement was accepted")

    import_replacement(old, url=url, access_token=TOKENS["owner"])
    import_replacement(other, url=url, access_token=TOKENS["owner_b"])
    asyncio.run(seed_orphans(url, 4101))

    request = replacement_request(target, "preview")
    preview = rpc(url, TOKENS["owner"], "ct_workspace_replace", request)["data"]
    check(
        preview["sql"]["tables"]["artifact_manifests"] == 1,
        "preview counts target manifest",
    )
    check(preview["r2"]["objects"] == 4103, "preview counts exact target prefix")
    check(not preview["r2"]["truncated"], "preview inventory is complete")
    check(
        "other R2 prefixes" in preview["preserved"], "preview states prefix isolation"
    )
    check("does not restore" in preview["quota"], "preview states quota boundary")

    rpc(url, TOKENS["reader"], "ct_workspace_replace", request, status=403)
    wrong_hash = {**request, "expected_export_sha256": "b" * 64}
    rpc(url, TOKENS["owner"], "ct_workspace_replace", wrong_hash, status=403)
    wrong_confirmation = {**request, "confirmation": "preview:wrong"}
    rpc(url, TOKENS["owner"], "ct_workspace_replace", wrong_confirmation, status=403)
    other_target = {**request, "workspace_id": str(OTHER_WORKSPACE)}
    rpc(url, TOKENS["owner_b"], "ct_workspace_replace", other_target, status=403)

    execute = replacement_request(target, "execute")
    first = rpc(url, TOKENS["owner"], "ct_workspace_replace", execute)["data"]
    check(first["sql_reset"] is True, "target SQL reset completed")
    check(first["deleted"] == 4000 and not first["complete"], "R2 reset is bounded")
    blocked = rpc(
        url,
        TOKENS["owner"],
        "ct_project_register",
        {
            "workspace_id": str(WORKSPACE),
            "agent_id": str(AGENT),
            "display_name": "Blocked while replacement is incomplete",
        },
        status=409,
    )
    check(
        blocked["error"]["code"] == "workspace_replacement_incomplete",
        "normal collection is blocked while reset is incomplete",
    )
    snapshot = rpc(
        url,
        TOKENS["owner"],
        "ct_workspace_snapshot",
        {"workspace_id": str(WORKSPACE)},
    )["data"]
    check(
        snapshot["snapshot_sequence"] == 0,
        "target SQL remains reset after partial R2 cleanup",
    )
    verify_replacement(other, url=url, access_token=TOKENS["owner_b"])
    # Discard the successful response after server completion, then make the
    # exact retry a client would make after a lost response.
    lost = httpx.post(
        url + "/v1/core",
        headers={"Authorization": "Bearer " + TOKENS["owner"]},
        json={
            "protocol": "ct.core.v1",
            "id": None,
            "method": "ct_workspace_replace",
            "params": execute,
        },
        timeout=60,
    )
    check(lost.status_code == 200, "server completed reset before response loss")
    second = rpc(url, TOKENS["owner"], "ct_workspace_replace", execute)["data"]
    check(
        second["deleted"] == 0
        and second["complete"]
        and second["already_complete"]
        and not second["sql_reset"],
        "lost completion response retry is non-destructive",
    )
    verify_replacement(other, url=url, access_token=TOKENS["owner_b"])

    # Model an interruption immediately after project registration. The importer
    # accepts only the exact matching sole project and safely resumes normal APIs.
    remote = CloudflareCollectorRemote(url=url, access_token=TOKENS["owner"])
    try:
        remote.register_project(
            ProjectRegistrationRequest(
                workspace_id=target.manifest.workspace_id,
                agent_id=target.manifest.agent_id,
                display_name=target.manifest.project_name,
                repository_identity=target.manifest.repository_identity,
                aliases=target.manifest.project_aliases,
            )
        )
    finally:
        remote.close()
    imported = import_replacement(target, url=url, access_token=TOKENS["owner"])
    check(
        imported["graphs"] == len(target.manifest.graphs),
        "validated replacement imports exactly",
    )
    check(
        imported["objects"] == target.manifest.object_count,
        "import verifies exact object count",
    )
    check(
        imported["bytes"] == target.manifest.total_bytes, "import verifies exact bytes"
    )
    repeated = import_replacement(target, url=url, access_token=TOKENS["owner"])
    check(repeated == imported, "committed import retry is idempotent")
    before_repeat = rpc(
        url,
        TOKENS["owner"],
        "ct_workspace_snapshot",
        {"workspace_id": str(WORKSPACE)},
    )["data"]
    protected = rpc(url, TOKENS["owner"], "ct_workspace_replace", execute)["data"]
    check(
        protected["deleted"] == 0
        and protected["already_complete"]
        and not protected["sql_reset"],
        "execute after import is non-destructive",
    )
    after_repeat = verify_replacement(target, url=url, access_token=TOKENS["owner"])
    check(
        after_repeat["snapshot_sequence"] == before_repeat["snapshot_sequence"],
        "execute after import preserves replacement SQL and R2",
    )
    verify_replacement(other, url=url, access_token=TOKENS["owner_b"])
    return {
        "status": "ok",
        "checks": checks,
        "workspace_id": str(WORKSPACE),
        "replacement_sha256": target.manifest.replacement_sha256,
        "sources": len(target.manifest.sources),
        "graphs": len(target.manifest.graphs),
        "objects": target.manifest.object_count,
        "bytes": target.manifest.total_bytes,
        "first_reset_deleted": first["deleted"],
        "completion_deleted_before_lost_response": 103,
        "lost_response_retry_deleted": second["deleted"],
        "post_import_retry_deleted": protected["deleted"],
        "other_workspace_verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "qualify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8794")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.mode == "prepare":
        if root.exists():
            shutil.rmtree(root)
        make_bundle(
            root / "target",
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            seed="replacement",
            project="Replacement",
        )
        make_bundle(
            root / "old",
            workspace_id=WORKSPACE,
            agent_id=AGENT,
            seed="old",
            project="Old",
        )
        make_bundle(
            root / "other",
            workspace_id=OTHER_WORKSPACE,
            agent_id=OTHER_AGENT,
            seed="other",
            project="Other",
        )
        reject_private_bundle(root / "target", root / "private")
        replacement = load_replacement(root / "target")
        print(
            json.dumps({"status": "prepared", **replacement.summary()}, sort_keys=True)
        )
        return 0
    parsed = urlparse(args.url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("qualification refuses a non-loopback URL")
    print(json.dumps(qualify(root, args.url), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
