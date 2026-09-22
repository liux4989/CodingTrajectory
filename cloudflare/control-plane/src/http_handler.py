"""HTTP entrypoint for the CodingTrajectory Cloudflare control plane."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

from artifacts import artifact_key
from coding_trajectory.control_plane.artifact_transport import (
    BATCH_CONTENT_TYPE,
    BATCH_WIRE_BYTES,
    decode_batch,
)
from js import Object
from prepared_api import publication_index, read_cursor, serve_prepared, validate_api
from pyodide.ffi import to_js
from shared import (
    Fault,
    authority_failure,
    bounded,
    bytes_from_buffer,
    digest,
    fields,
    js_get,
    object_value,
    require_that,
    text,
    uuid,
)
from workers import Response

Json = dict[str, Any]
COLLECT = {
    "ct_project_register",
    "ct_collector_register_source",
    "ct_collector_recover",
    "ct_collector_publish_observation",
    "ct_collector_publish_artifacts",
    "ct_collector_artifact_readiness",
    "ct_collector_heartbeat",
    "ct_collector_publish_living_observation",
}
READ = {
    "ct_workspace_snapshot",
    "ct_legacy_fact_cleanup_status",
    "ct_artifact_manifest",
    "ct_artifact_read",
    "ct_project_inventory_snapshot",
    "ct_remote_living",
}
PROTOCOL = "ct.core.v1"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def _json_loads(raw: bytes | str) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, parse_constant=reject_constant)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _js_options(value: Json) -> Any:
    return to_js(value, dict_converter=Object.fromEntries)


def _response_headers(env: Any) -> dict[str, str]:
    result = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    try:
        version = env.WORKER_VERSION
        version_id = js_get(version, "id")
        if version_id:
            result["X-CT-Worker-Version"] = str(version_id)
    except (AttributeError, TypeError):
        pass
    return result


def _json_response(value: Any, env: Any, status: int = 200) -> Response:
    return Response.from_json(value, status=status, headers=_response_headers(env))


def _response_error_code(body: Json) -> str:
    error = body.get("error")
    return (
        error["code"]
        if isinstance(error, dict) and isinstance(error.get("code"), str)
        else "method_failed"
    )


async def authenticate(request: Any, env: Any) -> Json:
    authorization = request.headers.get("authorization")
    token_match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{32,256})", authorization or "")
    require_that(token_match, "authentication_required", 401)
    token = token_match.group(1)
    try:
        registry = object_value(_json_loads(env.CT_PRINCIPALS))
    except Exception as error:
        if isinstance(error, Fault):
            raise
        raise Fault(503, "authentication_unavailable") from error
    raw = registry.get(await digest(token))
    require_that(raw, "authentication_required", 401)
    principal = {
        "workspace_id": uuid(raw.get("workspace_id")),
        "agent_id": uuid(raw.get("agent_id")),
        "roles": raw.get("roles"),
    }
    require_that(
        isinstance(principal["roles"], list)
        and all(role in {"read", "collect", "owner"} for role in principal["roles"]),
        "invalid_principal",
        503,
    )
    return principal


def error_response(
    error: Exception,
    env: Any,
    *,
    protocol: str = PROTOCOL,
    request_id: Any = None,
    method: Any = None,
    method_version: Any = None,
) -> Response:
    failure = authority_failure(error, "worker")
    code = failure.code
    return _json_response(
        {
            "protocol": protocol,
            "id": request_id,
            "method": method,
            "method_version": method_version,
            "ok": False,
            "data": None,
            "availability": {
                "state": "unsupported"
                if code == "unsupported_version"
                else "unavailable",
                "missing": [{"field": "$", "reason": code}],
            },
            "error": {"code": code, "message": code},
            "meta": None,
        },
        env,
        status=failure.status,
    )


class HttpHandler:
    def __init__(self, env: Any) -> None:
        self.env = env

    async def upload_object(
        self, principal: Json, kind: str, sha256: str, body: bytes
    ) -> Json:
        require_that(await digest(body) == sha256, "artifact_digest_mismatch")
        try:
            value = object_value(_json_loads(body))
        except Exception as error:
            if isinstance(error, Fault):
                raise
            raise Fault(400, "invalid_artifact_json") from error
        require_that(
            value.get("schema_version") == "ct.published_facts.v2"
            if kind == "facts"
            else value.get("schema_version") == "ct.prepared-summary.v2"
            if kind == "summary"
            else value.get("schema_version") == "ct.prepared-api.v1"
            and len(body) <= 448 * 1024,
            "artifact_schema_mismatch",
        )
        index = publication_index(value, len(body)) if kind == "api" else None
        key = artifact_key(principal["workspace_id"], kind, sha256)
        workspace = self.env.WORKSPACES.getByName(principal["workspace_id"])
        claim = object_value(
            _json_loads(
                await workspace.invoke(
                    "ct_internal_artifact_claim",
                    _json_dumps(
                        {
                            "request": {
                                "workspace_id": principal["workspace_id"],
                                "kind": kind,
                                "sha256": sha256,
                            }
                        }
                    ),
                    _json_dumps(principal),
                )
            )
        )
        require_that(claim.get("status") == 200, "artifact_claim_failed", 503)
        prior = await self.env.ARTIFACTS.get(key)
        if prior:
            metadata = js_get(prior, "customMetadata")
            require_that(
                js_get(prior, "size") == len(body)
                and js_get(metadata, "sha256") == sha256
                and js_get(metadata, "workspace_id") == principal["workspace_id"]
                and js_get(metadata, "kind") == kind,
                "artifact_identity_conflict",
                409,
            )
            require_that(
                await digest(bytes_from_buffer(await prior.arrayBuffer())) == sha256,
                "artifact_identity_conflict",
                409,
            )
        else:
            await self.env.ARTIFACTS.put(
                key,
                body,
                _js_options(
                    {
                        "customMetadata": {
                            "workspace_id": principal["workspace_id"],
                            "kind": kind,
                            "sha256": sha256,
                        },
                        "httpMetadata": {"contentType": "application/json"},
                    }
                ),
            )
        completion = object_value(
            _json_loads(
                await workspace.invoke(
                    "ct_internal_artifact_complete",
                    _json_dumps(
                        {
                            "request": {
                                "workspace_id": principal["workspace_id"],
                                "kind": kind,
                                "sha256": sha256,
                                "bytes": len(body),
                                "index": index,
                                "token": claim["body"]["token"],
                            }
                        }
                    ),
                    _json_dumps(principal),
                )
            )
        )
        require_that(
            completion.get("status") == 200,
            _response_error_code(completion.get("body", {}))
            if completion.get("body")
            else "artifact_completion_failed",
            completion.get("status", 503),
        )
        result: Json = {"ok": True, "sha256": sha256, "bytes": len(body)}
        if claim.get("body", {}).get("__benchmark"):
            result["__benchmark"] = claim["body"]["__benchmark"]
        if completion.get("body", {}).get("__benchmark"):
            result["__benchmark_completion"] = completion["body"]["__benchmark"]
        return result

    async def fetch(self, request: Any) -> Response:
        request_id: Any = None
        method: Any = None
        parsed_url = urlsplit(request.url)
        protocol = "ct.api.v1" if parsed_url.path == "/v1/api" else PROTOCOL
        method_version: Any = None
        try:
            principal = await authenticate(request, self.env)
            parsed_url = urlsplit(request.url)
            if parsed_url.path == "/v1/api":
                protocol = "ct.api.v1"
            if (
                request.method == "POST"
                and parsed_url.path == "/v1/artifacts/batch"
                and not parsed_url.query
            ):
                require_that(
                    "collect" in principal["roles"] or "owner" in principal["roles"],
                    "capability_required",
                    403,
                )
                require_that(
                    request.headers.get("content-type") == BATCH_CONTENT_TYPE,
                    "invalid_batch_content_type",
                    415,
                )
                try:
                    objects = decode_batch(
                        await bounded(request.body, BATCH_WIRE_BYTES)
                    )
                except Fault:
                    raise
                except (ValueError, TypeError):
                    raise Fault(400, "invalid_artifact_batch") from None
                slots = asyncio.Semaphore(4)

                async def upload(item):
                    kind, sha256, body = item
                    async with slots:
                        try:
                            result = await self.upload_object(
                                principal, kind, sha256, body
                            )
                            return {"kind": kind, **result}
                        except Exception as error:  # noqa: BLE001 -- each object reports a sanitized outcome
                            failure = authority_failure(error, "artifact_batch")
                            return {
                                "kind": kind,
                                "sha256": sha256,
                                "bytes": len(body),
                                "ok": False,
                                "error": {"code": failure.code},
                                "status": failure.status,
                            }

                results = await asyncio.gather(*(upload(item) for item in objects))
                return _json_response({"results": results}, self.env)
            artifact_upload = re.fullmatch(
                r"/v1/artifacts/(facts|summary|api)/([0-9a-f]{64})", parsed_url.path
            )
            if request.method == "PUT" and artifact_upload and not parsed_url.query:
                require_that(
                    "collect" in principal["roles"] or "owner" in principal["roles"],
                    "capability_required",
                    403,
                )
                kind, sha256 = artifact_upload.groups()
                body = await bounded(request.body, MAX_ARTIFACT_BYTES)
                result = await self.upload_object(principal, kind, sha256, body)
                return _json_response(result, self.env)

            if (
                request.method == "POST"
                and parsed_url.path == "/v1/api"
                and not parsed_url.query
            ):
                require_that(
                    "read" in principal["roles"] or "owner" in principal["roles"],
                    "capability_required",
                    403,
                )
                try:
                    message = object_value(
                        _json_loads(await bounded(request.body, 64 * 1024))
                    )
                except Exception as error:
                    if isinstance(error, Fault):
                        raise
                    raise Fault(400, "invalid_json") from error
                request_id = message.get("id")
                method = message.get("method")
                method_version = message.get("method_version")
                validate_api(message)
                workspace = self.env.WORKSPACES.getByName(principal["workspace_id"])
                identity = None
                if method == "living.sessions":
                    reply = object_value(
                        _json_loads(
                            await workspace.invoke(
                                "ct_remote_living",
                                _json_dumps(
                                    {
                                        "request": {
                                            "workspace_id": principal["workspace_id"],
                                            "calls": [
                                                {
                                                    "method": method,
                                                    "params": message["params"],
                                                }
                                            ],
                                        }
                                    }
                                ),
                                _json_dumps(principal),
                            )
                        )
                    )
                    require_that(
                        reply.get("status") == 200,
                        _response_error_code(reply.get("body", {})),
                        reply.get("status", 503),
                    )
                    result = reply["body"]["results"][0]["result"]
                else:
                    cursor = (
                        await read_cursor(
                            message["params"]["cursor"], self.env.CT_CURSOR_KEY
                        )
                        if message["params"].get("cursor")
                        else None
                    )
                    require_that(
                        not cursor
                        or cursor.get("workspace_id") == principal["workspace_id"],
                        "invalid_cursor",
                    )
                    require_that(
                        not cursor
                        or not message["params"].get("view_manifest_sha256")
                        or cursor.get("view_manifest_sha256")
                        == message["params"]["view_manifest_sha256"],
                        "invalid_cursor",
                    )
                    reply = object_value(
                        _json_loads(
                            await workspace.invoke(
                                "ct_internal_api_locator",
                                _json_dumps(
                                    {
                                        "request": {
                                            "workspace_id": principal["workspace_id"],
                                            "method": method,
                                            "params": message["params"],
                                            "view_manifest_sha256": cursor.get(
                                                "view_manifest_sha256"
                                            )
                                            if cursor
                                            else message["params"].get(
                                                "view_manifest_sha256"
                                            ),
                                        }
                                    }
                                ),
                                _json_dumps(principal),
                            )
                        )
                    )
                    require_that(
                        reply.get("status") == 200,
                        _response_error_code(reply.get("body", {})),
                        reply.get("status", 503),
                    )
                    identity = reply["body"]["identity"]
                    result = await serve_prepared(
                        self.env, reply["body"], method, message["params"]
                    )
                envelope = {
                    "protocol": protocol,
                    "id": request_id,
                    "method": method,
                    "method_version": method_version,
                    "ok": True,
                    "data": result,
                    "availability": {"state": "complete", "missing": []},
                    "error": None,
                    "meta": {
                        "source": "remote",
                        "freshness": "authoritative",
                        "content_scope": "facts",
                        "identity": identity,
                    },
                }
                response_body = _json_dumps(envelope).encode("utf-8")
                require_that(
                    len(response_body) <= 448 * 1024, "remote_result_too_large", 413
                )
                return Response(
                    response_body,
                    headers={
                        **_response_headers(self.env),
                        "Content-Type": "application/json",
                    },
                )

            require_that(
                request.method == "POST"
                and not parsed_url.query
                and parsed_url.path == "/v1/core",
                "not_found",
                404,
            )
            try:
                message = object_value(_json_loads(await bounded(request.body)))
            except Exception as error:
                if isinstance(error, Fault):
                    raise
                raise Fault(400, "invalid_json") from error
            fields(
                message,
                [
                    "protocol",
                    "id",
                    "method",
                    "params",
                    "idempotency_key",
                    "request_sha256",
                ],
                ["protocol", "method", "params"],
            )
            require_that(message["protocol"] == PROTOCOL, "invalid_protocol")
            request_id = message.get("id")
            method_name = text(message["method"], 128)
            method = method_name
            role = (
                "authenticated"
                if method_name == "ct_connection_status"
                else "collect"
                if method_name in COLLECT
                else "read"
                if method_name in READ
                else None
            )
            require_that(role, "not_found", 404)
            require_that(
                role == "authenticated"
                or role in principal["roles"]
                or "owner" in principal["roles"],
                "capability_required",
                403,
            )
            body = object_value(message["params"])
            require_that(
                body.get("workspace_id") == principal["workspace_id"],
                "workspace_denied",
                403,
            )
            if method_name == "ct_connection_status":
                fields(body, ["workspace_id"], ["workspace_id"])
                return _json_response(
                    {
                        "protocol": PROTOCOL,
                        "id": request_id,
                        "method": method_name,
                        "ok": True,
                        "data": {
                            "protocol": PROTOCOL,
                            "workspace_id": principal["workspace_id"],
                            "agent_id": principal["agent_id"],
                            "roles": principal["roles"],
                        },
                        "availability": {"state": "complete", "missing": []},
                        "error": None,
                    },
                    self.env,
                )
            if method_name in COLLECT:
                require_that(
                    body.get("agent_id") == principal["agent_id"], "agent_denied", 403
                )
            if message.get("idempotency_key") is not None:
                text(message["idempotency_key"], 512)
            envelope: Json = {"request": body}
            if message.get("idempotency_key") is not None:
                envelope["idempotency_key"] = message["idempotency_key"]
            if message.get("request_sha256") is not None:
                envelope["request_sha256"] = message["request_sha256"]
            workspace = self.env.WORKSPACES.getByName(principal["workspace_id"])
            result = object_value(
                _json_loads(
                    await workspace.invoke(
                        method_name, _json_dumps(envelope), _json_dumps(principal)
                    )
                )
            )
            if 200 <= result["status"] < 300 and result.get("body", {}).get(
                "__artifact_key"
            ):
                locator = result["body"]
                instrumentation = locator.get("__benchmark")
                artifact = await self.env.ARTIFACTS.get(locator["__artifact_key"])
                require_that(artifact, "artifact_object_missing", 503)
                artifact_bytes = bytes_from_buffer(await artifact.arrayBuffer())
                require_that(
                    await digest(artifact_bytes) == locator["sha256"],
                    "artifact_object_corrupt",
                    503,
                )
                try:
                    result["body"] = object_value(_json_loads(artifact_bytes))
                except Exception as error:
                    raise Fault(503, "artifact_object_corrupt") from error
                if instrumentation:
                    result["body"]["__benchmark"] = instrumentation
            ok = 200 <= result["status"] < 300
            failure_code = _response_error_code(result.get("body", {}))
            response = (
                {
                    "protocol": PROTOCOL,
                    "id": request_id,
                    "method": method_name,
                    "ok": True,
                    "data": result["body"],
                    "availability": {"state": "complete", "missing": []},
                    "error": None,
                }
                if ok
                else {
                    "protocol": PROTOCOL,
                    "id": request_id,
                    "method": method_name,
                    "ok": False,
                    "data": None,
                    "availability": {
                        "state": "unavailable",
                        "missing": [{"field": "$", "reason": failure_code}],
                    },
                    "error": {"code": failure_code},
                }
            )
            return _json_response(response, self.env, status=result["status"])
        except Exception as error:  # noqa: BLE001 -- sanitize every platform boundary failure
            return error_response(
                error,
                self.env,
                protocol=protocol,
                request_id=request_id,
                method=method,
                method_version=method_version,
            )
