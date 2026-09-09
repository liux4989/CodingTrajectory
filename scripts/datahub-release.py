#!/usr/bin/env python3
"""Sequential non-production Datahub release runner with sanitized receipts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "packages/plugins/datahub"
WRANGLER = PLUGIN / "web/node_modules/.bin/wrangler"
STATE_DIR = ROOT / ".artifacts/datahub-release"
STATE_FILE = STATE_DIR / "state.json"
ALLOWED_RESPONSES = {
    "/api/datahub/snapshot": "snapshot",
    "/api/datahub/changes": "changes",
    "/api/projects": "projects",
    "/api/sessions": "sessions",
    "/api/sessions/graph": "graph_detail",
    "/api/sessions/tree": "session_tree",
    "/api/sessions/items": "session_item_details",
}


class WorkerPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway: str
    facade: str
    url: HttpUrl | None = None
    hostname: str | None = None


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: Literal["non-production"]
    supabase_project_ref: str
    serving: WorkerPair
    candidate: WorkerPair
    cloudflare_account_id_env: str
    access_team_domain_env: str
    access_audience_env: str
    facade_secret_envs: list[str]

    @field_validator("supabase_project_ref")
    @classmethod
    def _project_ref(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z]{20}", value):
            raise ValueError("invalid Supabase project ref")
        return value


def _manifest(path: Path) -> ReleaseManifest:
    manifest = ReleaseManifest.model_validate_json(path.read_text())
    if manifest.serving.gateway == manifest.candidate.gateway:
        raise SystemExit("candidate gateway must be isolated from the serving gateway")
    if manifest.serving.facade == manifest.candidate.facade:
        raise SystemExit("candidate facade must be isolated from the serving facade")
    return manifest


def _run(command: list[str], *, timeout: int = 300, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _required(names: list[str]) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise SystemExit("missing required environment variables: " + ", ".join(missing))


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"stages": {}}
    return json.loads(STATE_FILE.read_text())


def _record(stage: str, evidence: dict[str, Any]) -> None:
    state = _load_state()
    source_commit = _git("rev-parse", "HEAD")
    if state.get("source_commit") not in {None, source_commit}:
        state = {"stages": {}}
    state["source_commit"] = source_commit
    state["environment"] = "non-production"
    state.setdefault("stages", {})[stage] = {
        "completed_at": int(time.time()),
        **evidence,
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(f"{stage}: PASS (receipt: {STATE_FILE})")


def _require_stage(stage: str) -> dict[str, Any]:
    state = _load_state()
    evidence = state.get("stages", {}).get(stage)
    if not evidence or state.get("source_commit") != _git("rev-parse", "HEAD"):
        raise SystemExit(f"stage {stage!r} has not passed for the current commit")
    return evidence


def _wrangler_json(*args: str) -> Any:
    output = _run([str(WRANGLER), *args], timeout=120, capture=True)
    start = min(index for index in (output.find("["), output.find("{")) if index >= 0)
    return json.loads(output[start:])


def _versions(pair: WorkerPair) -> dict[str, Any]:
    return {
        role: _wrangler_json(
            "deployments", "status", "--name", name, "--json"
        )
        for role, name in (("gateway", pair.gateway), ("facade", pair.facade))
    }


def cmd_preflight(manifest: ReleaseManifest) -> None:
    url = os.environ.get("CT_SUPABASE_URL", "")
    if url and manifest.supabase_project_ref not in url:
        raise SystemExit("CT_SUPABASE_URL does not match the manifest project ref")
    print("manifest target: non-production")
    print(f"serving pair: {manifest.serving.gateway} -> {manifest.serving.facade}")
    print(f"candidate pair: {manifest.candidate.gateway} -> {manifest.candidate.facade}")
    names = [
        "CLOUDFLARE_API_TOKEN",
        manifest.cloudflare_account_id_env,
        manifest.access_team_domain_env,
        manifest.access_audience_env,
        *manifest.facade_secret_envs,
    ]
    missing = [name for name in names if not os.environ.get(name)]
    print("missing remote inputs: " + (", ".join(missing) if missing else "none"))


def cmd_build(manifest: ReleaseManifest, allow_dirty: bool) -> None:
    if _git("status", "--porcelain") and not allow_dirty:
        raise SystemExit("release builds require a clean checkout (or --allow-dirty)")
    _run(["bash", "scripts/prepare-datahub-remote-handoff.sh"], timeout=600)
    _run(
        ["bash", "packages/plugins/datahub/scripts/prepare-hosted-worker.sh"],
        timeout=600,
    )
    with tempfile.TemporaryDirectory(prefix="ct-datahub-dry-run-") as temp:
        for config in (
            "wrangler.facade-candidate.jsonc",
            "wrangler.gateway-candidate.jsonc",
        ):
            out = Path(temp) / config
            _run(
                [
                    str(WRANGLER),
                    "deploy",
                    "--config",
                    str(PLUGIN / config),
                    "--dry-run",
                    "--outdir",
                    str(out),
                ],
                timeout=300,
            )
    hosted_assets = b"".join(
        path.read_bytes() for path in (PLUGIN / "web/dist").rglob("*") if path.is_file()
    )
    for forbidden in (b"CT_SUPABASE", manifest.supabase_project_ref.encode()):
        if forbidden in hosted_assets:
            raise SystemExit("hosted browser assets contain forbidden Supabase configuration")
    lock_hash = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    _record("build", {"uv_lock_sha256": lock_hash, "dirty": allow_dirty})


def cmd_runtime(manifest: ReleaseManifest) -> None:
    del manifest
    _require_stage("build")
    _run(["uv", "run", "python", "scripts/qualify-datahub-worker.py"], timeout=180)
    evidence = json.loads(
        (STATE_DIR / "runtime-qualification.json").read_text()
    )
    _record("runtime", evidence)


def _cloudflare_apps(account_id: str, token: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/access/apps",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 100},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(
            "Cloudflare Access application read failed with status "
            f"{response.status_code}; the API token needs account Access read scope"
        )
    payload = response.json()
    if payload.get("success") is not True or not isinstance(payload.get("result"), list):
        raise SystemExit("Cloudflare Access application inventory was malformed")
    return payload["result"]


def _cloudflare_deployments(
    account_id: str, token: str, worker: str, *, allow_missing: bool = False
) -> Any:
    response = httpx.get(
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/workers/scripts/{worker}/deployments",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code == 404 and allow_missing:
        return {"status": "worker_absent"}
    if response.status_code != 200:
        raise SystemExit(
            f"Cloudflare deployment inventory for {worker} failed with status "
            f"{response.status_code}"
        )
    payload = response.json()
    if payload.get("success") is not True:
        raise SystemExit(f"Cloudflare deployment inventory for {worker} was malformed")
    return payload.get("result")


def _candidate_access_evidence(
    apps: list[dict[str, Any]], hostname: str | None
) -> dict[str, Any]:
    matching = [
        app
        for app in apps
        if app.get("domain") == hostname and app.get("type") == "self_hosted"
    ]
    policies = [policy for app in matching for policy in app.get("policies") or []]
    serialized = json.dumps(policies).lower()
    exact_email_allow = any(
        policy.get("decision") == "allow"
        and '"email"' in json.dumps(policy.get("include") or []).lower()
        for policy in policies
    )
    return {
        "candidate_access_application_ids": [app.get("id") for app in matching],
        "candidate_access_audiences": [app.get("aud") for app in matching],
        "candidate_access_policy_ids": [policy.get("id") for policy in policies],
        "candidate_exact_email_allow_verified": exact_email_allow,
        "candidate_bypass_absent": '"decision": "bypass"' not in serialized,
        "candidate_everyone_absent": '"everyone"' not in serialized,
    }


def cmd_reconcile(manifest: ReleaseManifest) -> None:
    _required(["CLOUDFLARE_API_TOKEN", manifest.cloudflare_account_id_env])
    account_id = os.environ[manifest.cloudflare_account_id_env]
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    apps = _cloudflare_apps(account_id, token)
    access_evidence = _candidate_access_evidence(apps, manifest.candidate.hostname)
    serving = {
        role: _cloudflare_deployments(account_id, token, name)
        for role, name in (
            ("gateway", manifest.serving.gateway),
            ("facade", manifest.serving.facade),
        )
    }
    candidate = {
        role: _cloudflare_deployments(
            account_id, token, name, allow_missing=True
        )
        for role, name in (
            ("gateway", manifest.candidate.gateway),
            ("facade", manifest.candidate.facade),
        )
    }
    _record(
        "reconcile",
        {
            "serving_deployments": serving,
            "candidate_deployments": candidate,
            **access_evidence,
        },
    )


def _reader_headers(token: str) -> dict[str, str]:
    return {
        "apikey": os.environ["CT_SUPABASE_ANON_KEY"],
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def cmd_reader(manifest: ReleaseManifest) -> None:
    _required(manifest.facade_secret_envs)
    origin = os.environ["CT_SUPABASE_URL"].rstrip("/")
    auth = httpx.post(
        origin + "/auth/v1/token?grant_type=password",
        headers={"apikey": os.environ["CT_SUPABASE_ANON_KEY"]},
        json={
            "email": os.environ["CT_READER_EMAIL"],
            "password": os.environ["CT_READER_PASSWORD"],
        },
        timeout=30,
    )
    if auth.status_code != 200 or not isinstance(auth.json().get("access_token"), str):
        raise SystemExit(f"dedicated reader authentication failed: status {auth.status_code}")
    headers = _reader_headers(auth.json()["access_token"])
    workspace_id = os.environ["CT_REMOTE_WORKSPACE_ID"]
    read = httpx.post(
        origin + "/rest/v1/rpc/ct_workspace_snapshot",
        headers=headers,
        json={"request": {"workspace_id": workspace_id}},
        timeout=30,
    )
    if read.status_code != 200 or str(read.json().get("workspace_id")) != workspace_id:
        raise SystemExit(f"intended workspace read failed: status {read.status_code}")
    foreign = httpx.post(
        origin + "/rest/v1/rpc/ct_workspace_snapshot",
        headers=headers,
        json={"request": {"workspace_id": str(uuid.uuid4())}},
        timeout=30,
    )
    if foreign.status_code == 200:
        raise SystemExit("reader unexpectedly read a workspace without membership")
    mutation = httpx.post(
        origin + "/rest/v1/rpc/ct_collector_heartbeat",
        headers=headers,
        json={
            "request": {
                "workspace_id": workspace_id,
                "agent_id": str(uuid.uuid4()),
                "agent_instance_id": str(uuid.uuid4()),
                "observation_sequence": 0,
                "lease_seconds": 60,
                "observed_at": "2000-01-01T00:00:00Z",
                "runtime_state": "qualification",
            }
        },
        timeout=30,
    )
    if mutation.status_code not in {401, 403}:
        raise SystemExit(
            "reader mutation denial was not proven: "
            f"ct_collector_heartbeat returned status {mutation.status_code}"
        )
    _record(
        "reader",
        {
            "workspace_read_status": read.status_code,
            "foreign_workspace_status": foreign.status_code,
            "mutation_denial_status": mutation.status_code,
        },
    )


def _access_covered(manifest: ReleaseManifest) -> None:
    evidence = _require_stage("reconcile")
    if not evidence.get("candidate_access_application_ids"):
        raise SystemExit(
            "candidate hostname has no verified Cloudflare Access application; refusing deployment"
        )
    audience = os.environ.get(manifest.access_audience_env)
    if audience not in evidence.get("candidate_access_audiences", []):
        raise SystemExit(
            "CF_ACCESS_AUD does not match the verified candidate Access application"
        )
    if not evidence.get("candidate_exact_email_allow_verified"):
        raise SystemExit("candidate Access policy has no verified exact-email allow rule")
    if not evidence.get("candidate_bypass_absent"):
        raise SystemExit("candidate Access policy contains a bypass rule")
    if not evidence.get("candidate_everyone_absent"):
        raise SystemExit("candidate Access policy contains an Everyone selector")


def _deploy(config: str, secret_names: list[str]) -> None:
    values = {name: os.environ[name] for name in secret_names}
    with tempfile.NamedTemporaryFile("w", prefix="ct-secrets-", suffix=".json") as file:
        os.chmod(file.name, 0o600)
        json.dump(values, file)
        file.flush()
        _run(
            [
                str(WRANGLER),
                "deploy",
                "--config",
                str(PLUGIN / config),
                "--secrets-file",
                file.name,
                "--strict",
            ],
            timeout=300,
        )


def cmd_deploy_candidate(manifest: ReleaseManifest) -> None:
    if _require_stage("build").get("dirty"):
        raise SystemExit("candidate deployment requires a clean-checkout build receipt")
    _require_stage("runtime")
    _require_stage("reader")
    _required(
        [
            "CLOUDFLARE_API_TOKEN",
            manifest.cloudflare_account_id_env,
            manifest.access_team_domain_env,
            manifest.access_audience_env,
            *manifest.facade_secret_envs,
        ]
    )
    _access_covered(manifest)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "mutation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _deploy("wrangler.facade-candidate.jsonc", manifest.facade_secret_envs)
        _deploy(
            "wrangler.gateway-candidate.jsonc",
            [manifest.access_team_domain_env, manifest.access_audience_env],
        )
    _record("candidate_deploy", {"deployments": _versions(manifest.candidate)})


def _candidate_headers() -> dict[str, str]:
    _required(["DATAHUB_ACCESS_COOKIE"])
    return {"Cookie": "CF_Authorization=" + os.environ["DATAHUB_ACCESS_COOKIE"]}


def _validate_routes(base_url: str) -> dict[str, Any]:
    from datahub_plugin.api_models import validate_api_response

    signed_out = httpx.get(base_url + "/api/datahub/snapshot", follow_redirects=False)
    if signed_out.status_code not in {302, 401, 403}:
        raise SystemExit(f"signed-out denial failed: status {signed_out.status_code}")
    forged = httpx.get(
        base_url + "/api/datahub/snapshot",
        headers={"Cf-Access-Jwt-Assertion": "forged"},
        follow_redirects=False,
    )
    if forged.status_code not in {302, 401, 403}:
        raise SystemExit(f"forged assertion denial failed: status {forged.status_code}")

    headers = _candidate_headers()
    snapshot, first_request_ms = _timed_json_get(
        base_url, "/api/datahub/snapshot", headers
    )
    validate_api_response("snapshot", snapshot)
    repeated, steady_request_ms = _timed_json_get(
        base_url, "/api/datahub/snapshot", headers
    )
    validate_api_response("snapshot", repeated)
    if repeated["revision"] != snapshot["revision"]:
        raise SystemExit("candidate snapshot changed between qualification requests")
    revision = snapshot["revision"]
    projects = _json_get(base_url, "/api/projects?limit=2", headers)
    validate_api_response("projects", projects)
    sessions = _json_get(base_url, "/api/sessions?since_days=7&limit=2", headers)
    validate_api_response("sessions", sessions)
    if not sessions.get("items"):
        raise SystemExit("candidate Sessions route has no representative data")
    session_id = sessions["items"][0]["root_session_id"]
    paths = {
        "/api/datahub/changes": f"/api/datahub/changes?after_revision={revision}",
        "/api/sessions/graph": f"/api/sessions/graph?session_id={session_id}",
        "/api/sessions/tree": f"/api/sessions/tree?session_id={session_id}",
    }
    responses: dict[str, Any] = {
        "/api/datahub/snapshot": snapshot,
        "/api/projects": projects,
        "/api/sessions": sessions,
    }
    for route, path in paths.items():
        responses[route] = _json_get(base_url, path, headers)
        validate_api_response(ALLOWED_RESPONSES[route], responses[route])

    # The Tree contract does not expose item IDs. A random UUID proves the
    # metadata-only route and response schema without requesting content.
    items_path = "/api/sessions/items?item_ids=" + str(uuid.uuid4())
    items = _json_get(base_url, items_path, headers)
    validate_api_response("session_item_details", items)
    responses["/api/sessions/items"] = items

    for method, path in (
        ("GET", "/api/sessions/context-window"),
        ("POST", "/api/refresh"),
        ("GET", "/api/not-a-route"),
        ("GET", "/api/sessions?since_days=8"),
    ):
        response = httpx.request(
            method, base_url + path, headers=headers, follow_redirects=False
        )
        if response.status_code not in {400, 404}:
            raise SystemExit(f"prohibited/invalid route {path} returned {response.status_code}")
        if "application/json" not in response.headers.get("content-type", ""):
            raise SystemExit(f"API route {path} returned non-JSON content")
    deep = httpx.get(base_url + "/sessions", headers=headers, follow_redirects=False)
    if deep.status_code != 200 or "text/html" not in deep.headers.get("content-type", ""):
        raise SystemExit("deep-link SPA navigation failed")
    return {
        "signed_out_status": signed_out.status_code,
        "forged_status": forged.status_code,
        "revision": revision,
        "session_id": session_id,
        "first_request_ms": first_request_ms,
        "steady_request_ms": steady_request_ms,
        "routes": sorted(responses),
    }


def _json_get(base_url: str, path: str, headers: dict[str, str]) -> Any:
    payload, _ = _timed_json_get(base_url, path, headers)
    return payload


def _timed_json_get(
    base_url: str, path: str, headers: dict[str, str]
) -> tuple[Any, float]:
    started = time.monotonic()
    response = httpx.get(base_url + path, headers=headers, timeout=60)
    if response.status_code != 200:
        raise SystemExit(f"candidate route {path} failed with status {response.status_code}")
    if "application/json" not in response.headers.get("content-type", ""):
        raise SystemExit(f"candidate route {path} returned non-JSON content")
    return response.json(), round((time.monotonic() - started) * 1000, 1)


def cmd_validate_candidate(manifest: ReleaseManifest) -> None:
    _require_stage("candidate_deploy")
    _record(
        "candidate_validate",
        _validate_routes("https://" + str(manifest.candidate.hostname)),
    )


def cmd_promote(manifest: ReleaseManifest) -> None:
    _require_stage("candidate_validate")
    _required(
        [
            "CLOUDFLARE_API_TOKEN",
            manifest.cloudflare_account_id_env,
            manifest.access_team_domain_env,
            manifest.access_audience_env,
        ]
    )
    rollback = _versions(manifest.serving)
    rollback_gateway_version = _active_version(rollback["gateway"])
    if rollback_gateway_version is None:
        raise SystemExit("serving gateway has no unambiguous active rollback version")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "mutation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _deploy(
            "wrangler.gateway-promote.jsonc",
            [manifest.access_team_domain_env, manifest.access_audience_env],
        )
        try:
            validation = _validate_routes(str(manifest.serving.url).rstrip("/"))
        except BaseException:
            _run(
                [
                    str(WRANGLER),
                    "rollback",
                    rollback_gateway_version,
                    "--name",
                    manifest.serving.gateway,
                    "--message",
                    "automatic rollback after Datahub post-promotion validation failure",
                    "--yes",
                ],
                timeout=180,
            )
            raise
    _record(
        "promote",
        {
            "rollback_pair": rollback,
            "promoted_pair": _versions(
                WorkerPair(
                    gateway=manifest.serving.gateway,
                    facade=manifest.candidate.facade,
                )
            ),
            "validation": validation,
        },
    )


def _active_version(deployments: Any) -> str | None:
    if not isinstance(deployments, dict):
        return None
    versions = deployments.get("versions")
    if not isinstance(versions, list):
        return None
    active = [
        item.get("version_id")
        for item in versions
        if isinstance(item, dict)
        and item.get("percentage") == 100
        and isinstance(item.get("version_id"), str)
    ]
    return active[0] if len(active) == 1 else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=(
            "preflight",
            "build",
            "runtime",
            "reconcile",
            "reader",
            "deploy-candidate",
            "validate-candidate",
            "promote",
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PLUGIN / "release/non-production.json",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    manifest = _manifest(args.manifest)
    commands = {
        "preflight": lambda: cmd_preflight(manifest),
        "build": lambda: cmd_build(manifest, args.allow_dirty),
        "runtime": lambda: cmd_runtime(manifest),
        "reconcile": lambda: cmd_reconcile(manifest),
        "reader": lambda: cmd_reader(manifest),
        "deploy-candidate": lambda: cmd_deploy_candidate(manifest),
        "validate-candidate": lambda: cmd_validate_candidate(manifest),
        "promote": lambda: cmd_promote(manifest),
    }
    commands[args.stage]()


if __name__ == "__main__":
    main()
