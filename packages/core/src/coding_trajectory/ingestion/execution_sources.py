"""Read-only script source references into original transcript inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from coding_trajectory.ingestion.execution_models import SourceReference
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


def source_reference(evidence: ToolSourceEvidence) -> SourceReference:
    """Locate literal source in its authoritative log without exporting files."""
    return SourceReference(
        session_id=str(evidence.item.session_id),
        item_id=str(evidence.item.item_id),
        tool_name=evidence.item.tool_name,
        source_field=evidence.source_field,
        language=evidence.language,
        link=f"[Open source log](<{evidence.source_log}>)",
        record_indices=[row["_ct_record_index"] for row in evidence.records],
        record_digests=[row["_ct_record_sha256"] for row in evidence.records],
        source_sha256=hashlib.sha256(evidence.source_text.encode("utf-8")).hexdigest(),
        source_log=str(evidence.source_log),
        source_log_sha256=evidence.source_log_sha256,
        provider_call_id=getattr(evidence.item, "tool_call_id", None),
    )
