"""Cheap source inventory and checkpoint classification for living events."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coding_trajectory.discovery import DiscoveryCandidate

_CHECKSUM_BYTES = 4096


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LivingSourceSnapshot(_FrozenModel):
    path: str
    vendor: str
    file_identity: str | None
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)
    committed_offset: int = Field(ge=0)
    prefix_checksum: str | None = None
    tail_checksum: str | None = None
    status: Literal["ready", "partial", "error", "deleted"]
    error: str | None = None
    session_id: str | None = None
    parent_session_id: str | None = None
    root_session_id: str | None = None
    materialized_revision: int | None = Field(default=None, ge=0)
    cwd: str | None = None
    title: str | None = None


class LivingSourceChange(_FrozenModel):
    kind: Literal["new", "append", "replace", "truncate", "delete", "metadata", "error"]
    previous: LivingSourceSnapshot | None = None
    current: LivingSourceSnapshot
    needs_rebuild: bool


class LivingSourceInventory(_FrozenModel):
    changes: tuple[LivingSourceChange, ...] = ()
    unchanged_count: int = Field(default=0, ge=0)
    scanned_count: int = Field(default=0, ge=0)


class _DiskState(_FrozenModel):
    path: str
    file_identity: str
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)


def inventory_source_changes(
    candidates: list[DiscoveryCandidate],
    previous: dict[str, LivingSourceSnapshot],
) -> LivingSourceInventory:
    """Classify a source inventory without opening unchanged JSONL files."""

    changes: list[LivingSourceChange] = []
    seen: set[str] = set()
    unchanged = 0
    for candidate in candidates:
        path = str(candidate.path.expanduser().resolve())
        seen.add(path)
        before = previous.get(path)
        try:
            disk = _disk_state(candidate.path)
            kind = _classify(before, disk)
            if kind is None:
                unchanged += 1
                continue
            current = _changed_snapshot(candidate, before, disk, kind)
            needs_rebuild = kind in {"new", "append", "replace", "truncate"}
            if current.status == "error":
                kind = "error"
                needs_rebuild = False
            elif kind == "append" and (
                before is not None
                and current.committed_offset == before.committed_offset
            ):
                kind = "metadata"
                needs_rebuild = False
            changes.append(
                LivingSourceChange(
                    kind=kind,
                    previous=before,
                    current=current,
                    needs_rebuild=needs_rebuild,
                )
            )
        except OSError as exc:
            current = _error_snapshot(
                path=path,
                vendor=candidate.vendor.value,
                previous=before,
                error=f"{type(exc).__name__}: {exc}",
            )
            changes.append(
                LivingSourceChange(
                    kind="error",
                    previous=before,
                    current=current,
                    needs_rebuild=False,
                )
            )

    for path, before in previous.items():
        if path in seen or before.status == "deleted":
            continue
        changes.append(
            LivingSourceChange(
                kind="delete",
                previous=before,
                current=before.model_copy(update={"status": "deleted", "error": None}),
                needs_rebuild=True,
            )
        )
    changes.sort(key=lambda value: value.current.path)
    return LivingSourceInventory(
        changes=tuple(changes),
        unchanged_count=unchanged,
        scanned_count=len(candidates),
    )


def _disk_state(path: Path) -> _DiskState:
    stat = path.stat()
    if not path.is_file():
        raise ValueError(f"source is not a regular file: {path}")
    return _DiskState(
        path=str(path.expanduser().resolve()),
        file_identity=f"{stat.st_dev}:{stat.st_ino}",
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
    )


def _classify(
    previous: LivingSourceSnapshot | None, disk: _DiskState
) -> Literal["new", "append", "replace", "truncate", "metadata"] | None:
    if previous is None or previous.status == "deleted":
        return "new"
    if previous.file_identity != disk.file_identity:
        return "replace"
    if disk.size < previous.committed_offset or disk.size < previous.size:
        return "truncate"
    if disk.size == previous.size and disk.mtime_ns == previous.mtime_ns:
        return None
    if previous.status == "error":
        return "replace"
    if disk.size == previous.size:
        return "replace"
    if not _checkpoint_matches(previous, Path(disk.path)):
        return "replace"
    if disk.size > previous.committed_offset:
        return "append"
    return "metadata"


def _changed_snapshot(
    candidate: DiscoveryCandidate,
    previous: LivingSourceSnapshot | None,
    disk: _DiskState,
    kind: str,
) -> LivingSourceSnapshot:
    path = Path(disk.path)
    try:
        committed = _last_complete_offset(path, disk.size)
        prefix, tail = _checkpoint_checksums(path, committed)
        session_id = previous.session_id if previous else None
        parent_session_id = previous.parent_session_id if previous else None
        cwd = previous.cwd if previous else None
        title = previous.title if previous else None
        if kind in {"new", "replace", "truncate"} or session_id is None:
            header = candidate.adapter_cls().scan_identity(path)
            if header is None:
                raise ValueError("no session identity found in bounded header scan")
            session_id = str(header.session_id)
            parent_session_id = (
                str(header.parent_session_id)
                if header.parent_session_id is not None
                else None
            )
            cwd = header.cwd
            title = header.title
        after = _disk_state(path)
        if after != disk:
            raise RuntimeError("source metadata changed during header scan")
        return LivingSourceSnapshot(
            path=disk.path,
            vendor=candidate.vendor.value,
            file_identity=disk.file_identity,
            size=disk.size,
            mtime_ns=disk.mtime_ns,
            ctime_ns=disk.ctime_ns,
            committed_offset=committed,
            prefix_checksum=prefix,
            tail_checksum=tail,
            status="partial" if committed < disk.size else "ready",
            session_id=session_id,
            parent_session_id=parent_session_id,
            root_session_id=(
                previous.root_session_id
                if previous is not None and kind not in {"replace", "truncate"}
                else None
            ),
            materialized_revision=previous.materialized_revision if previous else None,
            cwd=cwd,
            title=title,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return _error_snapshot(
            path=disk.path,
            vendor=candidate.vendor.value,
            previous=previous,
            disk=disk,
            error=f"{type(exc).__name__}: {exc}",
        )


def _error_snapshot(
    *,
    path: str,
    vendor: str,
    previous: LivingSourceSnapshot | None,
    error: str,
    disk: _DiskState | None = None,
) -> LivingSourceSnapshot:
    return LivingSourceSnapshot(
        path=path,
        vendor=vendor,
        file_identity=disk.file_identity if disk else None,
        size=disk.size if disk else 0,
        mtime_ns=disk.mtime_ns if disk else 0,
        ctime_ns=disk.ctime_ns if disk else 0,
        committed_offset=previous.committed_offset if previous else 0,
        prefix_checksum=previous.prefix_checksum if previous else None,
        tail_checksum=previous.tail_checksum if previous else None,
        status="error",
        error=error,
        session_id=previous.session_id if previous else None,
        parent_session_id=previous.parent_session_id if previous else None,
        root_session_id=previous.root_session_id if previous else None,
        materialized_revision=previous.materialized_revision if previous else None,
        cwd=previous.cwd if previous else None,
        title=previous.title if previous else None,
    )


def _last_complete_offset(path: Path, size: int) -> int:
    if size <= 0:
        return 0
    chunk_size = 64 * 1024
    with path.open("rb") as source:
        source.seek(size - 1)
        if source.read(1) == b"\n":
            return size
        position = size
        while position > 0:
            start = max(0, position - chunk_size)
            source.seek(start)
            chunk = source.read(position - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            position = start
    return 0


def _checkpoint_checksums(path: Path, committed: int) -> tuple[str, str]:
    if committed == 0:
        empty = hashlib.sha256(b"").hexdigest()
        return empty, empty
    size = min(_CHECKSUM_BYTES, committed)
    with path.open("rb") as source:
        prefix = source.read(size)
        source.seek(committed - size)
        tail = source.read(size)
    return hashlib.sha256(prefix).hexdigest(), hashlib.sha256(tail).hexdigest()


def _checkpoint_matches(snapshot: LivingSourceSnapshot, path: Path) -> bool:
    try:
        prefix, tail = _checkpoint_checksums(path, snapshot.committed_offset)
    except OSError:
        return False
    return hmac.compare_digest(
        prefix, snapshot.prefix_checksum or ""
    ) and hmac.compare_digest(tail, snapshot.tail_checksum or "")


__all__ = [
    "LivingSourceChange",
    "LivingSourceInventory",
    "LivingSourceSnapshot",
    "inventory_source_changes",
]
