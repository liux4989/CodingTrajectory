"""Bounded transport framing; artifact bodies remain byte-for-byte unchanged."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from coding_trajectory.control_plane.collector_protocol import CollectorModel

BATCH_OBJECTS = 32
BATCH_BYTES = 512 * 1024
BATCH_OBJECT_BYTES = 64 * 1024
BATCH_HEADER_BYTES = 8192
BATCH_WIRE_BYTES = 4 + BATCH_HEADER_BYTES + BATCH_BYTES
BATCH_CONTENT_TYPE = "application/vnd.ct.artifact-batch.v1"


class BatchObject(CollectorModel):
    kind: Literal["facts", "summary", "api"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(strict=True, ge=1, le=BATCH_OBJECT_BYTES)


class BatchHeader(CollectorModel):
    version: Literal[1] = 1
    objects: list[BatchObject] = Field(min_length=1, max_length=BATCH_OBJECTS)

    @model_validator(mode="after")
    def bounded_unique(self):
        if sum(o.bytes for o in self.objects) > BATCH_BYTES:
            raise ValueError("batch payload exceeds limit")
        if len({(o.kind, o.sha256) for o in self.objects}) != len(self.objects):
            raise ValueError("duplicate batch object")
        return self


def encode_batch(objects: list[tuple[str, str, bytes]]) -> bytes:
    header = BatchHeader(
        objects=[BatchObject(kind=k, sha256=s, bytes=len(b)) for k, s, b in objects]
    )
    raw = header.model_dump_json().encode()
    if len(raw) > BATCH_HEADER_BYTES:
        raise ValueError("batch header exceeds limit")
    return len(raw).to_bytes(4, "big") + raw + b"".join(b for _, _, b in objects)


def decode_batch(body: bytes) -> list[tuple[str, str, bytes]]:
    if not 4 < len(body) <= BATCH_WIRE_BYTES:
        raise ValueError("invalid batch length")
    size = int.from_bytes(body[:4], "big")
    if not 0 < size <= BATCH_HEADER_BYTES or 4 + size > len(body):
        raise ValueError("invalid batch header length")
    header = BatchHeader.model_validate(json.loads(body[4 : 4 + size]))
    offset = 4 + size
    if offset + sum(o.bytes for o in header.objects) != len(body):
        raise ValueError("batch body length mismatch")
    result = []
    for obj in header.objects:
        result.append((obj.kind, obj.sha256, body[offset : offset + obj.bytes]))
        offset += obj.bytes
    return result


def upload_groups(objects, *, enabled=True):
    """Yield bounded small-object batches and standalone larger objects."""
    group, size = [], 0
    for ref in objects:
        if not enabled or ref.bytes > BATCH_OBJECT_BYTES:
            yield [ref]
        else:
            if len(group) == BATCH_OBJECTS or size + ref.bytes > BATCH_BYTES:
                yield group
                group, size = [], 0
            group.append(ref)
            size += ref.bytes
    if group:
        yield group
