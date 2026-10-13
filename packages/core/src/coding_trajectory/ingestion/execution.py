"""Build the canonical execution layer without reconstructing program order."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_trajectory.analysis.tool_arguments import (
    sanitize_argument_preview,
    tool_argument_detail,
)
from coding_trajectory.ingestion.execution_models import (
    ContextReferences,
    DirectItemReference,
    ExecutionResult,
    ExternalCall,
    SourceReference,
    ToolScriptExecution,
)
from coding_trajectory.ingestion.execution_sources import (
    ToolSourceEvidence,
    _extract_source,
    _native_command_source,
    source_reference,
)
from coding_trajectory.ingestion.models import Item, Session
from coding_trajectory.ingestion.process_tracing import (
    attach_process_traces,
    process_origins,
)
from coding_trajectory.ingestion.source_calls import (
    SourceCall,
    argument_strings,
    source_calls,
)
from coding_trajectory.query import DocumentError


def _activity(item: Item) -> tuple[dict[str, Any], dict[str, Any]]:
    activity = item.vendor_data.get("activity", {})
    activity = activity if isinstance(activity, dict) else {}
    provenance = activity.get("provenance", {})
    return activity, provenance if isinstance(provenance, dict) else {}


def _context(evidence: ToolSourceEvidence) -> ContextReferences:
    turn = next(
        t
        for t in evidence.canonical_session.turns
        if t.turn_id == evidence.item.turn_id
    )
    index = next(
        i for i, item in enumerate(turn.items) if item.item_id == evidence.item.item_id
    )
    return ContextReferences(
        session_id=str(evidence.item.session_id),
        turn_id=str(turn.turn_id),
        user_request_event_id=str(turn.user_request_event_id)
        if turn.user_request_event_id
        else None,
        previous_item_id=str(turn.items[index - 1].item_id) if index else None,
        next_item_id=str(turn.items[index + 1].item_id)
        if index + 1 < len(turn.items)
        else None,
        preceding_assistant_item_ids=[
            str(i.item_id) for i in turn.items[:index] if i.kind == "agent_message"
        ],
        following_assistant_item_ids=[
            str(i.item_id) for i in turn.items[index + 1 :] if i.kind == "agent_message"
        ],
    )


def _native_calls(
    evidence: ToolSourceEvidence,
) -> tuple[list[ExternalCall], list[ExternalCall]]:
    observed, source_only = [], []
    origin = getattr(evidence.item, "tool_call_id", None)
    if not origin:
        return observed, source_only
    for turn in evidence.full_session.turns:
        for item in turn.items:
            activity, provenance = _activity(item)
            if provenance.get("parent_tool_call_id") != origin:
                continue
            native = activity.get("fidelity") in {
                "observed_native",
                "derived_matched_native",
            }
            outcome = activity.get("outcome") if native else None
            arguments = (
                item.command
                if item.kind == "command_execution"
                else getattr(item, "input", None)
            )
            native_id = provenance.get("native_command_id") if native else None
            recorded_duration = None
            if native_id:
                for row in evidence.records:
                    payload = row.get("payload", {})
                    raw = payload.get("item") if isinstance(payload, dict) else None
                    if isinstance(raw, dict) and raw.get("id") == native_id:
                        arguments = {
                            key: raw[key] for key in ("command", "cwd") if key in raw
                        }
                        duration = raw.get("duration")
                        if isinstance(duration, dict):
                            seconds, nanos = duration.get("secs"), duration.get("nanos")
                            if all(
                                isinstance(value, (int, float))
                                and not isinstance(value, bool)
                                for value in (seconds, nanos)
                            ):
                                value = seconds * 1000 + nanos / 1_000_000
                                if math.isfinite(value) and value >= 0:
                                    recorded_duration = value
                        break
            call = ExternalCall(
                observation="observed" if native else "source_only",
                tool_name=item.tool_name or item.kind,
                item_id=str(item.item_id),
                provider_call_id=getattr(item, "tool_call_id", None),
                evidence_ref={
                    "event_ids": [str(i) for i in item.event_ids],
                    "source_offset": provenance.get("source_offset"),
                },
                ownership_basis=str(
                    provenance.get("relationship_basis") or "provider_parent_id"
                ),
                source_tool_name=provenance.get("source_tool_name"),
                arguments_ref={"value": arguments},
                arguments_preview=(
                    detail.description
                    if (detail := tool_argument_detail(item))
                    else None
                ),
                arguments_coverage=("recorded" if native else "source_reconstructed")
                if arguments is not None
                else "not_recorded",
                outcome=outcome if outcome in {"succeeded", "failed"} else None,
                started_at=item.started_at if native else None,
                completed_at=item.completed_at if native else None,
                duration_ms=recorded_duration
                if recorded_duration is not None
                else max(
                    (item.completed_at - item.started_at).total_seconds() * 1000, 0
                )
                if native and item.completed_at
                else None,
            )
            if native and call.provider_call_id:
                chunks = _output_chunks(
                    evidence, call.provider_call_id, provenance.get("native_command_id")
                )
                if chunks:
                    call.result_ref = {
                        "source_log": str(evidence.source_log),
                        "record_indices": [c["record_index"] for c in chunks],
                        "record_digests": [c.get("record_sha256") for c in chunks],
                        "provider_call_id": call.provider_call_id,
                    }
                    call.result_coverage = "recorded"
            (observed if native else source_only).append(call)
    return observed, source_only


def _inventories(evidence: ToolSourceEvidence) -> tuple[list[ExternalCall], str]:
    """Read bounded host observations; do not search JavaScript for execution."""
    origin = getattr(evidence.item, "tool_call_id", None)
    calls, claims = [], []
    if not origin:
        return calls, "unknown"
    snapshots = {}
    for index, row in enumerate(evidence.records):
        payload = row.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        message = row.get("message", {})
        message = message if isinstance(message, dict) else {}
        if message.get("toolCallId") == origin and isinstance(
            message.get("nestedCalls"), dict
        ):
            snapshots[("pi", origin)] = row
        metadata = _metadata(row)
        if metadata.get("cell_id") == origin and isinstance(
            metadata.get("executed_tool_calls"), list
        ):
            snapshots[
                ("codex", payload.get("call_id") or payload.get("id") or index)
            ] = row
    # A repeated output envelope replaces its inventory snapshot. Repeated
    # equal entries within one array remain distinct calls; names/arguments
    # cannot prove that two observations identify the same invocation.
    inventory_rows = sorted(
        snapshots.values(), key=lambda row: row.get("_ct_record_index", 0)
    )
    for index, row in enumerate(inventory_rows):
        index = row.get("_ct_record_index", index)
        message = row.get("message", {})
        message = message if isinstance(message, dict) else {}
        payload = row.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        inventory = (
            message.get("nestedCalls") if message.get("toolCallId") == origin else None
        )
        if isinstance(inventory, dict):
            claim = inventory.get("complete")
            if isinstance(claim, bool):
                claims.append(claim)
            entries = inventory.get("calls", [])
            for nested_index, entry in enumerate(
                entries if isinstance(entries, list) else []
            ):
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("name"), str
                ):
                    continue
                calls.append(
                    ExternalCall(
                        observation="observed",
                        tool_name=entry["name"],
                        provider_call_id=entry.get("id"),
                        evidence_ref={
                            "record_index": index,
                            "record_sha256": row.get("_ct_record_sha256"),
                            "pointer": f"message.nestedCalls.calls[{nested_index}]",
                        },
                        ownership_basis="parent_result_inventory",
                        arguments_ref={"value": entry.get("arguments")},
                        arguments_coverage="recorded"
                        if "arguments" in entry
                        else "truncated"
                        if "argumentsBytes" in entry
                        else "not_recorded",
                        outcome={
                            "ok": "succeeded",
                            "error": "failed",
                            "unfinished": "unfinished",
                        }.get(entry.get("status")),
                        duration_ms=entry.get("durationMs"),
                        failure=entry.get("error"),
                    )
                )
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                continue
        if not isinstance(metadata, dict) or metadata.get("cell_id") != origin:
            continue
        if isinstance(metadata.get("executed_tool_calls"), list):
            claim = metadata.get("tool_calls_complete")
            if isinstance(claim, bool):
                claims.append(claim)
            for nested_index, entry in enumerate(metadata["executed_tool_calls"]):
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("name"), str
                ):
                    continue
                arguments = entry.get("arguments")
                truncated = (
                    isinstance(arguments, dict)
                    and "_codex_executed_tool_call_truncated" in arguments
                )
                calls.append(
                    ExternalCall(
                        observation="observed",
                        tool_name=entry["name"],
                        evidence_ref={
                            "record_index": index,
                            "record_sha256": row.get("_ct_record_sha256"),
                            "pointer": f"payload.internal_chat_message_metadata_passthrough.executed_tool_calls[{nested_index}]",
                        },
                        ownership_basis="host_originating_call_metadata",
                        arguments_ref=None if truncated else {"value": arguments},
                        arguments_coverage="truncated"
                        if truncated
                        else "recorded"
                        if arguments is not None
                        else "not_recorded",
                    )
                )
    # Completeness is a provider claim, not inferred from script completion or
    # the number of source-level calls. Early output checkpoints may be partial.
    coverage = (
        "complete"
        if claims and claims[-1]
        else "partial"
        if claims or calls
        else "unknown"
    )
    return calls, coverage


def _output_chunks(
    evidence: ToolSourceEvidence,
    call_id: str | None = None,
    native_id: str | None = None,
    continuation_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    origin = call_id or getattr(evidence.item, "tool_call_id", None)
    chunks = []
    for index, row in enumerate(evidence.records):
        index = row.get("_ct_record_index", index)
        if not origin:
            break
        payload = row.get("payload", {})
        if (
            isinstance(payload, dict)
            and payload.get("type")
            in {"function_call_output", "custom_tool_call_output"}
            and payload.get("call_id") in {origin, *(continuation_ids or set())}
        ):
            chunks.append(
                {
                    "record_index": index,
                    "record_sha256": row.get("_ct_record_sha256"),
                    "timestamp": row.get("timestamp"),
                    "value": payload.get("output"),
                }
            )
        item = payload.get("item") if isinstance(payload, dict) else None
        if (
            isinstance(item, dict)
            and item.get("id") in {origin, native_id}
            and item.get("id") is not None
            and payload.get("type") == "item_completed"
        ):
            result = {
                key: item[key]
                for key in (
                    "aggregated_output",
                    "formatted_output",
                    "stdout",
                    "stderr",
                    "exit_code",
                    "results",
                    "result",
                    "error",
                    "content_items",
                )
                if key in item
            }
            if result:
                chunks.append(
                    {
                        "record_index": index,
                        "record_sha256": row.get("_ct_record_sha256"),
                        "timestamp": row.get("timestamp"),
                        "value": result,
                    }
                )
        message = row.get("message", {})
        if not isinstance(message, dict):
            continue
        if message.get("role") == "toolResult" and message.get("toolCallId") == origin:
            chunks.append(
                {
                    "record_index": index,
                    "record_sha256": row.get("_ct_record_sha256"),
                    "value": message.get("content"),
                }
            )
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == origin
                ):
                    chunks.append(
                        {
                            "record_index": index,
                            "record_sha256": row.get("_ct_record_sha256"),
                            "value": block.get("content"),
                        }
                    )
    if (
        not call_id
        and not chunks
        and getattr(evidence.item, "output", None) is not None
    ):
        chunks.append(
            {"item_id": str(evidence.item.item_id), "value": evidence.item.output}
        )
    return chunks


def _output_reference(
    evidence: ToolSourceEvidence, chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Refer to recorded outputs without duplicating their bodies on disk."""
    return {
        "source_log": str(evidence.source_log),
        "source_log_sha256": evidence.source_log_sha256,
        "record_indices": [c["record_index"] for c in chunks if "record_index" in c],
        "record_digests": [
            c.get("record_sha256") for c in chunks if "record_index" in c
        ],
        "item_ids": [c["item_id"] for c in chunks if "item_id" in c],
        "provider_call_id": getattr(evidence.item, "tool_call_id", None),
    }


def _banner_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict) and first.get("type") in {
            "text",
            "input_text",
            "output_text",
        }:
            text = first.get("text")
            return text if isinstance(text, str) else ""
    return ""


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload", {})
    value = (
        payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(payload, dict)
        else None
    )
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _recorded_timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return None


def _call_arguments(payload: dict[str, Any]) -> Any:
    value = payload.get("arguments", payload.get("input"))
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _continuations(
    evidence: ToolSourceEvidence, chunks: list[dict[str, Any]]
) -> tuple[set[str], str | None]:
    origin = getattr(evidence.item, "tool_call_id", None)
    runtime_id = next(
        (
            match.group(1)
            for c in chunks
            if (
                match := re.match(
                    r"Script running with cell ID\s+(\S+)", _banner_text(c["value"])
                )
            )
        ),
        None,
    )
    ids = set()
    for row in evidence.records:
        payload = row.get("payload", {})
        identity = payload.get("call_id")
        if not identity or identity == origin:
            continue
        args = _call_arguments(payload)
        explicit_wait = (
            str(payload.get("name", "")).rsplit(".", 1)[-1] == "wait"
            and runtime_id
            and isinstance(args, dict)
            and args.get("cell_id") == runtime_id
        )
        origin_metadata = _metadata(row).get("cell_id") == origin and payload.get(
            "type"
        ) in {"function_call_output", "custom_tool_call_output"}
        if explicit_wait or origin_metadata:
            ids.add(identity)
    return ids, runtime_id


def build_tool_script(
    evidence: ToolSourceEvidence, source: SourceReference
) -> ToolScriptExecution:
    observed, source_only = _native_calls(evidence)
    candidates, source_coverage = (
        source_calls(evidence.source_text)
        if evidence.language == "javascript"
        else ([], "not_supported")
    )
    matched_offsets = {
        (call.evidence_ref or {}).get("source_offset") for call in observed
    }
    for candidate in candidates:
        if candidate.source_byte_start in matched_offsets:
            continue
        source_only.append(
            ExternalCall(
                observation="source_only",
                tool_name=candidate.tool_name,
                evidence_ref={
                    "source_byte_start": candidate.source_byte_start,
                    "source_byte_end": candidate.source_byte_end,
                },
                ownership_basis="source_call_site",
                arguments_ref={"value": candidate.arguments}
                if candidate.arguments_static
                else None,
                arguments_preview=sanitize_argument_preview(candidate.expression),
                arguments_coverage="source_reconstructed"
                if candidate.arguments_static
                else "not_recorded",
            )
        )
    native_observed = observed
    inventories, coverage = _inventories(evidence)
    # Prefer one observed inventory surface instead of counting matching native
    # items and inventory entries twice without authoritative per-call IDs.
    if inventories or coverage == "complete":
        observed = inventories
    native_by_call = {
        c.provider_call_id: c for c in native_observed if c.provider_call_id
    }
    for call in observed:
        native = native_by_call.get(call.provider_call_id)
        if native and call is not native:
            call.item_id = native.item_id
            call.started_at = native.started_at
            call.completed_at = native.completed_at
            if call.duration_ms is None:
                call.duration_ms = native.duration_ms
            if call.result_ref is None:
                call.result_ref, call.result_coverage = (
                    native.result_ref,
                    native.result_coverage,
                )
    source_only = [
        call
        for call in source_only
        if not any(
            candidate.tool_name == call.tool_name
            and candidate.arguments_coverage == "recorded"
            and (candidate.arguments_ref or {}).get("value") is not None
            and (candidate.arguments_ref or {}).get("value")
            == (call.arguments_ref or {}).get("value")
            for candidate in observed
        )
    ]
    chunks = _output_chunks(evidence)
    continuation_ids, runtime_id = _continuations(evidence, chunks)
    controls = [
        item
        for turn in evidence.full_session.turns
        for item in turn.items
        if getattr(item, "tool_call_id", None) in continuation_ids
    ]
    if continuation_ids:
        chunks = _output_chunks(evidence, continuation_ids=continuation_ids)
    output_ref = _output_reference(evidence, chunks) if chunks else None
    # Only a transport banner establishes a runtime checkpoint. Emitted text
    # mentioning a running cell is not evidence that this invocation yielded.
    orchestration = (
        source.language == "javascript" and evidence.item.tool_name == "exec"
    )
    banners = [_banner_text(c["value"]) for c in chunks] if orchestration else []
    checkpoint = next(
        (
            text
            for text in reversed(banners)
            if re.match(
                r"Script (?:running with cell ID\s+|completed(?:\r?\n|$)|failed|error:|terminated)",
                text,
            )
        ),
        "",
    )
    running = re.match(r"Script running with cell ID\s+(\S+)", checkpoint)
    runtime_completed = bool(re.match(r"Script completed(?:\r?\n|$)", checkpoint))
    runtime_failed = bool(re.match(r"Script (?:failed|error:)", checkpoint))
    runtime_interrupted = bool(re.match(r"Script terminated", checkpoint))
    background_id = next(
        (
            row["toolUseResult"]["backgroundTaskId"]
            for row in evidence.records
            if isinstance(row.get("toolUseResult"), dict)
            and isinstance(row["toolUseResult"].get("backgroundTaskId"), str)
        ),
        None,
    )
    lifecycle = (
        "interrupted"
        if runtime_interrupted
        else "yielded"
        if running or background_id
        else "failed"
        if runtime_failed or evidence.item.status == "failed"
        else "completed"
        if runtime_completed
        or (not orchestration and evidence.item.status == "completed")
        else "unknown"
    )
    for call in [*observed, *source_only]:
        value = (call.arguments_ref or {}).get("value")
        if value is not None:
            call.arguments_preview = (
                call.arguments_preview
                or sanitize_argument_preview(
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)
                )
            )
            call.arguments_ref = (
                {
                    "source_log": str(evidence.source_log),
                    "source_log_sha256": evidence.source_log_sha256,
                    **call.evidence_ref,
                    "item_id": call.item_id,
                    "provider_call_id": call.provider_call_id,
                    "selection": "call_arguments",
                }
                if call.observation == "observed"
                else None
            )
        else:
            call.arguments_ref = None
        if call.failure:
            call.failure = sanitize_argument_preview(call.failure)
    return ToolScriptExecution(
        unit_id=str(evidence.item.item_id),
        control_item_ids=[str(item.item_id) for item in controls],
        item_ids=list(
            dict.fromkeys(
                [
                    str(evidence.item.item_id),
                    *[c.item_id for c in [*native_observed, *source_only] if c.item_id],
                    *[str(item.item_id) for item in controls],
                ]
            )
        ),
        session_id=str(evidence.item.session_id),
        turn_id=str(evidence.item.turn_id),
        item_id=str(evidence.item.item_id),
        tool_name=evidence.item.tool_name,
        source=source,
        source_preview=sanitize_argument_preview(evidence.source_text),
        source_preview_truncated=len(evidence.source_text) > 280,
        open_source_link=source.link,
        context_refs=_context(evidence),
        observed_external_calls=observed,
        source_only_calls=source_only,
        inventory_coverage="partial"
        if coverage == "unknown" and observed
        else coverage,
        source_analysis_coverage=source_coverage,
        result=ExecutionResult(
            invocation_status=evidence.item.status,
            lifecycle=lifecycle,
            started_at=evidence.item.started_at,
            invocation_completed_at=evidence.item.completed_at,
            runtime_execution_id=runtime_id
            or background_id
            or (running.group(1) if running else None),
            runtime_completed_at=(
                _recorded_timestamp(chunks[-1].get("timestamp"))
                if chunks
                and isinstance(chunks[-1].get("timestamp"), str)
                and (runtime_completed or runtime_failed or runtime_interrupted)
                else None
            ),
            lifecycle_coverage="runtime_checkpoint"
            if running
            or background_id
            or runtime_completed
            or runtime_failed
            or runtime_interrupted
            else "invocation_only",
            emitted_output_ref=output_ref,
            output_coverage="recorded" if chunks else "not_recorded",
            output_scope="script" if continuation_ids else "invocation",
            failure_output_ref=output_ref if lifecycle == "failed" else None,
        ),
        inspection_log_ref={
            "path": str(evidence.source_log),
            "scope": "execution_records",
            "sha256": hashlib.sha256(
                "".join(
                    row.get("_ct_record_sha256", "") for row in evidence.records
                ).encode()
            ).hexdigest(),
        },
    )


def _bind_external_calls(session: Session, records: list[dict[str, Any]]) -> None:
    """Observe native calls independently; infer ownership only from evidence.

    Literal argument overlap inside one recorded execution interval is an
    inference, never a provider parent ID or a claim about branch execution.
    Ambiguous parents remain direct items. Repeated runtime calls remain
    separate observations even when they match the same loop call site.
    """
    raw_by_id = {}
    raw_rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        payload = row.get("payload", {})
        raw = payload.get("item", {}) if isinstance(payload, dict) else {}
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            raw_by_id[raw["id"]] = payload
            raw_rows_by_id.setdefault(raw["id"], []).append(row)
    for turn in session.turns:
        roots: list[tuple[Item, list[SourceCall]]] = []
        for item in turn.items:
            if item.kind != "tool_call":
                continue
            try:
                text, _, language = _extract_source(item, None, session.vendor)
            except DocumentError:
                continue
            if language == "javascript":
                roots.append((item, source_calls(text)[0]))
        for item in turn.items:
            activity, provenance = _activity(item)
            if activity.get("fidelity") != "observed_native":
                continue
            native_id = provenance.get("native_command_id") or provenance.get(
                "native_item_id"
            )
            payload = raw_by_id.get(native_id, {})
            raw = payload.get("item", {})
            explicit = next(
                (
                    payload.get(key) or raw.get(key)
                    for key in (
                        "parent_tool_call_id",
                        "originating_call_id",
                        "parent_call_id",
                    )
                    if payload.get(key) or raw.get(key)
                ),
                None,
            )
            arguments = (
                item.command
                if item.kind == "command_execution"
                else getattr(item, "input", None)
            )
            if item.kind == "command_execution":
                try:
                    literal = _native_command_source(
                        item, raw_rows_by_id.get(native_id, []), None
                    )
                except DocumentError:
                    literal = None
                if literal:
                    arguments = {
                        **(arguments if isinstance(arguments, dict) else {}),
                        "cmd": literal[0],
                    }
            subjects = argument_strings(arguments)
            matches = []
            for root, candidates in roots:
                origin = getattr(root, "tool_call_id", None)
                if explicit and explicit == origin:
                    matches.append((root, None, "provider_parent_id"))
                    continue
                if (
                    explicit
                    or root.completed_at is None
                    or not (root.started_at <= item.started_at <= root.completed_at)
                ):
                    continue
                compatible = []
                for candidate in candidates:
                    if not candidate.arguments_static or not subjects.intersection(
                        argument_strings(candidate.arguments)
                    ):
                        continue
                    if isinstance(arguments, dict) and isinstance(
                        candidate.arguments, dict
                    ):
                        common = arguments.keys() & candidate.arguments.keys()
                        if any(
                            isinstance(arguments[key], (str, int, bool))
                            and arguments[key] != candidate.arguments[key]
                            for key in common
                        ):
                            continue
                    compatible.append(candidate)
                if compatible:
                    # A parent can be inferred even if repeated call sites
                    # cannot be disambiguated. Do not invent a source offset.
                    matches.append(
                        (
                            root,
                            compatible[0] if len(compatible) == 1 else None,
                            "inferred_arguments_and_interval",
                        )
                    )
            if len(matches) != 1:
                continue
            root, candidate, basis = matches[0]
            provenance.update(
                parent_tool_call_id=root.tool_call_id,
                relationship_basis=basis,
                content_custody="script_envelope",
            )
            if candidate:
                provenance.update(
                    source_offset=candidate.source_byte_start,
                    source_tool_name=candidate.tool_name,
                )
            activity["provenance"] = provenance
            item.vendor_data["activity"] = activity


def build_session_execution_layer(
    session: Session, source: Path, *, source_byte_end: int | None = None
) -> Session:
    """Attach one shared turn -> execution unit -> flat item-reference layer.

    Source is extracted from a single local snapshot. Flat items remain the
    only custody/measurement objects; inventories do not mint timed items.
    """
    records: list[dict[str, Any]] = []
    snapshot = b""
    if source.is_file():
        with source.open("rb") as stream:
            snapshot = (
                stream.read(source_byte_end)
                if source_byte_end is not None
                else stream.read()
            )
        for ordinal, line in enumerate(snapshot.split(b"\n")):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict):
                row["_ct_record_index"] = ordinal
                row["_ct_record_sha256"] = hashlib.sha256(line.strip()).hexdigest()
                records.append(row)
    record_groups: dict[str, list[dict[str, Any]]] = {}
    waits: dict[str, str] = {}
    for row in records:
        payload = row.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        message = row.get("message", {})
        message = message if isinstance(message, dict) else {}
        identities = {
            payload.get("call_id"),
            _metadata(row).get("cell_id"),
            message.get("toolCallId"),
        }
        native = payload.get("item", {})
        if isinstance(native, dict):
            identities.add(native.get("id"))
        content = message.get("content", [])
        if isinstance(content, list):
            identities.update(
                b.get("tool_use_id", b.get("id"))
                for b in content
                if isinstance(b, dict)
            )
        args = _call_arguments(payload)
        if (
            str(payload.get("name", "")).rsplit(".", 1)[-1] == "wait"
            and isinstance(args, dict)
            and isinstance(args.get("cell_id"), str)
        ):
            if not isinstance(payload.get("call_id"), str):
                continue
            waits[payload["call_id"]] = args["cell_id"]
            identities.add(args["cell_id"])
        for identity in identities:
            if isinstance(identity, str):
                record_groups.setdefault(identity, []).append(row)
    for identity, runtime_id in waits.items():
        record_groups.setdefault(runtime_id, []).extend(record_groups.get(identity, []))
    _bind_external_calls(session, records)
    child_identities: dict[str, set[str]] = {}
    for turn in session.turns:
        for item in turn.items:
            _, provenance = _activity(item)
            parent = provenance.get("parent_tool_call_id")
            if parent:
                child_identities.setdefault(parent, set()).update(
                    str(i)
                    for i in (
                        getattr(item, "tool_call_id", None),
                        provenance.get("native_command_id"),
                        provenance.get("native_item_id"),
                    )
                    if i
                )
    snapshot_digest = hashlib.sha256(snapshot).hexdigest()
    native_parents = {}
    for turn in session.turns:
        for item in turn.items:
            _, provenance = _activity(item)
            if provenance.get("native_command_id") and provenance.get(
                "parent_tool_call_id"
            ):
                native_parents[provenance["native_command_id"]] = provenance[
                    "parent_tool_call_id"
                ]
    origins = process_origins(records, source, snapshot_digest, native_parents)
    scripts: dict[str, ToolScriptExecution] = {}
    direct_sources: dict[str, SourceReference] = {}
    owned: set[str] = set()
    for turn in session.turns:
        for item in turn.items:
            if item.kind not in {"tool_call", "command_execution", "plan"}:
                continue
            _, provenance = _activity(item)
            if provenance.get("parent_tool_call_id"):
                continue
            # Recorded executor inputs define scripts. Tool names do not
            # categorize actions, and arbitrary arguments are not source code.
            origin = getattr(item, "tool_call_id", None)
            related = {
                origin,
                provenance.get("native_command_id"),
                *child_identities.get(origin, set()),
            }
            root_records = [
                row
                for identity in related
                if identity
                for row in record_groups.get(identity, [])
            ]
            for row in list(root_records):
                payload = row.get("payload", {})
                output = payload.get("output")
                if match := re.match(
                    r"Script running with cell ID\s+(\S+)", _banner_text(output)
                ):
                    root_records.extend(record_groups.get(match.group(1), []))
            root_records = sorted(
                {row["_ct_record_index"]: row for row in root_records}.values(),
                key=lambda row: row["_ct_record_index"],
            )
            value = getattr(item, "input", None)
            if (
                item.kind != "command_execution"
                and isinstance(value, str)
                and not (
                    session.vendor.value == "codex_cli" and item.tool_name == "exec"
                )
            ):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
            try:
                text, field, language = _native_command_source(
                    item, root_records, None
                ) or _extract_source(item, None, session.vendor)
            except DocumentError:
                continue
            if not root_records:
                continue
            evidence = ToolSourceEvidence(
                canonical_session=session,
                full_session=session,
                item=item,
                source_text=text,
                source_field=field,
                language=language,
                source_log=source,
                source_log_sha256=snapshot_digest,
                records=root_records,
            )
            reference = source_reference(evidence)
            # Shell operations are direct invocations with source attachments;
            # a source-text argument does not by itself prove orchestration.
            if language == "shell" or field in {"cmd", "command", "shell", "source"}:
                direct_sources[str(item.item_id)] = reference
                continue
            script = build_tool_script(evidence, reference)
            if language == "javascript":
                attach_process_traces(script, text, origins)
            scripts[str(item.item_id)] = script
            owned.update(script.item_ids)
    for turn in session.turns:
        units = []
        for item in turn.items:
            identity = str(item.item_id)
            if identity in scripts:
                units.append(scripts[identity])
            elif identity not in owned:
                reference = direct_sources.get(identity)
                units.append(
                    DirectItemReference(
                        unit_id=identity,
                        item_id=identity,
                        source=reference,
                        open_source_link=reference.link if reference else None,
                    )
                )
        turn.execution_units = units
    return session
