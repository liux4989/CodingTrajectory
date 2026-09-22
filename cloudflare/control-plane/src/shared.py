"""Shared validation, encoding, and SQLite helpers for the Python Worker."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import uuid as uuid_module
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

Json = dict[str, Any]
Principal = dict[str, Any]

UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_BODY = 3 * 1024 * 1024
MAX_BODY_BYTES = MAX_BODY
MAX_SAFE_INTEGER = 9_007_199_254_740_991
SURROGATE = re.compile("[\ud800-\udfff]")


class Fault(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def authority_failure(error: BaseException, boundary: str) -> Fault:
    if isinstance(error, Fault):
        return error
    message = str(error)[:4096]
    if re.search(
        r"Exceeded allowed rows read in Durable Objects free tier",
        message,
        re.IGNORECASE,
    ):
        code = "database_read_quota_exceeded"
    elif re.search(
        r"Exceeded allowed rows written in Durable Objects free tier",
        message,
        re.IGNORECASE,
    ):
        code = "database_write_quota_exceeded"
    else:
        code = "authority_unavailable"
    print(
        json.dumps(
            {
                "event": "control_plane_failure",
                "boundary": boundary,
                "code": code,
                "category": "unexpected"
                if code == "authority_unavailable"
                else "quota",
            },
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )
    return Fault(503, code)


def require_that(value: Any, code: str, status: int = 400) -> None:
    if not value:
        raise Fault(status, code)


def object_value(value: Any) -> Json:
    require_that(isinstance(value, dict), "object_required")
    return value


# The TypeScript module exports this name and the direct port reads naturally with it.
object = object_value


def fields(
    value: Json,
    allowed: list[str] | tuple[str, ...],
    required: list[str] | tuple[str, ...] = (),
) -> None:
    require_that(
        all(key in allowed for key in value) and all(key in value for key in required),
        "invalid_fields",
    )


def integer(value: Any, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    require_that(
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
        and abs(value) <= MAX_SAFE_INTEGER,
        "invalid_integer",
    )
    return value


def text(value: Any, maximum: int = 512) -> str:
    require_that(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_string")
    return value


def uuid(value: Any) -> str:
    result = text(value, 36)
    require_that(UUID.fullmatch(result), "invalid_uuid")
    return result.lower()


def parse_timestamp(value: Any) -> datetime:
    result = text(value, 64)
    try:
        parsed = datetime.fromisoformat(result)
    except ValueError as exc:
        raise Fault(400, "invalid_timestamp") from exc
    require_that(parsed.tzinfo is not None, "invalid_timestamp")
    return parsed


def timestamp(value: Any) -> str:
    parse_timestamp(value)
    return value


def _contract_models() -> dict[str, type[BaseModel]]:
    from coding_trajectory.contracts.living import (
        LivingChange,
        LivingEventsRequest,
        LivingEventsResponse,
        LivingSessionsChange,
        LivingSessionsRequest,
        LivingSessionsResponse,
    )
    from coding_trajectory.contracts.prepared_api import ApiRequest
    from coding_trajectory.control_plane.artifact_protocol import (
        ArtifactManifestRequest,
        ArtifactPublicationRequest,
        ArtifactReadinessRequest,
        ArtifactReadRequest,
        CompactPublicationRequest,
    )
    from coding_trajectory.control_plane.collector_protocol import (
        CollectorRecoveryRequest,
        LeaseHeartbeatRequest,
        LivingObservationRequest,
        ObservationRequest,
        ProjectRegistrationRequest,
        SourceCheckpointPayload,
        SourceRegistrationRequest,
    )

    return {
        "ct_project_register": ProjectRegistrationRequest,
        "ct_collector_register_source": SourceRegistrationRequest,
        "ct_collector_recover": CollectorRecoveryRequest,
        "ct_collector_publish_observation": ObservationRequest,
        "ct_collector_publish_artifacts": ArtifactPublicationRequest,
        "ct_collector_artifact_readiness": ArtifactReadinessRequest,
        "compact_publication": CompactPublicationRequest,
        "ct_collector_heartbeat": LeaseHeartbeatRequest,
        "ct_collector_publish_living_observation": LivingObservationRequest,
        "ct_artifact_manifest": ArtifactManifestRequest,
        "ct_artifact_read": ArtifactReadRequest,
        "checkpoint": SourceCheckpointPayload,
        "living_events_change": LivingChange,
        "living_sessions_change": LivingSessionsChange,
        "living_events_request": LivingEventsRequest,
        "living_events_response": LivingEventsResponse,
        "living_sessions_request": LivingSessionsRequest,
        "living_sessions_response": LivingSessionsResponse,
        "api_request": ApiRequest,
    }


_MODELS: dict[str, type[BaseModel]] | None = None


def validate_contract(name: str, value: Any) -> Any:
    """Validate JSON input and return the normalized JSON representation."""
    global _MODELS
    if name.startswith("api_method:"):
        from coding_trajectory.contracts.registry import service_contract

        method = name.removeprefix("api_method:")
        try:
            model = service_contract(method).request_model
        except KeyError as exc:
            raise Fault(400, "invalid_contract") from exc
    else:
        if _MODELS is None:
            _MODELS = _contract_models()
        model = _MODELS.get(name)
        if model is None:
            return value
    try:
        # JSON mode retains strict wire semantics while allowing JSON datetime/UUID strings.
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        parsed = model.model_validate_json(encoded, strict=True)
        return parsed.model_dump(mode="json", by_alias=True, exclude_none=False)
    except (ValidationError, TypeError, ValueError) as exc:
        raise Fault(400, "invalid_contract") from exc


# Compatibility with the generated-validator function name in the TypeScript source.
validate = validate_contract


def _combine_surrogate_pairs(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        first = ord(value[index])
        if 0xD800 <= first <= 0xDBFF and index + 1 < len(value):
            second = ord(value[index + 1])
            if 0xDC00 <= second <= 0xDFFF:
                result.append(
                    chr(0x10000 + ((first - 0xD800) << 10) + second - 0xDC00)
                )
                index += 2
                continue
        result.append(value[index])
        index += 1
    return "".join(result)


def _stable_string(value: str) -> str:
    if not SURROGATE.search(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # Explicit Python strings can contain UTF-16 surrogate code units.
    # JSON.stringify combines valid pairs and escapes remaining lone surrogates.
    encoded = json.dumps(
        _combine_surrogate_pairs(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return SURROGATE.sub(
        lambda match: f"\\u{ord(match.group(0)):04x}",
        encoded,
    )


def _js_string_sort_key(value: str) -> bytes:
    return value.encode("utf-16-be", errors="surrogatepass")


def stable(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(stable(child) for child in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{_stable_string(str(key))}:{stable(value[key])}"
                for key in sorted(
                    value, key=lambda item: _js_string_sort_key(str(item))
                )
            )
            + "}"
        )
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and abs(value) <= MAX_SAFE_INTEGER:
        return str(value)
    if isinstance(value, str):
        # This is the same JSON string spelling used by JSON.stringify for valid
        # Unicode strings, without a Python-to-JS call for every contract field.
        return _stable_string(value)
    if isinstance(value, (int, float)):
        # Canonical identities were originally defined by JSON.stringify. Keep
        # its exact spelling for floats and integers beyond the safe range.
        try:
            from js import JSON  # type: ignore[import-not-found]

            return str(JSON.stringify(value))
        except (ImportError, AttributeError, TypeError):
            return json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
    raise Fault(400, "invalid_json")


async def digest(value: str | bytes | bytearray | memoryview) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return hashlib.sha256(raw).hexdigest()


def digest_sync(value: str | bytes | bytearray | memoryview) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return hashlib.sha256(raw).hexdigest()


def js_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    try:
        return getattr(value, key)
    except (AttributeError, TypeError):
        pass
    try:
        return value[key]
    except (KeyError, TypeError, IndexError):
        return default


def py_value(value: Any) -> Any:
    if isinstance(
        value, (dict, list, tuple, str, bytes, bytearray, int, float, bool, type(None))
    ):
        return value
    converter = getattr(value, "to_py", None)
    if callable(converter):
        try:
            return converter()
        except (TypeError, RuntimeError):
            pass
    return value


def js_value(value: Any) -> Any:
    """Convert Python containers for direct calls through the workerd JS FFI."""
    try:
        from js import Object  # type: ignore[import-not-found]
        from pyodide.ffi import to_js

        return to_js(value, dict_converter=Object.fromEntries)
    except (ImportError, TypeError):
        return value


def rows(cursor: Any) -> list[Any]:
    array = cursor.toArray() if hasattr(cursor, "toArray") else cursor
    array = py_value(array)
    return list(array)


def bytes_from_buffer(value: Any) -> bytes:
    try:
        from js import Uint8Array  # type: ignore[import-not-found]

        return Uint8Array.new(value).to_bytes()
    except (ImportError, AttributeError, TypeError):
        pass
    value = py_value(value)
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    try:
        return bytes(value)
    except (TypeError, ValueError):
        length = int(js_get(value, "length", 0))
        return bytes(int(value[index]) for index in range(length))


async def bounded(stream: Any, limit: int = MAX_BODY) -> bytes:
    require_that(stream is not None, "body_required")
    reader = stream.getReader()
    chunks: list[bytes] = []
    length = 0
    try:
        while True:
            item = await reader.read()
            if bool(js_get(item, "done", False)):
                break
            chunk = bytes_from_buffer(js_get(item, "value"))
            length += len(chunk)
            if length > limit:
                await reader.cancel()
                raise Fault(413, "body_too_large")
            chunks.append(chunk)
    finally:
        reader.releaseLock()
    return b"".join(chunks)


def encode(value: bytes | bytearray | memoryview) -> str:
    return base64.b64encode(bytes(value)).decode("ascii")


def decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise Fault(400, "invalid_base64") from exc


def receipt(outcome: str, sequence: int | None, details: Json | None = None) -> Json:
    return {
        "receipt_id": str(uuid_module.uuid4()),
        "outcome": outcome,
        "committed_sequence": sequence,
        "details": details or {},
    }


class State:
    def __init__(self, sql: Any) -> None:
        self.sql = sql
        sql.exec(
            """CREATE TABLE IF NOT EXISTS sequence (id INTEGER PRIMARY KEY CHECK (id=1), value INTEGER NOT NULL);
            INSERT OR IGNORE INTO sequence VALUES(1,0);
            CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, key TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(kind,key,sequence));
            CREATE INDEX IF NOT EXISTS records_sequence ON records(kind,sequence);
            CREATE TABLE IF NOT EXISTS resources (digest TEXT NOT NULL, resource_id TEXT NOT NULL, PRIMARY KEY(digest,resource_id));"""
        )

    def head(self) -> int:
        return int(
            js_get(
                self.sql.exec("SELECT value FROM sequence WHERE id=1").one(), "value"
            )
        )

    def next(self) -> int:
        return int(
            js_get(
                self.sql.exec(
                    "UPDATE sequence SET value=value+1 WHERE id=1 RETURNING value"
                ).one(),
                "value",
            )
        )

    def pin(self, value: Any = None) -> int:
        head = self.head()
        return head if value is None else integer(value, 0, head)

    def get(self, kind: str, key: str, sequence: int | None = None) -> Json | None:
        if sequence is None:
            sequence = self.head()
        result = rows(
            self.sql.exec(
                "SELECT payload FROM records WHERE kind=? AND key=? AND sequence<=? ORDER BY sequence DESC LIMIT 1",
                kind,
                key,
                sequence,
            )
        )
        return json.loads(js_get(result[0], "payload")) if result else None

    def all(self, kind: str, sequence: int | None = None) -> list[Json]:
        if sequence is None:
            sequence = self.head()
        result = rows(
            self.sql.exec(
                """SELECT r.payload FROM records r JOIN
                (SELECT key,MAX(sequence) AS sequence FROM records WHERE kind=? AND sequence<=? GROUP BY key) latest
                ON r.key=latest.key AND r.sequence=latest.sequence WHERE r.kind=? LIMIT 10001""",
                kind,
                sequence,
                kind,
            )
        )
        require_that(len(result) <= 10000, "workspace_query_limit", 413)
        return [json.loads(js_get(row, "payload")) for row in result]

    def put(self, kind: str, key: str, payload: Json, sequence: int) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        require_that(
            len(encoded.encode("utf-8")) <= 1024 * 1024, "record_too_large", 413
        )
        self.sql.exec(
            "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,key,sequence) DO UPDATE SET payload=excluded.payload",
            kind,
            key,
            sequence,
            encoded,
        )


def safe_chronicle(value: Any, field: str = "") -> None:
    if isinstance(value, str):
        require_that(len(value) <= 512, "unbounded_chronicle_string")
        if field in {"content", "text_preview", "preview", "title"}:
            require_that(0 < len(value) <= 280, "invalid_preview")
            return
        require_that(
            not re.match(r"^\s*data:", value, re.IGNORECASE)
            and not re.search(r"(?:/Users/|/home/|[A-Za-z]:\\|~/)", value)
            and not (
                len(value) >= 128 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value)
            ),
            "private_chronicle_content",
        )
    elif isinstance(value, list):
        for child in value:
            safe_chronicle(child, field)
    elif isinstance(value, dict):
        for key, child in value.items():
            empty = child is None or child == "" or child == [] or child == {}
            require_that(
                empty
                or not re.search(
                    r"(data_uri|blob|media)", key.lower().replace("-", "_")
                ),
                "embedded_chronicle_content",
            )
            safe_chronicle(child, key)


def transaction_sync(storage: Any, callback: Callable[[], Any]) -> Any:
    return storage.transactionSync(callback)
