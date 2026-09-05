"""Amp live-plugin journal adapter. No CLI export or provider-usage inference."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from uuid import UUID

from pydantic import BaseModel, Field

from coding_trajectory.ingestion.adapters.base import BaseAdapter, SessionHeader
from coding_trajectory.ingestion.assembly import AssemblyHooks, assemble_session
from coding_trajectory.ingestion.models import (
    AmpExtensions,
    Session,
    SessionStatus,
    TurnStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.provenance import RecordSpan
from coding_trajectory.ingestion.retention import CanonicalRetention
from coding_trajectory.ingestion.transcript import TranscriptRecord


def amp_session_id(thread_id: str) -> UUID:
    if not thread_id.startswith("T-"):
        raise ValueError("Amp thread identity must start with T-")
    return UUID(thread_id[2:])


class AmpRecord(BaseModel):
    schema_version: Literal[1]
    type: Literal["thread", "message", "observation"]
    captured_at: datetime
    thread_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    message: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None
    event: str | None = None
    message_id: str | int | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    input: Any = None
    output: Any = None
    status: str | None = None


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


class AmpAdapter(BaseAdapter):
    vendor = Vendor.AMP

    def scan_header(self, source: Path) -> SessionHeader | None:
        return self.scan_identity_records(source, self._iter_records(source))

    def scan_identity_records(
        self, source: Path, records: Iterable[dict]
    ) -> SessionHeader | None:
        for raw in records:
            if raw.get("type") != "thread":
                continue
            record = AmpRecord.model_validate(raw)
            payload = record.payload
            identity = amp_session_id(payload["id"])
            uri = urlparse(payload.get("workspace_root") or "")
            cwd = unquote(uri.path) if uri.scheme == "file" else None
            return SessionHeader(
                session_id=identity,
                vendor=Vendor.AMP,
                title=payload.get("title"),
                cwd=cwd,
            )
        return None

    def _build_session(
        self,
        source: Path,
        records: Iterable[tuple[dict, RecordSpan | None]],
        *,
        retention: CanonicalRetention = "trajectory",
    ) -> Session:
        rows = [(AmpRecord.model_validate(raw), span) for raw, span in records]
        headers = [r for r, _ in rows if r.type == "thread"]
        if not headers:
            raise ValueError("Amp journal has no thread identity")
        payload = headers[-1].payload
        thread_id = payload["id"]
        session_id = amp_session_id(thread_id)
        if any(r.payload.get("id") != thread_id for r in headers):
            raise ValueError("Amp journal contains multiple thread identities")
        if any(r.thread_id not in (None, thread_id) for r, _ in rows):
            raise ValueError("Amp record belongs to another thread")
        uri = urlparse(payload.get("workspace_root") or "")
        cwd = unquote(uri.path) if uri.scheme == "file" else None

        # Preserve first observation times and insertion order; latest message
        # revisions replace bodies rather than becoming additional activity.
        messages: dict[str, tuple[AmpRecord, RecordSpan | None, datetime]] = {}
        observations: dict[
            tuple[str | None, str], tuple[AmpRecord, RecordSpan | None]
        ] = {}
        for record, span in rows:
            if record.type == "message":
                key = str(record.message["id"])
                first = messages[key][2] if key in messages else record.captured_at
                messages[key] = (record, span, first)
            elif record.type == "observation":
                key = (record.event, str(record.tool_use_id or record.message_id))
                observations.setdefault(key, (record, span))

        transcript: list[TranscriptRecord] = []
        calls: dict[str, dict[str, Any]] = {}
        results: dict[str, dict[str, Any]] = {}
        lifecycle = [
            record
            for record, _ in observations.values()
            if record.event in {"agent.start", "agent.end"}
        ]
        latest_lifecycle = max(
            lifecycle, key=lambda r: r.observed_at or r.captured_at, default=None
        )
        stride = max(
            (len(r.message.get("content", [])) + 2 for r, _, _ in messages.values()),
            default=2,
        )

        def emit(kind: str, role: str, ts: datetime, data: dict, span=None, order=None):
            transcript.append(
                TranscriptRecord(
                    sequence=len(transcript) if order is None else order,
                    timestamp=ts,
                    vendor=Vendor.AMP,
                    kind=kind,
                    role=role,
                    data=data,
                    fidelity="observed",
                    origin=span,
                )
            )

        for position, (key, (record, span, first)) in enumerate(messages.items()):
            order = position * stride
            message = record.message
            start = observations.get(("agent.start", key))
            ts = (start[0].observed_at or start[0].captured_at) if start else first
            blocks = message.get("content", [])
            text = "\n".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            )
            if message.get("role") == "user" and (text or start):
                emit("turn_started", "runtime", ts, {"turn_id_raw": key}, span, order)
                emit(
                    "user_message",
                    "user",
                    ts,
                    {"text": text, "cwd": cwd},
                    span,
                    order + 1,
                )
            elif message.get("role") == "assistant":
                thinking = [
                    b["thinking"] for b in blocks if b.get("type") == "thinking"
                ]
                if text or thinking:
                    emit(
                        "assistant_message",
                        "assistant",
                        first,
                        {"text": text, "vendor_data": {"thinking": thinking}},
                        span,
                        order,
                    )
            for block_index, block in enumerate(blocks, start=2):
                if block.get("type") == "tool_use":
                    calls.setdefault(
                        block["id"],
                        {
                            **block,
                            "time": first,
                            "span": span,
                            "order": order + block_index,
                        },
                    )
                elif block.get("type") == "tool_result":
                    results[block["toolUseID"]] = {
                        **block,
                        "time": first,
                        "span": span,
                        "order": order + block_index,
                    }

        # Live tool hooks are useful before the next transcript reconciliation.
        # Prefer their timestamps, but reconcile result semantics from messages.
        for (event, key), (record, span) in observations.items():
            ts = record.observed_at or record.captured_at
            if event == "tool.call":
                calls[key] = {
                    **calls.get(key, {}),
                    "name": record.tool_name,
                    "input": record.input,
                    "time": ts,
                    "span": span,
                }
            elif event == "tool.result":
                results[key] = {
                    "output": record.output,
                    "status": record.status,
                    **results.get(key, {}),
                    "time": ts,
                    "span": span,
                }
            elif event == "agent.start" and key not in messages:
                emit("turn_started", "runtime", ts, {"turn_id_raw": key}, span)
            elif event == "agent.end":
                emit(
                    "task_complete",
                    "runtime",
                    ts,
                    {
                        "status": "completed"
                        if record.status == "done"
                        else "interrupted",
                        "turn_id_raw": key,
                    },
                    span,
                    len(messages) * stride + 2,
                )

        spawn_links: dict[str, str] = {}
        conflicting_children: set[str] = set()
        for call_id, call in calls.items():
            name = call.get("name")
            emit(
                "tool_call",
                "assistant",
                call["time"],
                {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "input": call.get("input"),
                    "item_kind": "command_execution"
                    if name == "shell_command"
                    else "tool_call",
                },
                call["span"],
                call.get("order", len(messages) * stride),
            )
            result = results.get(call_id)
            if result is None or result.get("status") not in {
                "done",
                "error",
                "cancelled",
            }:
                continue
            output = result.get("output")
            obj = _object(output)
            exit_code = obj.get("exitCode", obj.get("exit_code"))
            failed = result.get("status") in {"error", "cancelled"} or (
                isinstance(exit_code, int) and exit_code != 0
            )
            emit(
                "tool_result",
                "tool",
                result["time"],
                {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "output": output,
                    "exit_code": exit_code if isinstance(exit_code, int) else None,
                    "status": "failed" if failed else "completed",
                },
                result["span"],
                result.get("order", len(messages) * stride + 1),
            )
            live_result = observations.get(("tool.result", call_id))
            creation = _object(live_result[0].output) if live_result else {}
            if (
                name == "create_thread"
                and not failed
                and ("tool.call", call_id) in observations
                and live_result
                and live_result[0].status == "done"
                and isinstance(creation.get("threadID"), str)
            ):
                try:
                    child = str(amp_session_id(creation["threadID"]))
                except ValueError:
                    continue
                if child == str(session_id):
                    continue
                if child in spawn_links and spawn_links[child] != call_id:
                    conflicting_children.add(child)
                spawn_links[child] = call_id
        for child in conflicting_children:
            spawn_links.pop(child, None)
        if not transcript:
            raise ValueError("Amp journal has no captured activity")
        transcript.sort(key=lambda r: (r.timestamp, r.sequence))
        for index, record in enumerate(transcript):
            record.sequence = index
        session = assemble_session(
            vendor=Vendor.AMP,
            source=source,
            session_id=session_id,
            transcript=transcript,
            retention=retention,
            hooks=AssemblyHooks(
                active_status=TurnStatus.RUNNING
                if latest_lifecycle and latest_lifecycle.event == "agent.start"
                else TurnStatus.INCOMPLETE,
                default_previous_turn_status=TurnStatus.INCOMPLETE,
                prefer_lifecycle=True,
                extensions=VendorExtensions(
                    amp=AmpExtensions(
                        thread_id=thread_id,
                        title=payload.get("title"),
                        spawn_links=spawn_links,
                    )
                ),
                provenance_sink=lambda p: setattr(self, "last_provenance", p),
            ),
        )
        session.cwd = cwd
        session.status = (
            SessionStatus.LIVING
            if session.turns and session.turns[-1].status == TurnStatus.RUNNING
            else SessionStatus.NOT_LIVING
        )
        if session.status == SessionStatus.LIVING:
            session.ended_at = None
            session.turns[-1].ended_at = None
        return session
