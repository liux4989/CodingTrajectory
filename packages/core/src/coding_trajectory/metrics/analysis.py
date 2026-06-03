"""Build derived execution metrics from canonical session_graphs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from coding_trajectory.ingestion.indexes import (
    SessionGraphIndex,
    build_session_graph_index,
    events_for_step,
    events_for_turn,
)
from coding_trajectory.ingestion.models import Event, EventType, Session, Step, SessionGraph, Turn, Vendor
from coding_trajectory.metrics.models import (
    ActivityUsageBreakdownFlat,
    CostEstimate,
    MetricSource,
    QuotaSnapshot,
    QuotaWindow,
    ContextCategoryFlat,
    ContextModelStatsFlat,
    ContextWindowStatsFlat,
    MessageStatsFlat,
    QuotaStatsFlat,
    SessionMetrics,
    SessionMetricsFlat,
    SessionContextStatsFlat,
    RuntimeStatsFlat,
    SessionUsageCompactFlat,
    StepMetrics,
    StepMetricsFlat,
    ToolOutputUsageFlat,
    ToolStepUsageFlat,
    TokenUsage,
    TokenUsageObservation,
    SessionGraphMetrics,
    SessionGraphMetricsFlat,
    SessionGraphToolUsageFlat,
    TurnUsageCompactFlat,
    TurnMetrics,
    TurnMetricsFlat,
)
from coding_trajectory.metrics.pricing import estimate_observation_cost


@dataclass
class _CodexUsageState:
    previous_totals: TokenUsage | None = None
    remaining_inherited_totals: TokenUsage | None = None
    seen_totals: set[tuple[int, int, int, int, int]] | None = None


def build_session_graph_metrics(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
    include_steps: bool = False,
) -> dict[str, Any]:
    """Return a flat usage summary: sessions → turns, optionally with step deltas."""
    full = _build_full_metrics(session_graph, extra_billing=extra_billing)
    sessions_flat: list[SessionMetricsFlat] = []

    for session in full.sessions:
        turns_flat: list[TurnMetricsFlat] = []
        for turn in session.turns:
            model = _turn_model(turn)
            turns_flat.append(
                TurnMetricsFlat(
                    turn_id=turn.turn_id,
                    sequence=turn.sequence,
                    status=turn.status,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    model=model,
                    token_usage=turn.token_usage,
                    cost=turn.cost_estimate.amount_usd,
                    extra_billing=turn.cost_estimate.extra_billing,
                    steps=_turn_step_deltas(turn) if include_steps else None,
                )
            )
        sessions_flat.append(
            SessionMetricsFlat(
                session_id=session.session_id,
                vendor=session.vendor,
                status=session.status,
                token_usage=session.token_usage,
                cost=session.cost_estimate.amount_usd,
                extra_billing=session.cost_estimate.extra_billing,
                turns=turns_flat,
            )
        )

    return SessionGraphMetricsFlat(
        root_session_id=full.root_session_id,
        token_usage=full.token_usage,
        cost=full.cost_estimate.amount_usd,
        extra_billing=full.cost_estimate.extra_billing,
        sessions=sessions_flat,
        warnings=full.warnings,
    ).model_dump(mode="json")


def build_session_graph_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    """Return provider-specific context-window stats.

    Only Codex currently preserves enough bootstrap prompt text for this view.
    Other providers should add their own prompt-block preservation and categorizer
    before being enabled here.
    """
    vendors = {session.vendor for session in session_graph.sessions}
    if vendors != {Vendor.CODEX_CLI}:
        names = ", ".join(sorted(vendor.value for vendor in vendors))
        raise NotImplementedError(f"session stats context breakdown is only implemented for codex_cli; got: {names}")

    root = _root_session(session_graph)
    latest_usage_event = _latest_codex_token_count_event(session_graph)
    latest_metrics = latest_usage_event.payload.get("metrics") if latest_usage_event is not None else None
    latest_metrics = latest_metrics if isinstance(latest_metrics, dict) else {}
    latest_usage = _token_usage_from_mapping(
        latest_metrics.get("last_token_usage") if isinstance(latest_metrics.get("last_token_usage"), dict) else {}
    )
    context_window = _as_int(latest_metrics.get("model_context_window"))
    if context_window == 0:
        context_window = _latest_task_started_context_window(session_graph) or 0

    categories = _codex_context_categories(session_graph, latest_usage.input_tokens, context_window)
    quota = _quota_stats_from_latest_event(latest_usage_event)
    runtime = _runtime_stats(session_graph)
    messages = _message_stats(session_graph)

    warnings = [
        "Category token counts are estimated from Codex JSONL prompt text, not vendor category attribution.",
    ]
    if not categories:
        warnings.append("No Codex prompt blocks were found for context category breakdown.")

    return SessionContextStatsFlat(
        root_session_id=session_graph.root_session_id,
        vendor=Vendor.CODEX_CLI.value,
        model=ContextModelStatsFlat(
            name=_codex_model(session_graph),
            context_window_tokens=context_window or None,
        ),
        context_window=ContextWindowStatsFlat(
            used_tokens=latest_usage.input_tokens,
            used_percent=_percent(latest_usage.input_tokens, context_window),
            source="latest_token_count",
            categories=categories,
        ),
        runtime=runtime,
        messages=messages,
        usage=latest_usage,
        quota=quota,
        warnings=warnings,
    ).model_dump(mode="json")


def build_session_graph_usage(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Return compact turn-level usage and cost accounting."""
    full = _build_full_metrics(session_graph, extra_billing=extra_billing)
    multi_session = len(full.sessions) > 1
    turns: list[TurnUsageCompactFlat] = []

    for session in full.sessions:
        for turn in session.turns:
            if turn_id is not None and str(turn.turn_id) != turn_id:
                continue
            turns.append(_compact_turn_usage(turn, session_id=session.session_id if multi_session else None))

    return SessionUsageCompactFlat(
        session_id=full.root_session_id,
        extra_billing=full.cost_estimate.extra_billing,
        turns=turns,
        total_usage=full.token_usage,
        cost_usd=full.cost_estimate.amount_usd,
        warnings=full.warnings,
    ).model_dump(mode="json")


def _root_session(session_graph: SessionGraph) -> Session:
    for session in session_graph.sessions:
        if session.session_id == session_graph.root_session_id:
            return session
    return session_graph.sessions[0]


def _latest_codex_token_count_event(session_graph: SessionGraph) -> Event | None:
    events = [
        event
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI
        and event.type == EventType.VENDOR_RAW
        and event.payload.get("raw_type") == "token_count"
    ]
    return max(events, key=lambda item: item.timestamp) if events else None


def _latest_task_started_context_window(session_graph: SessionGraph) -> int | None:
    values = [
        _as_int(event.payload.get("model_context_window"))
        for session in session_graph.sessions
        for event in session.events
        if event.vendor_source == Vendor.CODEX_CLI
        and event.type == EventType.VENDOR_RAW
        and event.payload.get("raw_type") == "task_started"
    ]
    values = [value for value in values if value > 0]
    return values[-1] if values else None


def _codex_model(session_graph: SessionGraph) -> str | None:
    for session in session_graph.sessions:
        for event in sorted(session.events, key=lambda item: item.timestamp):
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.payload.get("raw_type") != "token_count":
                continue
            metrics = event.payload.get("metrics")
            if not isinstance(metrics, dict):
                continue
            model = _as_str(metrics.get("model"))
            if model:
                return model
    for session in session_graph.sessions:
        for turn in session.turns:
            model = _turn_model(turn)
            if model:
                return model
    return None


def _codex_context_categories(
    session_graph: SessionGraph,
    used_tokens: int,
    context_window: int,
) -> list[ContextCategoryFlat]:
    buckets: dict[str, dict[str, Any]] = {}
    for session in session_graph.sessions:
        for event in session.events:
            if event.vendor_source != Vendor.CODEX_CLI:
                continue
            if event.type != EventType.VENDOR_RAW or event.payload.get("raw_type") != "prompt_block":
                continue
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            key, label = _codex_prompt_category(event.payload)
            bucket = buckets.setdefault(
                key,
                {"label": label, "tokens": 0, "source": set(), "children": {}},
            )
            tokens = _estimate_text_tokens(text)
            bucket["tokens"] += tokens
            source = event.payload.get("prompt_block")
            if isinstance(source, str) and source:
                bucket["source"].add(source)
                child = bucket["children"].setdefault(source, {"tokens": 0, "label": _prompt_block_label(source)})
                child["tokens"] += tokens

    denominator = context_window or used_tokens
    categories = [
        ContextCategoryFlat(
            key=key,
            label=value["label"],
            tokens=int(value["tokens"]),
            percent=_percent(int(value["tokens"]), denominator),
            confidence="estimated_tokens",
            source=", ".join(sorted(value["source"])) if value["source"] else None,
            children=[
                ContextCategoryFlat(
                    key=f"{key}.{child_key}",
                    label=child["label"],
                    tokens=int(child["tokens"]),
                    percent=_percent(int(child["tokens"]), denominator),
                    confidence="estimated_tokens",
                    source=child_key,
                )
                for child_key, child in sorted(value["children"].items())
            ],
        )
        for key, value in sorted(buckets.items(), key=lambda item: _category_sort_key(item[0]))
        if int(value["tokens"]) > 0
    ]
    prompt_tokens = sum(category.tokens for category in categories)
    message_tokens = max(used_tokens - prompt_tokens, 0)
    if message_tokens:
        categories.append(
            ContextCategoryFlat(
                key="messages",
                label="Messages",
                tokens=message_tokens,
                percent=_percent(message_tokens, denominator),
                confidence="estimated_tokens",
                source="latest_input_minus_prompt_blocks",
            )
        )
    return categories


def _codex_prompt_category(payload: dict[str, Any]) -> tuple[str, str]:
    block = _as_str(payload.get("prompt_block")) or ""
    text = payload.get("text")
    haystack = f"{block}\n{text if isinstance(text, str) else ''}".lower()
    if block == "base_instructions":
        return "system_prompt", "System prompt"
    if "agents.md" in haystack:
        return "agents_md", "AGENTS.md files"
    if "skills_instructions" in block or "### available skills" in haystack:
        return "skills", "Skills"
    if "plugins_instructions" in block or "### available plugins" in haystack:
        return "plugins", "Plugins"
    if "memory_summary" in haystack or "memory layout" in haystack or "## memory" in haystack:
        return "memory", "Memory"
    if "mcp" in haystack or "tools are grouped" in haystack:
        return "tools", "Tool definitions"
    return "developer_context", "Developer/app context"


def _prompt_block_label(block: str) -> str:
    if block == "base_instructions":
        return "base instructions"
    return block.replace("_", " ")


def _category_sort_key(key: str) -> int:
    order = {
        "system_prompt": 0,
        "developer_context": 1,
        "agents_md": 2,
        "skills": 3,
        "plugins": 4,
        "tools": 5,
        "memory": 6,
        "messages": 7,
    }
    return order.get(key, 99)


def _estimate_text_tokens(text: str) -> int:
    return max(round(len(text) / 4), 1) if text else 0


def _runtime_stats(session_graph: SessionGraph) -> RuntimeStatsFlat:
    started = min((session.started_at for session in session_graph.sessions), default=None)
    ended = max((session.ended_at or session.started_at for session in session_graph.sessions), default=None)
    status = _root_session(session_graph).status.value if _root_session(session_graph).status else None
    tool_calls = sum(
        1
        for session in session_graph.sessions
        for event in session.events
        if event.type == EventType.TOOL_CALL_REQUESTED
    )
    compactions = sum(
        1
        for session in session_graph.sessions
        for event in session.events
        if event.payload.get("raw_type") == "context_compacted"
    )
    return RuntimeStatsFlat(
        status=status,
        started_at=started,
        ended_at=ended,
        duration_seconds=round((ended - started).total_seconds()) if started and ended else None,
        turns=sum(len(session.turns) for session in session_graph.sessions),
        model_steps=sum(len(turn.steps) for session in session_graph.sessions for turn in session.turns),
        tool_calls=tool_calls,
        subagent_sessions=sum(1 for session in session_graph.sessions if session.parent_session_id is not None),
        compactions=compactions,
    )


def _message_stats(session_graph: SessionGraph) -> MessageStatsFlat:
    return MessageStatsFlat(
        user=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type == EventType.USER_PROMPT_SUBMITTED
        ),
        assistant=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type == EventType.LLM_RESPONSE
        ),
        developer=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.payload.get("raw_type") == "prompt_block"
            and event.payload.get("prompt_role") in {"developer", "system"}
        ),
        tool_outputs=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.type in {EventType.TOOL_CALL_SUCCEEDED, EventType.TOOL_CALL_FAILED}
        ),
        reasoning_items=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.payload.get("raw_type") == "reasoning"
        ),
        compacted_contexts=sum(
            1
            for session in session_graph.sessions
            for event in session.events
            if event.payload.get("raw_type") == "context_compacted"
        ),
    )


def _quota_stats_from_latest_event(event: Event | None) -> QuotaStatsFlat | None:
    if event is None:
        return None
    quota = event.payload.get("quota")
    if not isinstance(quota, dict):
        return None
    primary = quota.get("primary") if isinstance(quota.get("primary"), dict) else {}
    secondary = quota.get("secondary") if isinstance(quota.get("secondary"), dict) else {}
    return QuotaStatsFlat(
        plan_type=_as_str(quota.get("plan_type")),
        primary_used_percent=_as_float(primary.get("used_percent")),
        secondary_used_percent=_as_float(secondary.get("used_percent")),
        resets_at=_as_int(primary.get("resets_at")) or None,
    )


def _percent(value: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((value / denominator) * 100, 1)


def _compact_turn_usage(turn: TurnMetrics, *, session_id: UUID | None) -> TurnUsageCompactFlat:
    return TurnUsageCompactFlat(
        turn_id=turn.turn_id,
        session_id=session_id,
        usage=turn.token_usage,
        cost_usd=turn.cost_estimate.amount_usd,
        activity_usage=_turn_activity_breakdown(turn),
    )


def _turn_activity_breakdown(turn: TurnMetrics) -> list[ActivityUsageBreakdownFlat]:
    totals: dict[str, dict[str, Any]] = {
        "tool_steps": {"step_count": 0, "usage": TokenUsage(), "cost_usd": 0.0},
        "response_steps": {"step_count": 0, "usage": TokenUsage(), "cost_usd": 0.0},
        "mixed_steps": {"step_count": 0, "usage": TokenUsage(), "cost_usd": 0.0},
        "other_steps": {"step_count": 0, "usage": TokenUsage(), "cost_usd": 0.0},
    }
    for step in turn.steps:
        key = _activity_breakdown_kind(step)
        totals[key]["step_count"] += 1
        totals[key]["usage"] = totals[key]["usage"].plus(step.token_usage)
        totals[key]["cost_usd"] += step.cost_estimate.amount_usd
    return [
        ActivityUsageBreakdownFlat(
            category=key,
            usage=value["usage"],
            cost_usd=round(float(value["cost_usd"]), 8),
        )
        for key, value in totals.items()
        if value["step_count"] > 0
    ]


def _activity_breakdown_kind(step: StepMetrics) -> str:
    if step.kind == "mixed":
        return "mixed_steps"
    if step.tool_count > 0:
        return "tool_steps"
    if step.kind == "response":
        return "response_steps"
    return "other_steps"


def _turn_step_deltas(turn: TurnMetrics) -> list[StepMetricsFlat]:
    return [
        StepMetricsFlat(
            step_id=step.step_id,
            sequence=step.sequence,
            kind=step.kind,
            token_usage=None if _is_zero_usage(step.token_usage) else step.token_usage,
        )
        for step in turn.steps
    ]


def build_session_graph_tool_usage(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> dict[str, Any]:
    """Return tool-step cost boundaries and per-tool output size signals.

    Cost is observed/estimated at the step boundary. Individual tool entries only
    expose output-size signals because shell commands do not have independent
    billing records.
    """
    full = _build_full_metrics(session_graph, extra_billing=extra_billing)
    raw_steps = {
        step.step_id: step
        for session in session_graph.sessions
        for turn in session.turns
        for step in turn.steps
    }

    tool_steps: list[ToolStepUsageFlat] = []
    for session in full.sessions:
        for turn in session.turns:
            for step in turn.steps:
                if step.tool_count == 0:
                    continue

                raw_step = raw_steps.get(step.step_id)
                tools = _tool_output_usage(raw_step) if raw_step is not None else []
                tool_output_chars = sum(item.output_chars for item in tools)
                tool_output_original_tokens = sum(item.output_original_tokens or 0 for item in tools)
                tool_steps.append(
                    ToolStepUsageFlat(
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        turn_sequence=turn.sequence,
                        step_id=step.step_id,
                        step_sequence=step.sequence,
                        kind=step.kind,
                        observed_step_cost=step.cost_estimate.amount_usd,
                        token_usage=step.token_usage,
                        tool_count=step.tool_count,
                        duration_ms=step.tool_duration_ms,
                        tool_output_chars=tool_output_chars,
                        tool_output_original_tokens=tool_output_original_tokens,
                        tools=tools,
                    )
                )

    return SessionGraphToolUsageFlat(
        root_session_id=full.root_session_id,
        observed_tool_step_cost=round(sum(step.observed_step_cost for step in tool_steps), 8),
        extra_billing=full.cost_estimate.extra_billing,
        tool_step_count=len(tool_steps),
        tool_call_count=sum(step.tool_count for step in tool_steps),
        tool_output_chars=sum(step.tool_output_chars for step in tool_steps),
        tool_output_original_tokens=sum(step.tool_output_original_tokens for step in tool_steps),
        tool_steps=tool_steps,
        warnings=full.warnings,
    ).model_dump(mode="json")


def _tool_output_usage(step: Step) -> list[ToolOutputUsageFlat]:
    result: list[ToolOutputUsageFlat] = []
    tool_index = 0
    for item in step.items:
        if item.kind != "tool":
            continue
        output = "" if item.output is None else str(item.output)
        result.append(
            ToolOutputUsageFlat(
                tool_index=tool_index,
                tool_name=item.tool_name,
                status=item.status.value if item.status is not None else None,
                input_summary=_tool_input_summary(item.input),
                output_chars=len(output),
                output_original_tokens=_tool_original_token_count(output),
                output_truncated=_tool_output_is_truncated(output),
            )
        )
        tool_index += 1
    return result


def _tool_input_summary(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("cmd", "command", "path", "pattern", "query"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _compact_text(candidate)
    return _compact_text(str(value))


def _compact_text(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _tool_original_token_count(output: str) -> int | None:
    match = re.search(r"Original token count: (\d+)", output)
    if match is None:
        return None
    return int(match.group(1))


def _tool_output_is_truncated(output: str) -> bool:
    return "chars → event.detail" in output or "tokens truncated" in output


def _turn_model(turn: TurnMetrics) -> str | None:
    for step in turn.steps:
        model = _step_model(step)
        if model:
            return model
    return None


def _step_model(step: StepMetrics) -> str | None:
    for obs in step.observations:
        if obs.model:
            return obs.model
    return None


def _step_kind(step: Step, *, has_usage: bool = False) -> str:
    has_tool = any(item.kind == "tool" for item in step.items)
    has_text = any(item.kind == "text" for item in step.items)
    if has_tool and has_text:
        return "mixed"
    if has_tool:
        return "tool"
    if has_text or has_usage:
        return "response"
    return "empty"


def _step_tool_metrics(step: Step, events: list[Event]) -> tuple[int, int | None]:
    tool_count = sum(1 for item in step.items if item.kind == "tool")
    pending: dict[str, datetime] = {}
    durations_ms: list[int] = []

    for event in sorted(events, key=lambda item: item.timestamp):
        if event.type not in {
            EventType.TOOL_CALL_REQUESTED,
            EventType.TOOL_CALL_SUCCEEDED,
            EventType.TOOL_CALL_FAILED,
        }:
            continue

        key = _tool_event_key(event)
        if event.type == EventType.TOOL_CALL_REQUESTED:
            pending[key] = event.timestamp
            continue

        started_at = pending.pop(key, None)
        if started_at is None:
            continue
        duration_ms = int(max((event.timestamp - started_at).total_seconds(), 0) * 1000)
        durations_ms.append(duration_ms)

    return tool_count, sum(durations_ms) if durations_ms else None


def _tool_event_key(event: Event) -> str:
    payload = event.payload or {}
    value = payload.get("tool_call_id") or payload.get("tool_name")
    return str(value) if value else "__default__"


def _build_full_metrics(
    session_graph: SessionGraph,
    *,
    extra_billing: bool = False,
) -> SessionGraphMetrics:
    """Return full token/quota metrics projected onto the session_graph hierarchy."""
    index = build_session_graph_index(session_graph)
    sessions_by_id = {session.session_id: session for session in session_graph.sessions}
    session_metrics: list[SessionMetrics] = []
    total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    warnings: list[str] = []

    for session in session_graph.sessions:
        metrics = _build_session_metrics(
            session,
            index=index,
            sessions_by_id=sessions_by_id,
            extra_billing=extra_billing,
        )
        session_metrics.append(metrics)
        total = total.plus(metrics.token_usage)
        cost_total = cost_total.plus(metrics.cost_estimate)
        if not _session_has_usage(metrics):
            warnings.append(f"no token usage metrics found for session {session.session_id}")
        if not metrics.cost_estimate.complete:
            warnings.extend(metrics.cost_estimate.missing_reasons)

    return SessionGraphMetrics(
        root_session_id=session_graph.root_session_id,
        token_usage=total,
        cost_estimate=_finalize_cost(cost_total),
        sessions=session_metrics,
        warnings=_unique(warnings),
    )


def _build_session_metrics(
    session: Session,
    *,
    index: SessionGraphIndex,
    sessions_by_id: dict[UUID, Session],
    extra_billing: bool,
) -> SessionMetrics:
    codex_state = _CodexUsageState(
        remaining_inherited_totals=_inherited_codex_totals(session, sessions_by_id),
        seen_totals=set(),
    )
    turn_metrics: list[TurnMetrics] = []
    session_total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    latest_quota: QuotaSnapshot | None = None

    for turn in session.turns:
        metrics = _build_turn_metrics(
            session,
            turn,
            index=index,
            codex_state=codex_state,
            extra_billing=extra_billing,
        )
        turn_metrics.append(metrics)
        session_total = session_total.plus(metrics.token_usage)
        cost_total = cost_total.plus(metrics.cost_estimate)
        if metrics.quota_snapshots:
            latest_quota = metrics.quota_snapshots[-1]

    return SessionMetrics(
        session_id=session.session_id,
        vendor=session.vendor.value,
        status=session.status.value,
        token_usage=session_total,
        cost_estimate=_finalize_cost(cost_total),
        turns=turn_metrics,
        quota_snapshot=latest_quota,
    )


def _build_turn_metrics(
    session: Session,
    turn: Turn,
    *,
    index: SessionGraphIndex,
    codex_state: _CodexUsageState,
    extra_billing: bool,
) -> TurnMetrics:
    step_metrics: list[StepMetrics] = []
    turn_total = TokenUsage()
    cost_total = CostEstimate(extra_billing=extra_billing)
    quota_snapshots: list[QuotaSnapshot] = []

    for step in turn.steps:
        metrics = _build_step_metrics(
            session,
            step,
            index=index,
            codex_state=codex_state,
            extra_billing=extra_billing,
        )
        step_metrics.append(metrics)
        turn_total = turn_total.plus(metrics.token_usage)
        cost_total = cost_total.plus(metrics.cost_estimate)

    for event in events_for_turn(index, turn):
        quota = _quota_snapshot_from_event(event)
        if quota is not None:
            quota_snapshots.append(quota)

    quota_snapshots.sort(key=lambda item: item.timestamp)
    return TurnMetrics(
        turn_id=turn.turn_id,
        sequence=turn.sequence,
        status=turn.status.value,
        started_at=turn.started_at,
        completed_at=turn.ended_at,
        token_usage=turn_total,
        cost_estimate=_finalize_cost(cost_total),
        steps=step_metrics,
        quota_snapshots=quota_snapshots,
    )


def _build_step_metrics(
    session: Session,
    step: Step,
    *,
    index: SessionGraphIndex,
    codex_state: _CodexUsageState,
    extra_billing: bool,
) -> StepMetrics:
    observations: list[TokenUsageObservation] = []

    events = events_for_step(index, step)

    if not _step_has_usage_event(step, events):
        vendor_observation = _usage_from_step_vendor_data(step)
        if vendor_observation is not None:
            observations.append(vendor_observation)

    for event in events:
        event_observation = _usage_from_event(
            event,
            step=step,
            session=session,
            codex_state=codex_state,
        )
        if event_observation is not None:
            observations.append(event_observation)

    observations.sort(key=lambda item: item.timestamp)
    total = TokenUsage()
    vendor_cost = _vendor_reported_cost_from_step(step, observations=observations, extra_billing=extra_billing)
    cost_total = vendor_cost or CostEstimate(extra_billing=extra_billing)
    for observation in observations:
        total = total.plus(observation.usage)
        if vendor_cost is None:
            cost_total = cost_total.plus(
                estimate_observation_cost(observation, extra_billing=extra_billing)
            )

    tool_count, tool_duration_ms = _step_tool_metrics(step, events)

    return StepMetrics(
        step_id=step.step_id,
        sequence=step.sequence,
        kind=_step_kind(step, has_usage=bool(observations)),
        token_usage=total,
        cost_estimate=_finalize_cost(cost_total),
        observations=observations,
        tool_count=tool_count,
        tool_duration_ms=tool_duration_ms,
    )


def _usage_from_step_vendor_data(step: Step) -> TokenUsageObservation | None:
    data = step.vendor_data or {}
    normalized = data.get("metrics")
    if not isinstance(normalized, dict):
        return None

    usage = normalized.get("usage")
    if not isinstance(usage, dict):
        return None

    provider = _as_str(normalized.get("provider")) or step.vendor.value
    model = _as_str(normalized.get("model")) or _as_str(usage.get("model"))
    token_usage = _token_usage_from_mapping(usage)

    if _is_zero_usage(token_usage):
        return None

    return TokenUsageObservation(
        scope_type="step",
        scope_id=step.step_id,
        timestamp=step.timestamp,
        usage=token_usage,
        provider=provider,
        model=model,
        source=MetricSource(vendor=provider, source_type="step.vendor_data"),
    )


def _vendor_reported_cost_from_step(
    step: Step,
    *,
    observations: list[TokenUsageObservation],
    extra_billing: bool,
) -> CostEstimate | None:
    data = step.vendor_data or {}
    normalized = data.get("metrics")
    if not isinstance(normalized, dict):
        return None

    usage = normalized.get("usage")
    if not isinstance(usage, dict):
        return None

    amount = _as_float(usage.get("cost_usd"))
    if amount is None:
        return None

    model = next((observation.model for observation in observations if observation.model), None)
    return CostEstimate(
        amount_usd=amount,
        extra_billing=extra_billing,
        pricing_source="vendor_reported",
        pricing_effective_date=step.timestamp.date().isoformat(),
        model=model,
        complete=True,
    )


def _usage_from_event(
    event: Event,
    *,
    step: Step,
    session: Session,
    codex_state: _CodexUsageState,
) -> TokenUsageObservation | None:
    if event.type != EventType.VENDOR_RAW:
        return None
    if event.vendor_source != Vendor.CODEX_CLI:
        return None
    if event.payload.get("raw_type") != "token_count":
        return None

    metrics = event.payload.get("metrics")
    if not isinstance(metrics, dict):
        return None

    token_usage = _codex_delta_usage(metrics, codex_state)
    if token_usage is None or _is_zero_usage(token_usage):
        return None
    model = _as_str(metrics.get("model"))

    return TokenUsageObservation(
        scope_type="step",
        scope_id=step.step_id,
        timestamp=event.timestamp,
        usage=token_usage,
        provider=session.vendor.value,
        model=model,
        source=MetricSource(
            vendor=event.vendor_source.value,
            source_type="event.payload",
            event_id=event.event_id,
        ),
    )


def _codex_delta_usage(info: dict[str, Any], state: _CodexUsageState) -> TokenUsage | None:
    total_raw = info.get("total_token_usage")
    if isinstance(total_raw, dict):
        raw_totals = _token_usage_from_mapping(total_raw)
        total_key = _usage_key(raw_totals)
        if state.seen_totals is not None and total_key in state.seen_totals:
            return None
        if state.seen_totals is not None:
            state.seen_totals.add(total_key)

        current_totals = _subtract_usage(raw_totals, state.remaining_inherited_totals)
        previous_totals = state.previous_totals or TokenUsage()
        delta = _subtract_usage(current_totals, previous_totals)
        state.previous_totals = current_totals
        state.remaining_inherited_totals = None
        return delta

    last_raw = info.get("last_token_usage")
    if not isinstance(last_raw, dict):
        return None

    raw_delta = _token_usage_from_mapping(last_raw)
    delta = _subtract_usage(raw_delta, state.remaining_inherited_totals)
    state.remaining_inherited_totals = _subtract_usage(state.remaining_inherited_totals, raw_delta)
    previous_totals = state.previous_totals or TokenUsage()
    state.previous_totals = previous_totals.plus(delta)
    return delta


def _inherited_codex_totals(
    session: Session,
    sessions_by_id: dict[UUID, Session],
) -> TokenUsage | None:
    if session.vendor != Vendor.CODEX_CLI or session.parent_session_id is None:
        return None
    parent = sessions_by_id.get(session.parent_session_id)
    if parent is None or parent.vendor != Vendor.CODEX_CLI:
        return None

    state = _CodexUsageState(seen_totals=set())
    latest = TokenUsage()
    for event in sorted(parent.events, key=lambda item: item.timestamp):
        if event.timestamp > session.started_at:
            break
        if event.type != EventType.VENDOR_RAW:
            continue
        if event.vendor_source != Vendor.CODEX_CLI:
            continue
        if event.payload.get("raw_type") != "token_count":
            continue
        metrics = event.payload.get("metrics")
        if not isinstance(metrics, dict):
            continue
        delta = _codex_delta_usage(metrics, state)
        if delta is not None:
            latest = latest.plus(delta)

    return None if _is_zero_usage(latest) else latest


def _quota_snapshot_from_event(event: Event) -> QuotaSnapshot | None:
    if event.type != EventType.VENDOR_RAW:
        return None
    if event.vendor_source != Vendor.CODEX_CLI:
        return None
    if event.payload.get("raw_type") != "token_count":
        return None

    rate_limits = event.payload.get("quota")
    if not isinstance(rate_limits, dict):
        return None

    return QuotaSnapshot(
        timestamp=event.timestamp,
        source_event_id=event.event_id,
        limit_id=_as_str(rate_limits.get("limit_id")),
        plan_type=_as_str(rate_limits.get("plan_type")),
        primary=_quota_window(rate_limits.get("primary")),
        secondary=_quota_window(rate_limits.get("secondary")),
        rate_limit_reached_type=_as_str(rate_limits.get("rate_limit_reached_type")),
    )


def _quota_window(value: Any) -> QuotaWindow | None:
    if not isinstance(value, dict):
        return None
    return QuotaWindow(
        used_percent=_as_float(value.get("used_percent")),
        window_minutes=_as_int(value.get("window_minutes")),
        resets_at=_as_int(value.get("resets_at")),
    )


def _token_usage_from_mapping(value: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=_as_int(value.get("input_tokens") or value.get("inputTokens")),
        cached_input_tokens=_as_int(value.get("cached_input_tokens") or value.get("cachedInputTokens")),
        output_tokens=_as_int(value.get("output_tokens") or value.get("outputTokens")),
        reasoning_output_tokens=_as_int(
            value.get("reasoning_output_tokens") or value.get("reasoningOutputTokens")
        ),
        total_tokens=_as_int(value.get("total_tokens") or value.get("totalTokens")),
    )


def _usage_key(usage: TokenUsage) -> tuple[int, int, int, int, int]:
    return (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
        usage.total_tokens,
    )


def _subtract_usage(left: TokenUsage | None, right: TokenUsage | None) -> TokenUsage:
    left = left or TokenUsage()
    right = right or TokenUsage()
    return TokenUsage(
        input_tokens=max(left.input_tokens - right.input_tokens, 0),
        cached_input_tokens=max(left.cached_input_tokens - right.cached_input_tokens, 0),
        output_tokens=max(left.output_tokens - right.output_tokens, 0),
        reasoning_output_tokens=max(left.reasoning_output_tokens - right.reasoning_output_tokens, 0),
        total_tokens=max(left.total_tokens - right.total_tokens, 0),
    )


def _session_has_usage(metrics: SessionMetrics) -> bool:
    return not _is_zero_usage(metrics.token_usage)


def _is_zero_usage(usage: TokenUsage) -> bool:
    return all(value == 0 for value in usage.model_dump().values())


def _step_has_usage_event(step: Step, events: list[Event]) -> bool:
    if step.vendor != Vendor.CODEX_CLI:
        return False
    return any(
        event.type == EventType.VENDOR_RAW
        and event.vendor_source == Vendor.CODEX_CLI
        and event.payload.get("raw_type") == "token_count"
        for event in events
    )


def _finalize_cost(cost: CostEstimate) -> CostEstimate:
    return cost.model_copy(
        update={
            "amount_usd": round(cost.amount_usd, 8),
            "missing_reasons": _unique(cost.missing_reasons),
            "breakdown": cost.breakdown.model_copy(
                update={
                    "input_usd": round(cost.breakdown.input_usd, 8),
                    "cached_input_usd": round(cost.breakdown.cached_input_usd, 8),
                    "output_usd": round(cost.breakdown.output_usd, 8),
                    "reasoning_output_usd": round(cost.breakdown.reasoning_output_usd, 8),
                }
            ),
        }
    )


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
