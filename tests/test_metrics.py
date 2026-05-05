from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.ingestion.models import (
    Event,
    EventType,
    Session,
    Step,
    StepTextItem,
    Trajectory,
    Turn,
    Vendor,
)
from coding_trajectory.metrics import build_trajectory_metrics


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def test_metrics_roll_up_claude_step_usage() -> None:
    trajectory_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()

    step = Step(
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
        trajectory_id=trajectory_id,
        vendor=Vendor.CLAUDE_CODE,
        started_at=_ts(0),
        turns=[turn],
    )
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[session])

    result = build_trajectory_metrics(trajectory)

    assert result["token_usage"]["input_tokens"] == 10
    assert result["token_usage"]["cache_creation_input_tokens"] == 20
    assert result["token_usage"]["cache_read_input_tokens"] == 30
    assert result["token_usage"]["output_tokens"] == 40
    assert result["cost_estimate"]["amount_usd"] == 0.000714
    assert result["cost_estimate"]["complete"] is True
    assert result["cost_estimate"]["extra_billing"] is False
    observation = result["sessions"][0]["turns"][0]["steps"][0]["observations"][0]
    assert observation["model"] == "claude-sonnet-4-6"
    assert observation["source"]["source_type"] == "step.vendor_data"


def test_metrics_extract_codex_token_count_events_and_dedupe_snapshots() -> None:
    trajectory_id = uuid4()
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
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        events=[event_1, event_2],
        turns=[turn],
    )
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[session])

    result = build_trajectory_metrics(trajectory)

    assert result["token_usage"]["input_tokens"] == 100
    assert result["token_usage"]["cached_input_tokens"] == 25
    assert result["token_usage"]["output_tokens"] == 10
    assert result["token_usage"]["reasoning_output_tokens"] == 3
    step_metrics = result["sessions"][0]["turns"][0]["steps"][0]
    assert len(step_metrics["observations"]) == 1
    assert step_metrics["observations"][0]["model"] == "gpt-5.5"
    assert result["cost_estimate"]["amount_usd"] == 0.0006875
    assert result["sessions"][0]["quota_snapshot"]["plan_type"] == "plus"


def test_metrics_can_mark_cost_as_extra_billing() -> None:
    trajectory_id = uuid4()
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
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        turns=[turn],
    )
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[session])

    result = build_trajectory_metrics(
        trajectory,
        extra_billing=True,
    )

    assert result["cost_estimate"]["amount_usd"] == 15.25
    assert result["cost_estimate"]["extra_billing"] is True
    assert result["sessions"][0]["turns"][0]["cost_estimate"]["extra_billing"] is True


def test_metrics_compute_codex_deltas_from_cumulative_totals() -> None:
    trajectory_id = uuid4()
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
        trajectory_id=trajectory_id,
        vendor=Vendor.CODEX_CLI,
        started_at=_ts(0),
        events=[event_1, event_2],
        turns=[turn],
    )
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[session])

    result = build_trajectory_metrics(trajectory)

    assert result["token_usage"]["input_tokens"] == 175
    assert result["token_usage"]["cached_input_tokens"] == 40
    assert result["token_usage"]["output_tokens"] == 25
    assert len(result["sessions"][0]["turns"][0]["steps"][0]["observations"]) == 2
    assert result["cost_estimate"]["model"] == "gpt-5.4"
    assert result["cost_estimate"]["amount_usd"] == 0.0007225


def test_metrics_subtract_codex_parent_totals_for_forked_sessions() -> None:
    trajectory_id = uuid4()
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
        trajectory_id=trajectory_id,
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
        trajectory_id=trajectory_id,
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
    trajectory = Trajectory(trajectory_id=trajectory_id, sessions=[parent_session, child_session])

    result = build_trajectory_metrics(trajectory)

    child_metrics = result["sessions"][1]["turns"][0]
    assert child_metrics["token_usage"]["input_tokens"] == 30
    assert child_metrics["token_usage"]["cached_input_tokens"] == 5
    assert child_metrics["token_usage"]["output_tokens"] == 10
    assert result["token_usage"]["input_tokens"] == 130
    assert result["token_usage"]["cached_input_tokens"] == 25
    assert result["token_usage"]["output_tokens"] == 20
