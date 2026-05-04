"""Amp adapter — reads ~/.local/share/amp/threads/T-*.json and normalises to canonical models."""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.common import parse_iso_timestamp, parse_timestamp
from coding_trajectory.ingestion.models import (
    AmpExtensions,
    Session,
    ToolStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord, events_from_transcript, project_transcript

logger = logging.getLogger(__name__)

_DEFAULT_AMP_THREADS_DIR = Path.home() / ".local" / "share" / "amp" / "threads"


def _content_text(blocks: list[dict]) -> str | None:
    texts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = " ".join(text for text in texts if text).strip()
    return joined or None


def _thinking_blocks(blocks: list[dict]) -> list[str]:
    return [
        block.get("thinking", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking")
    ]


class AmpAdapter(BaseAdapter):
    """Normalise Amp thread JSON files into canonical Session objects."""

    vendor = Vendor.AMP
    _file_glob = "T-*.json"

    _current_thread: dict | None

    def _reset_ingest_state(self) -> None:
        self._current_thread = None

    def _load_records(self, path: Path) -> list[dict]:
        try:
            thread = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.warn(f"AmpAdapter: could not parse {path}: {exc}", stacklevel=2)
            return []
        return [thread] if isinstance(thread, dict) else []

    def ingest_default(self) -> list[Session]:
        return self.ingest_directory(_DEFAULT_AMP_THREADS_DIR)

    def _build_session(self, source: Path, records: list[dict]) -> Session:
        if not records:
            raise ValueError(f"AmpAdapter: no valid records found in {source}")

        thread = records[0]
        self._current_thread = thread

        thread_id = thread.get("id")
        try:
            session_id = UUID(str(thread_id).removeprefix("T-"))
        except ValueError:
            session_id = uuid4()

        parent_thread_id_raw = thread.get("parentId")
        parent_session_id: UUID | None = None
        if parent_thread_id_raw:
            try:
                parent_session_id = UUID(str(parent_thread_id_raw).removeprefix("T-"))
            except ValueError:
                pass

        created_at = parse_timestamp(thread.get("created")) or datetime.now(timezone.utc)
        messages = thread.get("messages") or []
        traces = ((thread.get("meta") or {}).get("traces") or [])
        message_timestamps = self._build_message_timestamps(
            thread_created=created_at,
            messages=messages,
            traces=traces,
        )

        transcript = self._build_transcript(messages, message_timestamps)
        events = events_from_transcript(session_id=session_id, records=transcript)
        ended_at = max((record.timestamp for record in transcript), default=created_at)
        turns = project_transcript(
            session_id=session_id,
            vendor=Vendor.AMP,
            records=transcript,
        )

        return Session(
            session_id=session_id,
            trajectory_id=uuid4(),
            vendor=self.vendor,
            started_at=created_at,
            ended_at=ended_at,
            parent_session_id=parent_session_id,
            events=events,
            turns=turns,
            extensions=self._parse_extensions(thread),
        )

    def _build_message_timestamps(
        self,
        *,
        thread_created: datetime,
        messages: list[dict],
        traces: list[dict],
    ) -> list[datetime]:
        trace_by_message = self._trace_timestamps_by_message_id(traces)
        timestamps: list[datetime] = []
        last_ts: datetime | None = None

        for msg in messages:
            meta = msg.get("meta") or {}
            message_id = msg.get("messageId")

            explicit = parse_timestamp(meta.get("sentAt"))
            inferred = explicit or trace_by_message.get(message_id)
            if inferred is None:
                inferred = last_ts or thread_created

            if last_ts is not None and inferred <= last_ts:
                inferred = last_ts + timedelta(milliseconds=1)

            timestamps.append(inferred)
            last_ts = inferred

        return timestamps

    def _trace_timestamps_by_message_id(self, traces: list[dict]) -> dict[Any, datetime]:
        by_message_id: dict[Any, datetime] = {}
        for trace in traces:
            if not isinstance(trace, dict):
                continue

            context = trace.get("context") or {}
            message_id = context.get("messageId")
            if message_id is None:
                continue

            trace_ts = (
                parse_iso_timestamp(trace.get("startTime"))
                or parse_iso_timestamp(trace.get("endTime"))
            )
            if trace_ts is None:
                continue

            existing = by_message_id.get(message_id)
            if existing is None or trace_ts < existing:
                by_message_id[message_id] = trace_ts

        return by_message_id

    def _build_transcript(
        self,
        messages: list[dict],
        message_timestamps: list[datetime],
    ) -> list[TranscriptRecord]:
        """Extract only CT-useful transcript facts from Amp messages."""
        tool_id_to_name: dict[str, str] = {}
        for msg in messages:
            if msg.get("role") == "assistant":
                for block in (msg.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id_to_name[block.get("id") or ""] = block.get("name") or ""

        transcript: list[TranscriptRecord] = []
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            message_id = msg.get("messageId")
            content = msg.get("content") or []
            ts = message_timestamps[idx]

            if role == "user":
                text = _content_text(content)
                tool_results = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]

                if text and not tool_results:
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.AMP,
                            role="user",
                            kind="user_message",
                            data={"message_id": message_id, "text": text},
                        )
                    )

                elif tool_results:
                    for block in tool_results:
                        run = block.get("run") or {}
                        status = run.get("status")
                        tool_id = block.get("toolUseID") or ""
                        transcript.append(
                            TranscriptRecord(
                                sequence=len(transcript),
                                timestamp=ts,
                                vendor=Vendor.AMP,
                                role="tool",
                                kind="tool_result",
                                data={
                                    "message_id": message_id,
                                    "tool_call_id": tool_id,
                                    "tool_name": tool_id_to_name.get(tool_id, ""),
                                    "output": run.get("result"),
                                    "status": (
                                        ToolStatus.COMPLETED.value
                                        if status == "done"
                                        else ToolStatus.FAILED.value
                                    ),
                                    "attach_to_previous_step": True,
                                },
                                fidelity="synthetic",
                            )
                        )

            elif role == "assistant":
                usage = msg.get("usage") or {}
                thinking = _thinking_blocks(content)

                vendor_data: dict = {}
                if thinking:
                    vendor_data["thinking"] = thinking
                if usage:
                    vendor_data["model"] = usage.get("model")
                    vendor_data["input_tokens"] = usage.get("inputTokens")
                    vendor_data["output_tokens"] = usage.get("outputTokens")

                stop_reason = (msg.get("state") or {}).get("stopReason")
                if stop_reason:
                    vendor_data["stop_reason"] = stop_reason

                tool_blocks = [
                    block
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                ]
                transcript.append(
                    TranscriptRecord(
                        sequence=len(transcript),
                        timestamp=ts,
                        vendor=Vendor.AMP,
                        role="assistant",
                        kind="assistant_message",
                        data={
                            "text": _content_text(content),
                            "message_id": message_id,
                            "vendor_data": {k: v for k, v in vendor_data.items() if v is not None},
                            "flush_after": not tool_blocks,
                        },
                    )
                )

                for index, block in enumerate(tool_blocks):
                    tool_id = block.get("id")
                    transcript.append(
                        TranscriptRecord(
                            sequence=len(transcript),
                            timestamp=ts,
                            vendor=Vendor.AMP,
                            role="assistant",
                            kind="tool_call",
                            data={
                                "message_id": message_id,
                                "tool_name": block.get("name"),
                                "tool_call_id": tool_id,
                                "input": block.get("input"),
                                "flush_after": index == len(tool_blocks) - 1,
                            },
                        )
                    )

        return transcript

    def _parse_extensions(self, thread: dict) -> VendorExtensions | None:
        env = (thread.get("env") or {}).get("initial") or {}
        trees = env.get("trees") or []
        platform = env.get("platform") or {}
        first_tree = trees[0] if trees else {}
        repo = first_tree.get("repository") or {}
        return VendorExtensions(
            amp=AmpExtensions(
                thread_id=thread.get("id"),
                thread_version=thread.get("v"),
                parent_thread_id=thread.get("parentId"),
                workspace_id=first_tree.get("uri"),
                workspace_name=first_tree.get("displayName"),
                git_url=repo.get("url"),
                git_ref=repo.get("ref"),
                agent_version=platform.get("clientVersion"),
                client_type=platform.get("clientType"),
                os_platform=platform.get("os"),
                title=thread.get("title"),
                agent_mode=thread.get("agentMode"),
            )
        )
