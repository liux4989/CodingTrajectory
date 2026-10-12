"""Lossless local script source artifacts from transcript inputs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from coding_trajectory.ingestion.execution_models import SourceArtifact
from coding_trajectory.ingestion.models import Item, Session, Vendor
from coding_trajectory.query import DocumentError


class ToolSourceEvidence(BaseModel):
    """Transient local evidence; never serialized into Core's retained store."""

    canonical_session: Session
    full_session: Session
    item: Item
    source_text: str
    source_field: str
    language: str
    source_log: Path
    source_log_sha256: str
    records: list[dict[str, Any]]


def _extract_source(
    item: Item, field: str | None, vendor: Vendor
) -> tuple[str, str, str]:
    value = (
        item.command
        if item.kind == "command_execution"
        else getattr(item, "input", None)
    )
    literal_execution_source = item.kind == "command_execution" or (
        vendor == Vendor.CODEX_CLI and item.tool_name == "exec"
    )
    if (
        not literal_execution_source
        and isinstance(value, str)
        and value.lstrip().startswith(("{", "["))
    ):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                value = parsed
        except ValueError:
            pass
    if field:
        for part in field.split("."):
            if not isinstance(value, dict) or part not in value:
                raise DocumentError(f"source field {field!r} is unavailable")
            value = value[part]
        source_field = field
    elif isinstance(value, dict):
        fields = [
            key
            for key in (
                "code",
                "script",
                "source",
                "function",
                "cmd",
                "command",
                "shell",
            )
            if isinstance(value.get(key), str)
        ]
        if not fields:
            raise DocumentError(
                "no source-text argument; select one explicitly with --field"
            )
        if len(fields) != 1:
            raise DocumentError(
                "multiple source-text arguments; select one with --field"
            )
        source_field = fields[0]
        value = value[source_field]
    else:
        source_field = "command" if item.kind == "command_execution" else "input"
    if not isinstance(value, str):
        raise DocumentError("the selected argument has no source text")
    # Extensions reflect explicit execution contracts, not inferred actions.
    language = "shell" if item.kind == "command_execution" else "text"
    if (
        vendor == Vendor.CODEX_CLI
        and item.tool_name == "exec"
        and source_field == "input"
    ):
        language = "javascript"
    return value, source_field, language


def _native_command_source(
    item: Item, records: list[dict[str, Any]], field: str | None
) -> tuple[str, str, str] | None:
    """Read literal native command text before adapter display normalization."""
    if item.kind != "command_execution":
        return None
    activity = item.vendor_data.get("activity", {})
    provenance = activity.get("provenance", {}) if isinstance(activity, dict) else {}
    native_id = (
        provenance.get("native_command_id") if isinstance(provenance, dict) else None
    )
    if not native_id:
        return None
    sources = set()
    for index, row in enumerate(records):
        payload = row.get("payload", {})
        native = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(native, dict) or native.get("id") != native_id:
            continue
        command = native.get("command")
        pointer = f"records[{row.get('_ct_record_index', index)}].payload.item.command"
        if isinstance(command, str):
            sources.add((command, pointer))
        elif (
            isinstance(command, list)
            and command
            and isinstance(command[0], str)
            and Path(command[0]).name in {"sh", "bash", "zsh", "fish", "dash", "ksh"}
        ):
            # An argument vector is not source code. Only an explicit shell
            # source argument has a lossless textual representation.
            for position, part in enumerate(command[:-1]):
                if (
                    isinstance(part, str)
                    and part in {"-c", "-lc"}
                    and isinstance(command[position + 1], str)
                ):
                    sources.add((command[position + 1], f"{pointer}[{position + 1}]"))
    if len({text for text, _ in sources}) != 1:
        raise DocumentError("native command has no unambiguous literal script source")
    if field and field != "command":
        raise DocumentError("native command source field is command")
    text, pointer = min(sources, key=lambda pair: pair[1])
    return text, pointer, "shell"


def materialize_bytes(path: Path, content: bytes) -> None:
    """Atomically install complete private content, preserving an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        stream.write(content)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise DocumentError(
                    "existing artifact differs from its content digest"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def materialize_source(
    evidence: ToolSourceEvidence, directory: Path | None = None
) -> SourceArtifact:
    source_bytes = evidence.source_text.encode("utf-8")
    digest = hashlib.sha256(source_bytes).hexdigest()
    extension = {"javascript": "js", "shell": "sh", "text": "txt"}[evidence.language]
    directory = (
        directory.resolve() if directory else Path.cwd() / ".artifacts" / "tool-sources"
    )
    path = directory / f"{evidence.item.item_id}-{digest}.{extension}"
    artifact = SourceArtifact(
        session_id=str(evidence.item.session_id),
        item_id=str(evidence.item.item_id),
        tool_name=evidence.item.tool_name,
        source_field=evidence.source_field,
        language=evidence.language,
        path=str(path),
        link=f"[Full source](<{path}>)",
        provenance_path=str(path.with_suffix(path.suffix + ".json")),
        source_sha256=digest,
        source_log=str(evidence.source_log),
        source_log_sha256=evidence.source_log_sha256,
        provider_call_id=getattr(evidence.item, "tool_call_id", None),
    )
    path = Path(artifact.path)
    # Validate ownership before materializing anything. Keep exports immutable
    # and preserve the first source-log snapshot when live logs later append.
    if path.exists() and path.read_bytes() != source_bytes:
        raise DocumentError("existing source export differs from its content digest")
    provenance_path = Path(artifact.provenance_path)
    if provenance_path.exists():
        existing = SourceArtifact.model_validate_json(provenance_path.read_bytes())
        if (
            existing.item_id != artifact.item_id
            or existing.source_sha256 != artifact.source_sha256
        ):
            raise DocumentError(
                "existing source provenance does not match the selected item"
            )
        artifact = existing
    materialize_bytes(path, source_bytes)
    content = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
    # Two concurrent requests can fence different append-only snapshots for
    # the same source. Keep the first matching receipt, never overwrite it.
    try:
        materialize_bytes(provenance_path, content)
    except DocumentError:
        existing = SourceArtifact.model_validate_json(provenance_path.read_bytes())
        if (
            existing.item_id != artifact.item_id
            or existing.source_sha256 != artifact.source_sha256
        ):
            raise
        artifact = existing
    return artifact
