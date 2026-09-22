"""Tested release promotion, independent of data sync and destructive resets. Local preparation is resumable; deployment is not retried."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from coding_trajectory.contracts.prepared_api import REMOTE_API_METHODS
from coding_trajectory.contracts.registry import SERVICE_CONTRACTS
from coding_trajectory.control_plane.artifact_protocol import ArtifactManifest
from coding_trajectory.control_plane.collector import CloudflareCollectorRemote
from coding_trajectory.control_plane.connections import load_profile_credentials
from coding_trajectory.control_plane.publication_run import (
    digest,
    encode,
    events,
    record,
    save,
)
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/control-plane"
WRANGLER = WORKER / "node_modules/.bin/wrangler"
INPUTS = (
    "uv.lock",
    "cloudflare/control-plane/package-lock.json",
    "cloudflare/control-plane/wrangler.jsonc",
    "cloudflare/control-plane/pyproject.toml",
    "cloudflare/control-plane/uv.lock",
    "cloudflare/control-plane/pylock.toml",
)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2, 3] = 3
    source: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str
    environment: Literal["staging"] | None = (
        None  # Legacy plans pinned preparation to staging.
    )
    inputs: dict[str, str]
    tools: dict[str, str]


def output(command, *, cwd=ROOT):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def source(source_sha):
    if output(["git", "rev-parse", "HEAD"]) != source_sha or output(
        ["git", "status", "--porcelain", "--untracked-files=no"]
    ):
        raise ValueError("release requires the exact clean reviewed checkout")
    if output(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "cloudflare/control-plane",
        ]
    ):
        raise ValueError("release requires committed Worker sources and lockfiles")
    return output(["git", "rev-parse", "HEAD^{tree}"])


def tools():
    return {
        "python": platform.python_version(),
        "node": output(["node", "--version"]),
        "uv": output(["uv", "--version"]),
        "wrangler": output([str(WRANGLER), "--version"], cwd=WORKER),
        "python_worker": output(
            ["uv", "run", "--frozen", "python", "--version"], cwd=WORKER
        ),
        "pywrangler": output(
            ["uv", "run", "--frozen", "pywrangler", "--version"], cwd=WORKER
        ),
    }


def hashes(paths, base):
    return {
        str(path.relative_to(base)): digest(path.read_bytes()) for path in sorted(paths)
    }


def check_hashes(values, base):
    for name, expected in values.items():
        path = (base / name).resolve()
        if (
            not path.is_relative_to(base.resolve())
            or digest(path.read_bytes()) != expected
        ):
            raise ValueError("cached release evidence changed")


def compatible(response):
    """Conservative descriptor gate, not a replacement for bounded readback."""
    versions = {name: SERVICE_CONTRACTS[name].version for name in REMOTE_API_METHODS}
    manifests = [
        ArtifactManifest.model_validate(value) for value in response["manifests"]
    ]
    count = 0
    for manifest in manifests:
        if str(manifest.workspace_id) != response["workspace_id"]:
            raise ValueError("manifest workspace mismatch")
        for graph in manifest.graphs:
            if not graph.api_methods:
                raise ValueError("graph has no prepared methods")
            for method in graph.api_methods:
                if (
                    method.error
                    or method.index is None
                    or method.method_version != versions.get(method.method)
                ):
                    raise ValueError(
                        "incompatible live prepared data; resolve the data/version mismatch before activation"
                    )
                count += 1
    return {
        "projects": len(manifests),
        "graphs": sum(len(m.graphs) for m in manifests),
        "methods": count,
    }


class Release:
    def __init__(self, directory, environment="staging"):
        if environment not in {"staging", "production"}:
            raise ValueError("unsupported target environment")
        self.environment = environment
        self.directory = Path(directory).expanduser().resolve()

    def target_events(self):
        return [
            r
            for r in events(self.directory)
            if r.get("environment", "staging") == self.environment
        ]

    def env_args(self):
        return ["--env", "staging"] if self.environment == "staging" else []

    def receipt_name(self):
        return (
            "receipt.json"
            if self.environment == "staging"
            else "receipt-production.json"
        )

    def __enter__(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise ValueError("release directory must have mode 0700")
        self.lock = (self.directory / ".lock").open("ab")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError("release job already active") from None
        return self

    def __exit__(self, *_):
        self.lock.close()

    def stopped(self):
        if (self.directory / "STOP").exists():
            raise ValueError(
                "stop requested; active command drained; use resume for local preparation"
            )

    def put(self, name, value):
        body = encode(value)
        save(self.directory, name, body)
        record(self.directory, "sealed", file=name, sha256=digest(body))

    def read(self, name):
        body = (self.directory / name).read_bytes()
        if not any(
            r.get("file") == name and r.get("sha256") == digest(body)
            for r in events(self.directory)
        ):
            raise ValueError("unsealed or changed release evidence")
        return json.loads(body)

    def load(self):
        plan = Plan.model_validate(self.read("plan.json"))
        if source(plan.source) != plan.tree or tools() != plan.tools:
            raise ValueError("source/toolchain differs from release plan")
        check_hashes(plan.inputs, ROOT)
        return plan

    def phase(self, name, command, *, cwd=ROOT, outputs=(), bundle_dir=None):
        self.stopped()
        receipt = self.directory / f"{name}.json"
        if receipt.exists():
            value = self.read(receipt.name)
            check_hashes(value["outputs"], self.directory)
            return value
        attempt = uuid4().hex
        log = self.directory / f"{name}-{attempt}.log"
        start = time.monotonic()
        record(self.directory, "phase_started", phase=name, attempt=attempt)
        with log.open("xb") as stream:
            # A stop marker drains this child. Never signal a possibly committed write.
            result = subprocess.run(
                command,
                cwd=cwd,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                check=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        elapsed = round(time.monotonic() - start, 3)
        record(
            self.directory,
            "phase_finished",
            phase=name,
            attempt=attempt,
            seconds=elapsed,
            exit_code=result.returncode,
        )
        if result.returncode:
            raise ValueError(f"{name} failed; inspect private phase log")
        files = [log, *outputs]
        if bundle_dir:
            files.extend(p for p in bundle_dir.rglob("*") if p.is_file())
        value = {"seconds": elapsed, "outputs": hashes(files, self.directory)}
        if bundle_dir:
            value["entry"] = str(
                (bundle_dir / "src/index.py").relative_to(self.directory)
            )
            value["config"] = str(
                (bundle_dir / "wrangler.jsonc").relative_to(self.directory)
            )
        self.put(receipt.name, value)
        return value

    def prepare(self, sha):
        if not (self.directory / "plan.json").exists():
            plan = Plan(
                source=sha,
                tree=source(sha),
                inputs=hashes([ROOT / p for p in INPUTS], ROOT),
                tools=tools(),
            )
            self.put("plan.json", plan.model_dump())
            record(
                self.directory,
                "planned",
                plan_sha256=digest((self.directory / "plan.json").read_bytes()),
            )
        plan = self.load()
        if sha != plan.source:
            raise ValueError("source argument differs from plan")
        fixture = self.directory / "fixture-index-heavy.json"
        publication = self.directory / "synthetic-publication.json"
        steps = [
            (
                "release",
                ["uv", "run", "python", "scripts/qualify-deploy-release.py"],
                (),
            ),
            ("contracts", ["npm", "--prefix", str(WORKER), "run", "check"], ()),
            (
                "metrics",
                ["uv", "run", "python", "scripts/validate-metrics-baselines.py"],
                (),
            ),
            (
                "collector",
                ["uv", "run", "python", "scripts/qualify-collector-preparation.py"],
                (),
            ),
            (
                "connections",
                ["uv", "run", "python", "scripts/qualify-connection-workflows.py"],
                (),
            ),
            (
                "prepared",
                ["uv", "run", "python", "scripts/qualify-prepared-api.py"],
                (),
            ),
            (
                "index-heavy",
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/qualify-prepared-api.py",
                    "--shape",
                    "index-heavy",
                    "--fixture-output",
                    str(fixture),
                ],
                (fixture,),
            ),
            (
                "worker-publication",
                [
                    "node",
                    "scripts/qualify-prepared-api.mjs",
                    str(fixture),
                    "--publication",
                    str(publication),
                ],
                (publication,),
            ),
        ]
        for name, command, outputs in steps:
            self.phase(name, command, outputs=outputs)
        self.load()
        if not (self.directory / "build.json").exists():
            bundle_dir = self.directory / f"worker-{uuid4().hex}"
            self.phase(
                "build",
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/build-python-worker.py",
                    "--outdir",
                    str(bundle_dir),
                ],
                cwd=ROOT,
                bundle_dir=bundle_dir,
            )
        self.verify()
        return self.status()

    def verify(self):
        self.load()
        for phase in (
            "release",
            "contracts",
            "metrics",
            "collector",
            "connections",
            "prepared",
            "index-heavy",
            "worker-publication",
            "build",
        ):
            check_hashes(self.read(f"{phase}.json")["outputs"], self.directory)
        build = self.read("build.json")
        bundle = {
            "entry": build["entry"],
            "config": build["config"],
            "files": build["outputs"],
        }
        if any(bundle[key] not in bundle["files"] for key in ("entry", "config")):
            raise ValueError("Python bundle entry/config is not sealed")
        bundle_dir = (self.directory / bundle["config"]).parent
        actual = hashes(
            (
                p
                for p in bundle_dir.rglob("*")
                if p.is_file() and ".wrangler" not in p.relative_to(bundle_dir).parts
            ),
            self.directory,
        )
        expected = {
            name: sha
            for name, sha in bundle["files"].items()
            if (self.directory / name).is_relative_to(bundle_dir)
        }
        if actual != expected:
            raise ValueError("Python bundle module inventory changed")
        return bundle

    def remote_status(self):
        value = json.loads(
            output(
                [str(WRANGLER), "deployments", "status", *self.env_args(), "--json"],
                cwd=WORKER,
            )
        )
        versions = value["versions"]
        if len(versions) != 1 or versions[0]["percentage"] != 100:
            raise ValueError("only single-version activation is supported")
        version = json.loads(
            output(
                [
                    str(WRANGLER),
                    "versions",
                    "view",
                    versions[0]["version_id"],
                    *self.env_args(),
                    "--json",
                ],
                cwd=WORKER,
            )
        )
        bindings = version["resources"]["bindings"]
        names = {b["name"]: b for b in bindings}
        expected_bucket = "coding-trajectory-artifacts" + (
            "-staging" if self.environment == "staging" else ""
        )
        if (
            names.get("ARTIFACTS", {}).get("bucket_name") != expected_bucket
            or names.get("WORKSPACES", {}).get("class_name") != "Workspace"
            or any(
                names.get(name, {}).get("type") != "secret_text"
                for name in ("CT_PRINCIPALS", "CT_CURSOR_KEY")
            )
            or any(
                name.startswith(("CT_MAINTENANCE", "CT_REPLACEMENT")) for name in names
            )
        ):
            raise ValueError(
                "target bindings, required credentials, or maintenance state differ"
            )
        return {
            "deployment": str(UUID(value["id"])),
            "worker_version": str(UUID(versions[0]["version_id"])),
            "release_marker": {
                key: version.get("annotations", {}).get(key)
                for key in ("workers/message", "workers/tag")
            },
            "bindings_sha256": digest(
                encode(sorted(bindings, key=lambda b: b["name"]))
            ),
        }

    def live(self, profiles, version):
        result = []
        for name in profiles:
            credentials = load_profile_credentials(name)
            profile = credentials.profile
            with closing(
                CloudflareCollectorRemote(
                    url=str(profile.cloudflare_url),
                    access_token=credentials.access_token,
                )
            ) as remote:

                def check_version(response):
                    if response.headers.get("X-CT-Worker-Version") != version:
                        raise ValueError(
                            "reader endpoint differs from pinned target version"
                        )

                remote._client.event_hooks["response"].append(check_version)
                data = remote._rpc(
                    "ct_artifact_manifest", {"workspace_id": str(profile.workspace_id)}
                )
            if data["workspace_id"] != str(profile.workspace_id):
                raise ValueError("reader workspace mismatch")
            result.append(
                {
                    "profile": name,
                    "workspace": str(profile.workspace_id),
                    "endpoint": str(profile.cloudflare_url),
                    "manifest_sha256": digest(encode(data)),
                    **compatible(data),
                }
            )
        if not result or len({r["workspace"] for r in result}) != len(result):
            raise ValueError("provide one reader profile per affected workspace")
        return result

    def preflight(self, profiles):
        bundle = self.verify()
        self.stopped()
        before = self.remote_status()
        live = self.live(profiles, before["worker_version"])
        plan = {
            "environment": self.environment,
            "source_plan_sha256": digest(encode(self.read("plan.json"))),
            "release_sha256": digest(encode(bundle)),
            "before": before,
            "workspaces": live,
        }
        sha = digest(encode(plan))
        name = f"activation-{sha}.json"
        if not (self.directory / name).exists():
            self.put(name, plan)
        return {"activation_sha256": sha, "remote_writes": 0, "workspaces": len(live)}

    def deploy(self, approved=None, *, profiles=()):
        if approved is None and not profiles:
            raise ValueError("deploy requires reader profiles or an approved preflight")
        bundle = self.verify()
        if any(r["event"] == "deployment_started" for r in self.target_events()):
            raise ValueError("deployment was already invoked; reconcile, never replay")
        if approved is not None and profiles:
            raise ValueError(
                "choose reader profiles or an approved preflight, not both"
            )
        fresh = approved is None
        if fresh:
            approved = self.preflight(profiles)["activation_sha256"]
        if len(approved) != 64 or any(c not in "0123456789abcdef" for c in approved):
            raise ValueError("invalid approval digest")
        plan = self.read(f"activation-{approved}.json")
        if digest(encode(plan)) != approved or (
            not fresh
            and self.preflight([r["profile"] for r in plan["workspaces"]])[
                "activation_sha256"
            ]
            != approved
        ):
            raise ValueError("activation evidence changed; obtain a fresh approval")
        self.stopped()
        record(
            self.directory,
            "deployment_started",
            environment=self.environment,
            activation_sha256=approved,
        )
        # Persist intent BEFORE the first write. Even spawn failure is reconciled, not retried.
        self.phase(
            "deployment" if self.environment == "staging" else "deployment-production",
            [
                str(WRANGLER),
                "deploy",
                "--config",
                str(self.directory / bundle["config"]),
                *self.env_args(),
                "--strict",
                "--tag",
                approved[:16],
                "--message",
                f"ct-release:{approved}",
            ],
            cwd=(self.directory / bundle["config"]).parent,
        )
        receipt = self.reconcile()
        if receipt["state"] != "code_active":
            return receipt
        try:
            return self.smoke(receipt)
        except Exception as error:
            raise ValueError(
                "code is active but smoke validation failed; rerun smoke, not deploy"
            ) from error

    def reconcile(self):
        self.verify()
        started = [
            r for r in self.target_events() if r["event"] == "deployment_started"
        ]
        if not started:
            raise ValueError("no deployment intent to reconcile")
        approved = started[0]["activation_sha256"]
        current = self.remote_status()
        marker = current["release_marker"]
        matched = (
            marker.get("workers/message") == f"ct-release:{approved}"
            and marker.get("workers/tag") == approved[:16]
        )
        receipt = {
            "state": "code_active" if matched else "unknown",
            "activation_sha256": approved,
            **current,
            "remote_writes": 0,
            "deployment_attempts": 1,
            "publication_attempts": 0,
            "phase_seconds": {
                r["phase"]: r["seconds"]
                for r in events(self.directory)
                if r["event"] == "phase_finished"
            },
            "readback": "not performed; application qualification remains separate",
        }
        name = self.receipt_name() if matched else f"reconciliation-{uuid4().hex}.json"
        if not (self.directory / name).exists():
            self.put(name, receipt)
        return receipt

    def smoke(self, receipt=None):
        receipt = receipt if receipt is not None else self.reconcile()
        if receipt["state"] != "code_active":
            raise ValueError("cannot smoke-check an unconfirmed activation")
        plan = self.read(f"activation-{receipt['activation_sha256']}.json")
        if receipt.get("bindings_sha256") != plan["before"].get("bindings_sha256"):
            raise ValueError("activation changed target bindings")
        checks = []
        for target in plan["workspaces"]:
            credentials = load_profile_credentials(target["profile"])
            with closing(
                CloudflareCollectorRemote(
                    url=str(credentials.profile.cloudflare_url),
                    access_token=credentials.access_token,
                )
            ) as remote:

                def pin(response):
                    if (
                        response.headers.get("X-CT-Worker-Version")
                        != receipt["worker_version"]
                    ):
                        raise ValueError("smoke target version changed")

                remote._client.event_hooks["response"].append(pin)
                status = remote._rpc(
                    "ct_connection_status", {"workspace_id": target["workspace"]}
                )
                if status["workspace_id"] != target["workspace"] or not (
                    {"read", "owner"} & set(status["roles"])
                ):
                    raise ValueError("smoke reader identity differs")
                manifests = remote._rpc(
                    "ct_artifact_manifest", {"workspace_id": target["workspace"]}
                )
                if manifests.get("workspace_id") != target["workspace"]:
                    raise ValueError("smoke manifest workspace differs")
                compatible(manifests)
                if not manifests["manifests"]:
                    checks.append(
                        {
                            "workspace": target["workspace"],
                            "method": "ct_artifact_manifest",
                            "status": 200,
                            "empty_workspace": True,
                        }
                    )
                    continue
                project = manifests["manifests"][0]["project_id"]
                response = remote._client.post(
                    str(credentials.profile.cloudflare_url).rstrip("/") + "/v1/api",
                    json={
                        "protocol": "ct.api.v1",
                        "method": "project.sessions",
                        "method_version": SERVICE_CONTRACTS["project.sessions"].version,
                        "params": {"project_id": project, "limit": 1},
                    },
                )
                response.raise_for_status()
                value = response.json()
                if (
                    not value.get("ok")
                    or value.get("availability", {}).get("state") != "complete"
                ):
                    raise ValueError("smoke read unavailable")
                SERVICE_CONTRACTS["project.sessions"].validate_response(value["data"])
                checks.append(
                    {
                        "workspace": target["workspace"],
                        "method": "project.sessions",
                        "status": 200,
                    }
                )
        result = {
            "state": "smoke_passed",
            "environment": self.environment,
            "worker_version": receipt["worker_version"],
            "checks": checks,
            "remote_writes": 0,
        }
        name = f"smoke-{receipt['activation_sha256']}.json"
        if not (self.directory / name).exists():
            self.put(name, result)
        return result

    def status(self):
        rows = self.target_events()
        if (self.directory / self.receipt_name()).exists():
            receipt = self.read(self.receipt_name())
            smoke_name = f"smoke-{receipt['activation_sha256']}.json"
            state = (
                self.read(smoke_name)["state"]
                if (self.directory / smoke_name).exists()
                else receipt["state"]
            )
        elif any(r["event"] == "deployment_started" for r in rows):
            state = "deployment_outcome_unknown"
        elif (self.directory / "build.json").exists():
            state = "prepared"
        else:
            state = "preparing" if rows else "unplanned"
        return {
            "state": state,
            "stop_requested": (self.directory / "STOP").exists(),
            "completed_phases": [
                r["phase"]
                for r in rows
                if r["event"] == "phase_finished" and r["exit_code"] == 0
            ],
            "receipt_available": (self.directory / self.receipt_name()).exists(),
            "note": "Local evidence only; use reconcile for remote outcome. No automatic deploy retry.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "resume",
            "verify",
            "status",
            "stop",
            "preflight",
            "deploy",
            "reconcile",
            "smoke",
        ),
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Durable private directory outside disposable worktrees",
    )
    parser.add_argument("--source-sha")
    parser.add_argument(
        "--environment",
        choices=("staging", "production"),
        help="Explicit activation target; preparation is shared",
    )
    parser.add_argument(
        "--reader-profile",
        action="append",
        default=[],
        help="One per affected workspace; publishers must be paused",
    )
    parser.add_argument(
        "--approve-activation",
        help="Explicitly approved digest from read-only preflight",
    )
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.update(UV_FROZEN="true", WRANGLER_SEND_METRICS="false", CI="true")
    if (
        args.action in {"preflight", "deploy", "reconcile", "smoke"}
        and not args.environment
    ):
        raise ValueError("--environment is required for remote operations")
    job = Release(args.run_dir, args.environment or "staging")
    if args.action == "status":
        result = job.status()
    elif args.action == "stop":
        # No run lock: the active owner sees this after its current child drains.
        if not job.directory.is_dir():
            raise ValueError("release directory does not exist")
        if not (job.directory / "STOP").exists():
            save(job.directory, "STOP", b"stop requested\n")
        result = {
            "stop_requested": True,
            "note": "Wait for owner exit; an in-flight deployment may commit. Reconcile before further action.",
        }
    else:
        with job:
            signal.signal(
                signal.SIGTERM, lambda *_: (job.directory / "STOP").touch(mode=0o600)
            )
            signal.signal(
                signal.SIGINT, lambda *_: (job.directory / "STOP").touch(mode=0o600)
            )
            if args.action in ("prepare", "resume"):
                if not args.source_sha:
                    raise ValueError("--source-sha is required")
                if args.action == "resume":
                    job.load()
                    (job.directory / "STOP").unlink(missing_ok=True)
                result = job.prepare(args.source_sha)
            elif args.action == "verify":
                result = {"bundle_sha256": digest(encode(job.verify()))}
            elif args.action == "preflight":
                result = job.preflight(args.reader_profile)
            elif args.action == "deploy":
                result = job.deploy(
                    args.approve_activation, profiles=args.reader_profile
                )
            elif args.action == "smoke":
                result = job.smoke()
            else:
                result = job.reconcile()
    print(json.dumps(result, indent=2))
    if result.get("state") == "unknown":
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"release stopped: {exc}", file=sys.stderr)
        sys.exit(1)
