from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Session,
    Step,
    StepTextItem,
    StepToolItem,
    SessionGraph,
    ToolStatus,
    Turn,
    Vendor,
)
from coding_trajectory.metrics import build_session_graph_metrics


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def test_metrics_roll_up_claude_step_usage() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    step_id = uuid4()

    step = Step(
        step_id=step_id,
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CLAUDE_CODE,
        items=[StepTextItem(text="done")],
        vendor_data={
            "metrics": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 40,
                },
            },
        },
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_metrics(session_graph)

    assert result["token_usage"]["input_tokens"] == 10
    assert result["token_usage"]["cache_creation_input_tokens"] == 20
    assert result["token_usage"]["cache_read_input_tokens"] == 30
    assert result["token_usage"]["output_tokens"] == 40
    assert result["cost"] == 0.000714
    assert result["extra_billing"] is False
    turn_metrics = result["sessions"][0]["turns"][0]
    assert turn_metrics["started_at"] == "2026-01-01T00:00:00Z"
    assert turn_metrics["completed_at"] is None
    assert turn_metrics["model"] == "claude-sonnet-4-6"
    assert "step_ids" not in turn_metrics
    assert turn_metrics["steps"] == [
        {
            "step_id": str(step_id),
            "sequence": 0,
            "kind": "response",
            "usage_metrics": {
                "token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 0,
                },
            },
        }
    ]
    assert turn_metrics["cost"] == 0.000714
    assert turn_metrics["extra_billing"] is False


def test_metrics_include_tool_duration_when_tool_events_are_paired() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    requested_id = uuid4()
    completed_id = uuid4()

    requested = Event(
        event_id=requested_id,
        session_id=session_id,
        timestamp=_ts(1),
        type=EventType.TOOL_CALL_REQUESTED,
        vendor_source=Vendor.CLAUDE_CODE,
        payload={"tool_call_id": "tool-1", "tool_name": "Read"},
    )
    completed = Event(
        event_id=completed_id,
        session_id=session_id,
        timestamp=_ts(3),
        type=EventType.TOOL_CALL_SUCCEEDED,
        vendor_source=Vendor.CLAUDE_CODE,
        payload={"tool_call_id": "tool-1", "tool_name": "Read"},
    )
    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CLAUDE_CODE,
        items=[StepToolItem(tool_name="Read", tool_call_id="tool-1", status=ToolStatus.COMPLETED)],
        event_ids=[requested_id, completed_id],
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        events=[requested, completed],
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_metrics(session_graph)

    step_metrics = result["sessions"][0]["turns"][0]["steps"][0]
    assert step_metrics["kind"] == "tool"
    assert step_metrics["tool_metrics"] == {"tool_count": 1, "duration_ms": 2000}
    assert "usage_metrics" not in step_metrics


def test_metrics_extract_codex_token_count_events_and_dedupe_snapshots() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    event_id_1 = uuid4()
    event_id_2 = uuid4()

    payload = {
        "raw_type": "token_count",
        "metrics": {
            "model": "gpt-5.5",
            "total_token_usage": {
                "input_tokens": 100,
                "cached_input_tokens": 25,
                "output_tokens": 10,
                "reasoning_output_tokens": 3,
                "total_tokens": 110,
            },
            "last_token_usage": {
                "input_tokens": 100,
                "cached_input_tokens": 25,
                "output_tokens": 10,
                "reasoning_output_tokens": 3,
                "total_tokens": 110,
            },
            "model_context_window": 258400,
        },
        "quota": {
            "limit_id": "codex",
            "plan_type": "plus",
            "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1777583539},
            "secondary": {"used_percent": 43.0, "window_minutes": 10080, "resets_at": 1777966380},
        },
    }
    event_1 = Event(
        event_id=event_id_1,
        session_id=session_id,
        timestamp=_ts(1),
        type=EventType.VENDOR_RAW,
        vendor_source=Vendor.CODEX_CLI,
        actor="assistant",
        payload=payload,
    )
    event_2 = event_1.model_copy(update={"event_id": event_id_2, "timestamp": _ts(2)})
    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(0),
        vendor=Vendor.CODEX_CLI,
        event_ids=[event_id_1, event_id_2],
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        event_ids=[event_id_1, event_id_2],
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        events=[event_1, event_2],
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_metrics(session_graph)

    assert result["token_usage"]["input_tokens"] == 100
    assert result["token_usage"]["cached_input_tokens"] == 25
    assert result["token_usage"]["output_tokens"] == 10
    assert result["token_usage"]["reasoning_output_tokens"] == 3
    turn_metrics = result["sessions"][0]["turns"][0]
    assert turn_metrics["model"] == "gpt-5.5"
    assert "step_ids" not in turn_metrics
    assert turn_metrics["steps"][0]["step_id"] == str(step.step_id)
    assert turn_metrics["steps"][0]["kind"] == "response"
    assert turn_metrics["steps"][0]["usage_metrics"]["token_usage"]["input_tokens"] == 100
    assert result["cost"] == 0.0006875


def test_metrics_can_mark_cost_as_extra_billing() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()

    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(1),
        vendor=Vendor.CODEX_CLI,
        vendor_data={
            "metrics": {
                "model": "gpt-5.4",
                "usage": {
                    "input_tokens": 1_000_000,
                    "cached_input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                },
            },
        },
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_metrics(
        session_graph,
        extra_billing=True,
    )

    assert result["cost"] == 15.25
    assert result["extra_billing"] is True
    assert result["sessions"][0]["turns"][0]["extra_billing"] is True


def test_metrics_compute_codex_deltas_from_cumulative_totals() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    event_id_1 = uuid4()
    event_id_2 = uuid4()

    event_1 = Event(
        event_id=event_id_1,
        session_id=session_id,
        timestamp=_ts(1),
        type=EventType.VENDOR_RAW,
        vendor_source=Vendor.CODEX_CLI,
        actor="assistant",
        payload={
            "raw_type": "token_count",
            "metrics": {
                "model": "openai/gpt-5.4-2026-01-01",
                "total_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                    "total_tokens": 110,
                },
            },
        },
    )
    event_2 = event_1.model_copy(
        update={
            "event_id": event_id_2,
            "timestamp": _ts(2),
            "payload": {
                "raw_type": "token_count",
                "metrics": {
                    "model": "openai/gpt-5.4-2026-01-01",
                    "total_token_usage": {
                        "input_tokens": 175,
                        "cached_input_tokens": 40,
                        "output_tokens": 25,
                        "total_tokens": 200,
                    },
                },
            },
        }
    )
    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(0),
        vendor=Vendor.CODEX_CLI,
        event_ids=[event_id_1, event_id_2],
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        events=[event_1, event_2],
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_metrics(session_graph)

    assert result["token_usage"]["input_tokens"] == 175
    assert result["token_usage"]["cached_input_tokens"] == 40
    assert result["token_usage"]["output_tokens"] == 25
    assert result["cost"] == 0.0007225
    turn_metrics = result["sessions"][0]["turns"][0]
    assert turn_metrics["model"] == "openai/gpt-5.4-2026-01-01"


def test_metrics_subtract_codex_parent_totals_for_forked_sessions() -> None:
    root_session_id = uuid4()
    parent_session_id = uuid4()
    child_session_id = uuid4()
    parent_turn_id = uuid4()
    child_turn_id = uuid4()
    parent_event_id = uuid4()
    child_event_id = uuid4()

    parent_event = Event(
        event_id=parent_event_id,
        session_id=parent_session_id,
        timestamp=_ts(1),
        type=EventType.VENDOR_RAW,
        vendor_source=Vendor.CODEX_CLI,
        actor="assistant",
        payload={
            "raw_type": "token_count",
            "metrics": {
                "model": "gpt-5.4",
                "total_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                    "total_tokens": 110,
                },
            },
        },
    )
    child_event = Event(
        event_id=child_event_id,
        session_id=child_session_id,
        timestamp=_ts(3),
        type=EventType.VENDOR_RAW,
        vendor_source=Vendor.CODEX_CLI,
        actor="assistant",
        payload={
            "raw_type": "token_count",
            "metrics": {
                "model": "gpt-5.4",
                "total_token_usage": {
                    "input_tokens": 130,
                    "cached_input_tokens": 25,
                    "output_tokens": 20,
                    "total_tokens": 150,
                },
            },
        },
    )
    parent_step = Step(
        session_id=parent_session_id,
        turn_id=parent_turn_id,
        sequence=0,
        timestamp=_ts(0),
        vendor=Vendor.CODEX_CLI,
        event_ids=[parent_event_id],
    )
    child_step = Step(
        session_id=child_session_id,
        turn_id=child_turn_id,
        sequence=0,
        timestamp=_ts(2),
        vendor=Vendor.CODEX_CLI,
        event_ids=[child_event_id],
    )
    parent_session = Session(
        session_id=parent_session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        events=[parent_event],
        turns=[
            Turn(
                session_id=parent_session_id,
                turn_id=parent_turn_id,
                sequence=0,
                started_at=_ts(0),
                steps=[parent_step],
            )
        ],
    )
    child_session = Session(
        session_id=child_session_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(2),
        parent_session_id=parent_session_id,
        events=[child_event],
        turns=[
            Turn(
                session_id=child_session_id,
                turn_id=child_turn_id,
                sequence=0,
                started_at=_ts(2),
                steps=[child_step],
            )
        ],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[parent_session, child_session])

    result = build_session_graph_metrics(session_graph)

    child_metrics = result["sessions"][1]["turns"][0]
    assert child_metrics["token_usage"]["input_tokens"] == 30
    assert child_metrics["token_usage"]["cached_input_tokens"] == 5
    assert child_metrics["token_usage"]["output_tokens"] == 10
    assert result["token_usage"]["input_tokens"] == 130
    assert result["token_usage"]["cached_input_tokens"] == 25
    assert result["token_usage"]["output_tokens"] == 20


def test_metrics_turns_include_started_at_and_completed_at() -> None:
    root_session_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()

    step = Step(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        timestamp=_ts(5),
        vendor=Vendor.CLAUDE_CODE,
        items=[StepTextItem(text="done")],
        vendor_data={
            "metrics": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            },
        },
    )
    turn = Turn(
        session_id=session_id,
        turn_id=turn_id,
        sequence=0,
        started_at=_ts(0),
        ended_at=_ts(10),
        steps=[step],
    )
    session = Session(
        session_id=session_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    session_graph = SessionGraph(root_session_id=root_session_id, sessions=[session])

    result = build_session_graph_metrics(session_graph)

    turn_metrics = result["sessions"][0]["turns"][0]
    assert turn_metrics["started_at"] == "2026-01-01T00:00:00Z"
    assert turn_metrics["completed_at"] == "2026-01-01T00:00:10Z"
