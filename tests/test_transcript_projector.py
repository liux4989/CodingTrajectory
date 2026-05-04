from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from coding_trajectory.ingestion.models import StepToolItem, ToolStatus, Vendor
from coding_trajectory.ingestion.transcript import TranscriptRecord, project_transcript


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def test_projector_updates_tool_result_on_previous_flushed_step() -> None:
    session_id = uuid4()
    records = [
        TranscriptRecord(
            sequence=0,
            timestamp=_ts(0),
            vendor=Vendor.AMP,
            role="user",
            kind="user_message",
            data={"text": "inspect"},
        ),
        TranscriptRecord(
            sequence=1,
            timestamp=_ts(1),
            vendor=Vendor.AMP,
            role="assistant",
            kind="assistant_message",
            data={"text": "I will read it."},
        ),
        TranscriptRecord(
            sequence=2,
            timestamp=_ts(1),
            vendor=Vendor.AMP,
            role="assistant",
            kind="tool_call",
            data={
                "tool_name": "read",
                "tool_call_id": "tool-1",
                "input": {"path": "README.md"},
                "flush_after": True,
            },
        ),
        TranscriptRecord(
            sequence=3,
            timestamp=_ts(2),
            vendor=Vendor.AMP,
            role="tool",
            kind="tool_result",
            data={
                "tool_call_id": "tool-1",
                "output": "contents",
                "status": ToolStatus.COMPLETED.value,
                "attach_to_previous_step": True,
            },
        ),
    ]

    turns = project_transcript(session_id=session_id, vendor=Vendor.AMP, records=records)

    assert len(turns) == 1
    assert len(turns[0].steps) == 1
    tool = next(item for item in turns[0].steps[0].items if isinstance(item, StepToolItem))
    assert tool.tool_call_id == "tool-1"
    assert tool.status == ToolStatus.COMPLETED
    assert tool.output == "contents"
