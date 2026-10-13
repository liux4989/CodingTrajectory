"""Resolve polling targets from recorded process IDs, never from tool names alone."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from coding_trajectory.ingestion.execution_models import (
    ProcessTrace,
    ToolScriptExecution,
)
from coding_trajectory.ingestion.source_calls import source_calls


class ProcessOrigin(BaseModel):
    process_id: str
    launch_index: int
    command: str | list[str]
    arguments: dict[str, Any]
    launch_ref: dict[str, Any]
    command_ref: dict[str, Any] | None = None
    output_ref: dict[str, Any] | None = None


def record_ref(row: dict[str, Any], source: Path, digest: str) -> dict[str, Any]:
    return {
        "source_log": str(source),
        "source_log_sha256": digest,
        "record_indices": [row["_ct_record_index"]],
        "record_digests": [row["_ct_record_sha256"]],
        "provider_call_id": row.get("payload", {}).get("call_id"),
    }


def _arguments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("arguments", payload.get("input"))
    name = str(payload.get("name", "")).rsplit(".", 1)[-1]
    if name == "exec":
        if not isinstance(value, str):
            return []
        calls, _ = source_calls(value)
        return [
            c.arguments
            for c in calls
            if c.tool_name == "exec_command"
            and c.arguments_static
            and isinstance(c.arguments, dict)
            and isinstance(c.arguments.get("cmd"), str)
        ]
    if name != "exec_command":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return (
        [value] if isinstance(value, dict) and isinstance(value.get("cmd"), str) else []
    )


def _envelopes(value: Any) -> list[dict[str, Any]]:
    # Only parse transport-level JSON text blocks. Do not mine stdout for IDs.
    blocks = value if isinstance(value, list) else [value]
    result = []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else block
        if not isinstance(text, str):
            continue
        try:
            envelope = json.loads(text)
        except ValueError:
            continue
        if (
            isinstance(envelope, dict)
            and "chunk_id" in envelope
            and "wall_time_seconds" in envelope
        ):
            result.append(envelope)
    return result


def process_origins(
    records: list[dict[str, Any]],
    source: Path,
    digest: str,
    native_parents: dict[str, str],
) -> dict[str, list[ProcessOrigin]]:
    inputs = {
        row["payload"]["call_id"]: row
        for row in records
        if isinstance(row.get("payload"), dict)
        and row["payload"].get("type") in {"function_call", "custom_tool_call"}
        and isinstance(row["payload"].get("call_id"), str)
    }
    native: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in records:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CommandExecution":
            continue
        parent = native_parents.get(item.get("id"))
        if parent and item.get("process_id") is not None:
            native.setdefault((parent, str(item["process_id"])), []).append(row)
    result: dict[str, list[ProcessOrigin]] = {}
    for row in records:
        payload = row.get("payload", {})
        if not isinstance(payload, dict) or payload.get("type") not in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            continue
        origin = inputs.get(payload.get("call_id"))
        if origin is None:
            continue
        arguments = _arguments(origin["payload"])
        if not arguments or origin["_ct_record_index"] >= row["_ct_record_index"]:
            continue
        for envelope in _envelopes(payload.get("output")):
            identity = envelope.get("session_id")
            if isinstance(identity, bool) or not isinstance(identity, (str, int)):
                continue
            identity = str(identity)
            matches = native.get((payload["call_id"], identity), [])
            # A native process record binds the returned ID to a particular
            # command even if its completion record arrives after the launch.
            commands = {
                json.dumps(r["payload"]["item"].get("command")) for r in matches
            }
            native_ids = {r["payload"]["item"].get("id") for r in matches}
            if len(commands) == 1 and len(native_ids) == 1:
                command_row = matches[-1]
                command = command_row["payload"]["item"].get("command")
                command_text = command if isinstance(command, str) else None
                if (
                    isinstance(command, list)
                    and command
                    and isinstance(command[0], str)
                    and Path(command[0]).name
                    in {"sh", "bash", "zsh", "fish", "dash", "ksh"}
                ):
                    command_text = next(
                        (
                            command[index + 1]
                            for index, flag in enumerate(command[:-1])
                            if flag in {"-c", "-lc"}
                            and isinstance(command[index + 1], str)
                        ),
                        None,
                    )
                candidates = [a for a in arguments if a["cmd"] == command_text]
            elif not matches and len(arguments) == 1:
                origin_payload = origin["payload"]
                if str(origin_payload.get("name", "")).rsplit(".", 1)[-1] == "exec":
                    calls, _ = source_calls(origin_payload.get("input") or "")
                    if len(calls) != 1 or calls[0].tool_name != "exec_command":
                        continue
                command_row = None
                candidates = arguments
                command = arguments[0]["cmd"]
            else:
                continue
            if len(candidates) != 1 or not isinstance(command, (str, list)):
                continue
            result.setdefault(identity, []).append(
                ProcessOrigin(
                    process_id=identity,
                    launch_index=row["_ct_record_index"],
                    command=command,
                    arguments=candidates[0],
                    launch_ref={
                        **record_ref(row, source, digest),
                        "input_ref": record_ref(origin, source, digest),
                    },
                    command_ref=record_ref(command_row, source, digest)
                    if command_row
                    else None,
                    output_ref=record_ref(command_row, source, digest)
                    if command_row
                    and command_row["payload"]["item"].get("aggregated_output")
                    is not None
                    else None,
                )
            )
    return result


def attach_process_traces(
    script: ToolScriptExecution,
    source_text: str,
    origins: dict[str, list[ProcessOrigin]],
) -> None:
    candidates, _ = source_calls(source_text)
    by_offset = {c.source_byte_start: c for c in candidates}
    for call in [*script.observed_external_calls, *script.source_only_calls]:
        if call.tool_name != "write_stdin":
            continue
        offset = call.evidence_ref.get(
            "source_byte_start", call.evidence_ref.get("source_offset")
        )
        candidate = by_offset.get(offset)
        if (
            candidate is None
            or not candidate.arguments_static
            or not isinstance(candidate.arguments, dict)
        ):
            continue
        arguments = candidate.arguments
        identity = arguments.get("session_id")
        if isinstance(identity, bool) or not isinstance(identity, (str, int)):
            continue
        identity = str(identity)
        if not script.source.record_indices:
            continue
        prior = [
            o
            for o in origins.get(identity, [])
            if o.launch_index < min(script.source.record_indices)
        ]
        latest = max((o.launch_index for o in prior), default=-1)
        matches = [o for o in prior if o.launch_index == latest]
        trace = ProcessTrace(
            process_id=identity,
            resolution="unresolved" if not matches else "ambiguous",
            poll_arguments=arguments,
            poll_output_ref={
                **script.result.emitted_output_ref,
                "scope": "script_invocation",
            }
            if script.result.emitted_output_ref
            else None,
            poll_ref={
                "source_log": script.source.source_log,
                "source_log_sha256": script.source.source_log_sha256,
                "record_indices": script.source.record_indices,
                "record_digests": script.source.record_digests,
                "provider_call_id": script.source.provider_call_id,
                **call.evidence_ref,
            },
        )
        if len(matches) == 1:
            origin = matches[0]
            trace.resolution = "resolved"
            trace.command, trace.arguments = origin.command, origin.arguments
            trace.launch_ref, trace.command_ref = origin.launch_ref, origin.command_ref
            trace.output_ref = origin.output_ref
        call.process = trace
