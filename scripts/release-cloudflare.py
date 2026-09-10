"""One explicit, resumable Cloudflare release workflow; plan is the default.

Connection profiles and supervisor/CI secrets remain outside release receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from coding_trajectory.control_plane.chronicle import ChronicleGraphArtifact
from coding_trajectory.control_plane.collector import (
    CloudflareCollectorRemote,
    CollectorIdentity,
)
from coding_trajectory.control_plane.connections import (
    load_profile,
    load_profile_credentials,
)
from coding_trajectory.control_plane.upload_capture import (
    CanonicalCapture,
    CaptureSource,
    UploadCapturePage,
)
from coding_trajectory.control_plane.upload_service import UploadService
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "cloudflare/control-plane"
WEB = ROOT / "packages/plugins/datahub/web"
CONFIGS = {
    "authority": CORE / "wrangler.jsonc",
    "datahub": WEB.parent / "wrangler.live.jsonc",
}
STEPS = ["qualification", "authority", "migration", "datahub", "canary", "verification"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collector_profile: str
    reader_profile: str
    datahub_url_env: str
    access_client_id_env: str
    access_client_secret_env: str


class ReleaseConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    staging: Target
    production: Target


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ct.release.v1"] = "ct.release.v1"
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    environment: Literal["staging", "production"]
    clean_source: bool
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "qualified", "verified", "failed"] = "pending"
    completed: list[str] = Field(default_factory=list)
    versions: dict[str, UUID] = Field(default_factory=dict)
    previous_versions: dict[str, UUID] = Field(default_factory=dict)
    migrated_records: int = 0
    canary_session: UUID | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    updated_at: str = ""
    failure: str | None = None


class ReleaseError(RuntimeError):
    pass


def command(argv: list[str], *, timeout: int = 180) -> str:
    # Capture command output locally; never attach it to a remote/CI receipt.
    try:
        result = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError("release_command_unavailable") from None
    if result.returncode:
        raise ReleaseError("release_command_failed")
    return result.stdout


def source() -> tuple[str, bool]:
    return command(["git", "rev-parse", "HEAD"]).strip(), not bool(
        command(["git", "status", "--porcelain"]).strip()
    )


def source_digest() -> str:
    digest = hashlib.sha256()
    for name in sorted(
        command(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        ).split("\0")
    ):
        if not name:
            continue
        path = ROOT / name
        digest.update(name.encode() + b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"deleted")
    return digest.hexdigest()


def persist(path: Path, receipt: Receipt) -> None:
    receipt.updated_at = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".release-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(receipt.model_dump_json(indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configurations(environment: str) -> dict[str, dict[str, Any]]:
    result = {}
    for component, path in CONFIGS.items():
        document = json.loads(path.read_text())
        stage = document["env"]["staging"]
        if stage["name"] == document["name"]:
            raise ReleaseError("staging_worker_not_isolated")
        result[component] = {**document, **(stage if environment == "staging" else {})}
    core_document = json.loads(CONFIGS["authority"].read_text())
    if {v["bucket_name"] for v in core_document["r2_buckets"]} & {
        v["bucket_name"] for v in core_document["env"]["staging"]["r2_buckets"]
    }:
        raise ReleaseError("staging_storage_not_isolated")
    binding = next(v for v in result["datahub"]["services"] if v["binding"] == "CORE")
    if binding["service"] != result["authority"]["name"]:
        raise ReleaseError("release_binding_target_mismatch")
    return result


def wrangler(component: str, environment: str, *args: str) -> str:
    argv = [
        "node",
        str(CORE / "node_modules/wrangler/bin/wrangler.js"),
        *args,
        "--config",
        str(CONFIGS[component]),
    ]
    argv += ["--env", "staging" if environment == "staging" else ""]
    return command(argv)


def active_version(component: str, environment: str) -> UUID:
    deployments = json.loads(
        wrangler(component, environment, "deployments", "list", "--json")
    )
    if not deployments:
        raise ReleaseError("deployment_missing")
    newest = max(deployments, key=lambda row: row["created_on"])
    versions = newest["versions"]
    if len(versions) != 1 or versions[0]["percentage"] != 100:
        raise ReleaseError("release_requires_single_active_version")
    return UUID(versions[0]["version_id"])


def qualify() -> None:
    # Sequential orchestration keeps build/test fan-out below two jobs.
    for argv in (
        ["npm", "--prefix", str(CORE), "run", "check"],
        ["npm", "--prefix", str(WEB), "run", "check:worker"],
        ["npm", "--prefix", str(WEB), "run", "check:api"],
        ["node", str(ROOT / "scripts/qualify-refactor-runtime.mjs"), "--all"],
        ["bash", str(ROOT / "scripts/check-metrics-quality-gate.sh")],
        ["uv", "run", "python", str(ROOT / "scripts/validate-metrics-baselines.py")],
        ["npm", "--prefix", str(WEB), "run", "build:hosted"],
    ):
        command(argv, timeout=900)


def require_https(value: str, *, allow_loopback: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        not (
            parsed.scheme == "https"
            or (
                allow_loopback
                and parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
            )
        )
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ReleaseError("release_endpoint_requires_https_origin")
    return value.rstrip("/")


class Connections:
    def __init__(self, target: Target, *, allow_loopback: bool = False):
        self.collector = load_profile_credentials(target.collector_profile)
        self.reader = load_profile_credentials(target.reader_profile)
        if (
            self.collector.profile.role != "collector"
            or self.reader.profile.role != "reader"
            or self.collector.profile.workspace_id != self.reader.profile.workspace_id
            or self.collector.profile.cloudflare_url
            != self.reader.profile.cloudflare_url
        ):
            raise ReleaseError("release_connection_scope_mismatch")
        self.origin = require_https(
            str(self.collector.profile.cloudflare_url), allow_loopback=allow_loopback
        )
        self.hub = require_https(
            os.environ.get(target.datahub_url_env, ""), allow_loopback=allow_loopback
        )
        self.access = {
            "CF-Access-Client-Id": os.environ.get(target.access_client_id_env, ""),
            "CF-Access-Client-Secret": os.environ.get(
                target.access_client_secret_env, ""
            ),
        }
        if not all(self.access.values()):
            raise ReleaseError("release_access_service_credentials_missing")
        self.workspace = str(self.collector.profile.workspace_id)
        self.agent = str(self.collector.profile.agent_id)
        self.client = httpx.Client(timeout=30, follow_redirects=False)

    def close(self):
        self.client.close()

    def request(
        self, url: str, *, headers=None, body=None
    ) -> tuple[int, dict[str, Any], httpx.Headers]:
        # Never follow redirects with credentials or read an unbounded response.
        with self.client.stream(
            "POST" if body is not None else "GET", url, headers=headers, json=body
        ) as response:
            encoded = bytearray()
            for chunk in response.iter_bytes():
                encoded.extend(chunk)
                if len(encoded) > 1024 * 1024:
                    raise ReleaseError("release_response_budget")
            try:
                payload = json.loads(encoded)
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            return response.status_code, payload, response.headers

    def core(self, method: str, params: dict[str, Any], *, reader=False):
        token = self.reader.access_token if reader else self.collector.access_token
        return self.request(
            self.origin + "/v1/core",
            headers={"Authorization": f"Bearer {token}"},
            body={
                "protocol": "ct.core.v1",
                "method": method,
                "params": {"workspace_id": self.workspace, **params},
            },
        )

    def migrate(self, checkpoint) -> None:
        for _ in range(10000):
            status, envelope, _ = self.core(
                "ct_catalog_migrate", {"agent_id": self.agent}
            )
            if status != 200 or not envelope.get("ok"):
                raise ReleaseError("catalog_migration_failed")
            data = envelope["data"]
            checkpoint(int(data["migrated"]))
            if data["complete"]:
                return
        raise ReleaseError("catalog_migration_budget_resume_required")

    def canary(self, path: Path, commit: str) -> UUID:
        session = uuid5(NAMESPACE_URL, f"ct-release:{self.workspace}:{commit}")
        stamp = command(["git", "show", "-s", "--format=%cI", commit]).strip()
        artifact = ChronicleGraphArtifact.model_validate(
            {
                "graph": {
                    "root_session_id": session,
                    "project": "Release-Canary",
                    "session_count": 1,
                    "turn_count": 1,
                    "item_count": 0,
                },
                "sessions": [
                    {
                        "session_id": session,
                        "vendor": "codex_cli",
                        "started_at": stamp,
                        "ended_at": stamp,
                        "status": "not_living",
                        "turns": [
                            {
                                "turn_id": uuid5(session, "turn"),
                                "sequence": 0,
                                "started_at": stamp,
                                "completed_at": stamp,
                                "status": "completed",
                            }
                        ],
                    }
                ],
            }
        )
        identity = CollectorIdentity(
            workspace_id=UUID(self.workspace),
            agent_id=UUID(self.agent),
            agent_instance_id=uuid5(session, "collector"),
            project_name="Release-Canary",
        )
        service = UploadService(path, identity)
        remote = CloudflareCollectorRemote(
            url=self.origin, access_token=self.collector.access_token, chunked=True
        )
        try:
            service.prepare_capture(
                UploadCapturePage(
                    repository_id=str(session),
                    cursor=str(session),
                    captures=(
                        CanonicalCapture(
                            artifact=artifact,
                            sources=(
                                CaptureSource(
                                    vendor="codex_cli",
                                    native_session_id=str(session),
                                    segments=(1,),
                                    chronicle_digest=artifact.digest(),
                                    observed_at=stamp,
                                    source_generation="release-v1",
                                ),
                            ),
                        ),
                    ),
                )
            )
            status = service.publish(remote, max_batches=1)
            if status["pending_bytes"] or status["authority_error"]:
                raise ReleaseError("canary_publication_pending_resume_required")
            return session
        finally:
            remote.close()
            service.close()

    def verify(
        self, versions: dict[str, UUID], canary_session: UUID | None = None
    ) -> dict[str, bool]:
        status, envelope, headers = self.core("ct_connection_status", {}, reader=True)
        if (
            status != 200
            or not envelope.get("ok")
            or envelope["data"].get("roles") != ["read"]
            or headers.get("X-CT-Worker-Version") != str(versions["authority"])
        ):
            raise ReleaseError("deployed_reader_or_authority_version_mismatch")
        denied, _, _ = self.core(
            "ct_project_register",
            {"agent_id": self.agent, "display_name": "Release-Denied"},
            reader=True,
        )
        if denied != 403:
            raise ReleaseError("reader_write_denial_failed")
        signed_out, _, _ = self.request(self.hub + "/api/datahub/query")
        if signed_out not in {302, 303, 401, 403}:
            raise ReleaseError("signed_out_denial_failed")
        status, catalog, _ = self.core(
            "ct_published_catalog", {"limit": 1}, reader=True
        )
        if status != 200 or not catalog.get("ok"):
            raise ReleaseError("committed_catalog_unavailable")
        status, live, headers = self.request(
            self.hub + "/api/datahub/query",
            headers=self.access,
            body={
                "protocol": "ct.datahub.v1",
                "method": "datahub.snapshot",
                "params": {},
            },
        )
        if (
            status != 200
            or not live.get("ok")
            or live["data"]["transport"]["workspace_id"] != self.workspace
            or headers.get("X-CT-Worker-Version") != str(versions["datahub"])
            or live["data"]["revision"] < catalog["data"]["published_sequence"]
        ):
            raise ReleaseError("deployed_datahub_verification_failed")
        if canary_session:
            status, detail, after_headers = self.request(
                self.hub + "/api/datahub/query",
                headers=self.access,
                body={
                    "protocol": "ct.datahub.v1",
                    "method": "session.graph",
                    "params": {"session_id": str(canary_session)},
                },
            )
            if (
                status != 200
                or not detail.get("ok")
                or detail["data"]["root_session_id"] != str(canary_session)
                or after_headers.get("X-CT-Worker-Version") != str(versions["datahub"])
            ):
                raise ReleaseError("canary_not_visible_without_redeployment")
        return {
            "reader_read": True,
            "reader_write_denied": True,
            "signed_out_denied": True,
            "access_authenticated_live_read": True,
            "worker_versions_match": True,
            "canary_visible_without_redeployment": canary_session is not None,
        }


def execute(args) -> Receipt | dict[str, Any]:
    configs = configurations(args.environment)
    commit, clean = source()
    if args.action == "plan":
        return {
            "environment": args.environment,
            "clean_source": clean,
            "workers": {name: value["name"] for name, value in configs.items()},
            "steps": STEPS,
            "deploys": False,
            "requires_staging_receipt": args.environment == "production",
        }
    path = (
        args.receipt
        or ROOT / ".artifacts/releases" / f"{args.environment}-{commit}.json"
    )
    fingerprint = source_digest()
    receipt = (
        Receipt.model_validate_json(path.read_bytes())
        if path.exists()
        else Receipt(
            commit=commit,
            environment=args.environment,
            clean_source=clean,
            source_digest=fingerprint,
        )
    )
    if receipt.commit != commit or receipt.environment != args.environment:
        raise ReleaseError("receipt_source_or_environment_mismatch")
    if receipt.source_digest != fingerprint:
        raise ReleaseError("receipt_source_changed_use_new_receipt")
    if args.action == "deploy" and (not clean or not receipt.clean_source):
        raise ReleaseError("deployment_requires_committed_clean_source")
    target = None
    try:
        receipt.failure = None
        if (
            args.action in {"check", "deploy"}
            and "qualification" not in receipt.completed
        ):
            qualify()
            if source_digest() != fingerprint or source()[0] != commit:
                raise ReleaseError("source_changed_during_qualification")
            receipt.completed.append("qualification")
            receipt.status = "qualified"
            persist(path, receipt)
        if args.action == "check":
            return receipt
        configuration = ReleaseConfiguration.model_validate_json(
            (ROOT / "cloudflare/release.json").read_bytes()
        )
        if args.environment == "production" and args.action == "deploy":
            if args.staging_receipt is None:
                raise ReleaseError("verified_staging_receipt_required")
            stage = Receipt.model_validate_json(args.staging_receipt.read_bytes())
            if (
                stage.status != "verified"
                or stage.environment != "staging"
                or stage.commit != commit
                or not stage.clean_source
                or stage.source_digest != fingerprint
                or not stage.checks.get("canary_visible_without_redeployment")
            ):
                raise ReleaseError("staging_receipt_not_qualified_for_this_commit")
        target = Connections(getattr(configuration, args.environment))
        # Production and staging credentials must never resolve to the same authority.
        other_name = "production" if args.environment == "staging" else "staging"
        other = getattr(configuration, other_name)
        try:
            other_profile = load_profile(other.collector_profile)
        except RuntimeError:
            other_profile = None
        if (
            other_profile
            and require_https(str(other_profile.cloudflare_url)) == target.origin
        ):
            raise ReleaseError("release_authorities_not_isolated")
        if args.action == "deploy":
            for component in ("authority", "datahub"):
                if component not in receipt.completed:
                    # Record an existing version when available; new staging Workers
                    # may have no previous deployment. No data restore is attempted.
                    try:
                        receipt.previous_versions[component] = active_version(
                            component, args.environment
                        )
                    except ReleaseError:
                        if args.environment == "production":
                            raise
                    wrangler(component, args.environment, "deploy")
                    receipt.versions[component] = active_version(
                        component, args.environment
                    )
                    receipt.completed.append(component)
                    persist(path, receipt)
                elif (
                    active_version(component, args.environment)
                    != receipt.versions[component]
                ):
                    raise ReleaseError("deployment_changed_since_checkpoint")
                if component == "authority" and "migration" not in receipt.completed:

                    def migrated(count):
                        receipt.migrated_records += count
                        persist(path, receipt)

                    target.migrate(migrated)
                    receipt.completed.append("migration")
                    persist(path, receipt)
            if "canary" not in receipt.completed:
                receipt.canary_session = target.canary(
                    path.with_suffix(".canary.sqlite3"), commit
                )
                receipt.completed.append("canary")
                persist(path, receipt)
        if set(receipt.versions) != {"authority", "datahub"}:
            raise ReleaseError("deployed_versions_missing_from_receipt")
        if receipt.canary_session is None:
            raise ReleaseError("canary_missing_from_receipt")
        receipt.checks = target.verify(receipt.versions, receipt.canary_session)
        if "verification" not in receipt.completed:
            receipt.completed.append("verification")
        receipt.status = "verified"
        persist(path, receipt)
        return receipt
    except (RuntimeError, OSError, ValueError, httpx.HTTPError) as error:
        receipt.status = "failed"
        receipt.failure = (
            str(error)
            if isinstance(error, ReleaseError)
            else "release_dependency_unavailable"
        )
        persist(path, receipt)
        raise ReleaseError(receipt.failure) from None
    finally:
        if target:
            target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment", choices=("staging", "production"), default="staging"
    )
    parser.add_argument(
        "--action", choices=("plan", "check", "deploy", "verify"), default="plan"
    )
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--staging-receipt", type=Path)
    args = parser.parse_args()
    try:
        result = execute(args)
        print(
            result.model_dump_json()
            if isinstance(result, Receipt)
            else json.dumps(result)
        )
        return 0
    except (ReleaseError, ValueError, OSError, RuntimeError):
        # Profile validation errors can contain input values. Only receipts retain
        # the closed release error category; CLI output never echoes configuration.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "action": args.action,
                    "error": "release_failed; inspect the bounded release receipt",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
