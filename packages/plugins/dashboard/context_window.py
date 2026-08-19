"""Context Window projection: assembly from compacted service payloads."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from coding_trajectory.runtime import ServiceApiClient, default_plugin_client

try:
    from .context_window_compact import (
        _compact_overview_api,
        _compact_stats_api,
        _compact_usage_api,
    )
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from context_window_compact import (
        _compact_overview_api,
        _compact_stats_api,
        _compact_usage_api,
    )
try:
    from .context_window_models import (
        CacheBreakRecord,
        CacheBreakSummary,
        CategoryKey,
        CompactionEventRecord,
        CompactionSummary,
        Confidence,
        ContextCategory,
        ContextEvent,
        ContextSessionSection,
        ContextWindowProjection,
        CostEvidence,
        ExpensiveItem,
        TokenEvidence,
        _category_sort_key,
        _visible_text_size,
    )
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from context_window_models import (
        CacheBreakRecord,
        CacheBreakSummary,
        CategoryKey,
        CompactionEventRecord,
        CompactionSummary,
        Confidence,
        ContextCategory,
        ContextEvent,
        ContextSessionSection,
        ContextWindowProjection,
        CostEvidence,
        ExpensiveItem,
        TokenEvidence,
        _category_sort_key,
        _visible_text_size,
    )
try:
    from .context_window_render import _dedupe, render_markdown
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from context_window_render import _dedupe, render_markdown
try:
    from .context_window_tools import (
        _confidence,
        _cost_evidence_from_estimate,
        _optional_float,
        _optional_int,
        _optional_text,
        _tool_category,
        _tool_events_by_turn,
        _tool_item_events,
    )
except ImportError:  # pragma: no cover - direct plugin-directory imports
    from context_window_tools import (
        _confidence,
        _cost_evidence_from_estimate,
        _optional_float,
        _optional_int,
        _optional_text,
        _tool_category,
        _tool_events_by_turn,
        _tool_item_events,
    )


def main(
    argv: list[str] | None = None,
    *,
    prog: str = "ct plugin dashboard session context-window",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Inspect context composition and trajectory events for one session.",
    )
    parser.add_argument("session_id")
    parser.add_argument(
        "--turn",
        dest="turn_id",
        default=None,
        help="Limit the event timeline to one turn.",
    )
    parser.add_argument(
        "--output",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format. Defaults to markdown.",
    )
    args = parser.parse_args(argv)

    try:
        projection = build_projection(args.session_id, turn_id=args.turn_id)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if args.output == "json":
        print(projection.model_dump_json(indent=2))
    else:
        print(render_markdown(projection))
    return 0


def build_projection(
    session_id: str,
    *,
    turn_id: str | None = None,
    client: ServiceApiClient | None = None,
) -> ContextWindowProjection:
    client = client or default_plugin_client()
    stats, overview, usage, tool_usage = _load_projection_inputs(session_id, client)

    selected_stats = _selected_session_stats(stats, session_id)
    active_session_id = str(
        selected_stats.get("session_id") or selected_stats.get("id") or session_id
    )
    selected_usage = _selected_session_usage(usage, active_session_id)
    session_sections = _project_session_sections(stats, usage)

    vendor = str(
        selected_stats.get("vendor")
        or stats.get("vendor")
        or _overview_vendor(overview)
        or "unknown"
    )
    model = selected_stats.get("model") or {}
    model_name = _optional_text(model.get("name"))
    categories = _project_categories(selected_stats)
    provider_usage_buckets = _project_provider_usage_buckets(selected_stats)
    expensive_items = _project_expensive_items(tool_usage, session_id=active_session_id)
    events = [
        *_category_events(categories),
        *_trajectory_events(
            overview,
            selected_usage,
            tool_usage,
            turn_id=turn_id,
            session_id=active_session_id,
        ),
    ]
    warnings = [
        str(item)
        for item in selected_stats.get("warnings") or stats.get("warnings") or []
    ]
    if len(session_sections) > 1:
        warnings.append(
            "This session id resolves to a session graph; context-window values are scoped to the active session, not the graph aggregate."
        )
    warnings.extend(_projection_warnings(events))
    if turn_id and not any(event.turn_id == turn_id for event in events):
        raise SystemExit(f"turn not found in session overview: {turn_id}")

    compaction = _project_compaction(selected_stats)
    cache_breaks = _project_cache_breaks(
        selected_usage, vendor=vendor, compaction=compaction
    )

    context = selected_stats.get("context") or {}
    reported_context_window = model.get("context_window") or model.get(
        "context_window_tokens"
    )
    # Core resolves the context window in ``session.stats`` (reported value or
    # the merged static/live model catalog) — the dashboard reads it directly.
    context_window = reported_context_window
    used_tokens = _optional_int(context.get("used") or context.get("used_tokens"))
    used_percent = _optional_float(context.get("pct") or context.get("used_percent"))
    if used_percent is None and used_tokens is not None and context_window:
        used_percent = round((used_tokens / context_window) * 100, 1)
    # Session-total cost is the pricing SoT's estimate, emitted by
    # ``session.usage`` as ``cost`` + ``pricing`` (source/effective_date).
    reported_cost = _optional_float(selected_usage.get("cost"))
    pricing = selected_usage.get("pricing") or {}
    return ContextWindowProjection(
        session_id=str(stats.get("id") or session_id),
        active_session_id=active_session_id,
        vendor=vendor,
        model=model_name,
        context_window_tokens=_token_evidence(
            context_window,
            confidence="structural",
            source="ct session stats:model.context_window",
        ),
        used_tokens=_token_evidence(
            used_tokens,
            confidence="exact_usage",
            source="ct session stats:context.used",
        ),
        used_percent=used_percent,
        token_cost=(
            CostEvidence(
                value_usd=reported_cost,
                confidence=str(pricing.get("confidence") or "estimated"),
                source=str(pricing.get("source") or "ct session usage:cost"),
                effective_date=pricing.get("effective_date"),
            )
            if reported_cost is not None
            else None
        ),
        categories=sorted(categories, key=_category_sort_key, reverse=True),
        provider_usage_buckets=sorted(
            provider_usage_buckets,
            key=_category_sort_key,
            reverse=True,
        ),
        session_sections=session_sections,
        expensive_items=expensive_items,
        events=events,
        compaction=compaction,
        cache_breaks=cache_breaks,
        warnings=_dedupe(warnings),
    )


def _load_projection_inputs(
    session_id: str,
    client: ServiceApiClient,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    # ``ct api batch`` returns internal service shapes, while the dedicated
    # commands apply CLI display compaction. Keep that compatibility boundary
    # local to this plugin instead of importing CLI-private helpers.
    requests = [
        {
            "id": name,
            "method": method,
            "params": {"session_id": session_id},
        }
        for name, method in (
            ("stats", "session.stats"),
            ("overview", "session.overview"),
            ("usage", "session.usage"),
            ("tool_usage", "session.tool_usage"),
        )
    ]
    payload = {"items": [client.execute(request) for request in requests]}
    rows = {
        str(item.get("id")): item
        for item in payload.get("items") or []
        if isinstance(item, dict)
    }
    stats = _required_batch_result(rows, "stats")
    overview = _required_batch_result(rows, "overview")
    usage = _required_batch_result(rows, "usage")
    tool_usage = _required_batch_result(rows, "tool_usage")
    if not isinstance(stats.get("root_session_id"), str):
        raise RuntimeError("ct api request returned invalid result: stats")
    if not isinstance(overview.get("root_session_id"), str):
        raise RuntimeError("ct api request returned invalid result: overview")
    if not isinstance(usage.get("session_id"), str) or not isinstance(
        usage.get("total_usage"), dict
    ):
        raise RuntimeError("ct api request returned invalid result: usage")
    if not isinstance(tool_usage.get("root_session_id"), str):
        raise RuntimeError("ct api request returned invalid result: tool_usage")
    return (
        _compact_stats_api(stats),
        _compact_overview_api(overview),
        _compact_usage_api(usage),
        tool_usage,
    )


def _required_batch_result(
    rows: dict[str, dict[str, Any]], request_id: str
) -> dict[str, Any]:
    row = rows.get(request_id)
    if row is None:
        raise RuntimeError(f"ct api batch omitted response: {request_id}")
    if not row.get("ok"):
        error = row.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(str(message or f"ct api request failed: {request_id}"))
    result = row.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"ct api request returned invalid result: {request_id}")
    return result


def _selected_session_stats(stats: dict[str, Any], session_id: str) -> dict[str, Any]:
    sessions = [item for item in stats.get("sessions") or [] if isinstance(item, dict)]
    if not sessions:
        return stats
    for item in sessions:
        if session_id in {str(item.get("id")), str(item.get("session_id"))}:
            return item
    for item in sessions:
        if item.get("role") == "main":
            return item
    return sessions[0]


def _selected_session_usage(usage: dict[str, Any], session_id: str) -> dict[str, Any]:
    sessions = [item for item in usage.get("sessions") or [] if isinstance(item, dict)]
    if not sessions:
        return usage
    for item in sessions:
        if session_id in {str(item.get("id")), str(item.get("session_id"))}:
            return item
    for item in sessions:
        if item.get("role") == "main":
            return item
    return sessions[0]


def _project_session_sections(
    stats: dict[str, Any],
    usage: dict[str, Any],
) -> list[ContextSessionSection]:
    usage_by_id = {
        str(item.get("id") or item.get("session_id")): item
        for item in usage.get("sessions") or []
        if isinstance(item, dict)
    }
    sections: list[ContextSessionSection] = []
    for item in stats.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or item.get("id") or "")
        if not session_id:
            continue
        usage_item = (
            usage_by_id.get(session_id) or usage_by_id.get(str(item.get("id"))) or {}
        )
        context = item.get("context") or {}
        pricing = usage_item.get("pricing") or {}
        cost = _optional_float(usage_item.get("cost"))
        sections.append(
            ContextSessionSection(
                session_id=session_id,
                role=str(item.get("role") or item.get("relationship") or "session"),
                label=str(
                    item.get("title")
                    or item.get("agent_name")
                    or item.get("relationship")
                    or item.get("role")
                    or session_id[:8]
                ),
                relationship=_optional_text(item.get("relationship")),
                parent_session_id=_optional_text(item.get("parent")),
                used_tokens=_token_evidence(
                    _optional_int(context.get("used")),
                    confidence="exact_usage",
                    source="ct session stats:sessions[].context.used",
                ),
                used_percent=_optional_float(context.get("pct")),
                token_cost=(
                    CostEvidence(
                        value_usd=cost,
                        confidence=str(pricing.get("confidence") or "estimated"),
                        source=str(pricing.get("source") or "ct session usage:cost"),
                        effective_date=pricing.get("effective_date"),
                    )
                    if cost is not None
                    else None
                ),
            )
        )
    return sections


def _project_compaction(stats: dict[str, Any]) -> CompactionSummary | None:
    """Lift the compaction timeline from ``ct session stats`` or ``session.usage`` JSON.

    ``stats`` carries ``compaction`` (count, cumulative dropped, last event,
    and the full ``events`` list) when the session has compacted; the field is
    absent or ``None`` otherwise.

    Form-agnostic: the CLI display path (``compact_stats_payload`` /
    ``compact_payload``) renames ``cumulative_dropped_tokens`` ->
    ``cumulative_dropped``, ``pre_tokens`` -> ``pre``, etc. The raw
    ``ct api call session.usage`` / ``session.stats`` api emits the long
    names. Read both so this works on either form - the cache-breaks
    aggregate calls this on raw-api ``session.usage`` data.
    """
    compaction = stats.get("compaction")
    if not isinstance(compaction, dict) or not compaction.get("count"):
        return None
    events = [
        CompactionEventRecord(
            timestamp=str(event.get("timestamp") or ""),
            mechanism=str(event.get("mechanism") or ""),
            trigger=_optional_text(event.get("trigger")),
            pre_tokens=_optional_int(event.get("pre") or event.get("pre_tokens")),
            post_tokens=_optional_int(event.get("post") or event.get("post_tokens")),
            dropped_tokens=_optional_int(
                event.get("dropped") or event.get("dropped_tokens")
            ),
        )
        for event in compaction.get("events") or []
        if isinstance(event, dict) and event.get("timestamp")
    ]
    return CompactionSummary(
        count=int(compaction.get("count") or 0),
        cumulative_dropped_tokens=_optional_int(
            compaction.get("cumulative_dropped")
            or compaction.get("cumulative_dropped_tokens")
        ),
        events=events,
    )


def _turn_model_key(turn: dict[str, Any]) -> str | None:
    """Stable ``provider/model`` identity for a turn, for cache-key-change
    detection. Reads the ``provider``/``model`` fields core emits per turn in
    ``session.usage``. Returns ``None`` when either is absent (stale/CLI form),
    in which case model-switch attribution is skipped for that turn."""
    provider = _optional_text(turn.get("provider"))
    model = _optional_text(turn.get("model"))
    if not provider or not model:
        return None
    return f"{provider}/{model}"


# Per-turn miss count at or below this is cache-breakpoint granularity noise.
# Applied only to ``unattributed`` (the no-cause bucket); the attributed types
# already require a confirming observation so ``re_read > 0`` suffices.
NOISE_FLOOR_TOKENS = 1024


def _project_cache_breaks(
    usage: dict[str, Any],
    *,
    vendor: str,
    compaction: CompactionSummary | None = None,
    require_effort_changes: bool = True,
) -> CacheBreakSummary | None:
    """Classify measured turn-boundary cache losses into supported causes.

    A row needs both an observed cache-hit reduction from the prior turn's
    final request to this turn's first request and either an aligned effort
    change or a TTL-sized idle gap. Post-compaction turns are skipped because
    their loss is a compaction signature, not an effort/TTL cache break.

    ``effort_changes`` must be present in ``usage`` (core always emits it, even
    as ``{"count": 0}`` when the session never changed effort) — its absence
    means the installed ct is stale/incomplete and lacks the
    effort_change-ingestion capability. Throw rather than silently falling back
    to the pure heuristic, so a stale install is loud instead of producing
    plausible-but-unconfirmed break labels.
    """
    if require_effort_changes and "effort_changes" not in usage:
        raise SystemExit(
            "ct does not surface effort_changes in session.usage — the install "
            "is stale or incomplete (Direction 1 effort-change ingestion is "
            "missing). Reinstall ct editable and retry:\n"
            "  uv tool install --force --editable packages/cli "
            "--with-editable packages/core --with-editable "
            "packages/plugins/code_time --with-editable packages/plugins/dashboard"
        )
    turns = [t for t in (usage.get("turns") or []) if isinstance(t, dict)]
    if not turns:
        return None

    skip_turns = _post_compaction_turn_ids(usage, compaction)
    effort_change_by_turn = _effort_changed_turns(usage)
    ttl_confirmed_s, ttl_likely_min_s = _vendor_ttl_thresholds(vendor)
    records: list[CacheBreakRecord] = []
    prev_model_key: str | None = None
    for turn in turns:
        turn_id = str(turn.get("id") or turn.get("turn_id") or "")
        if turn_id in skip_turns:
            continue
        runtime = turn.get("runtime") or {}
        idle = _optional_float(runtime.get("wait_before_seconds"))
        re_read = _optional_int(turn.get("cache_boundary_loss_tokens")) or 0
        model_key = _turn_model_key(turn)
        if idle is None or re_read <= 0:
            prev_model_key = model_key or prev_model_key
            continue
        cached_after = _optional_int(turn.get("cache_first_call_cached_tokens"))
        # Cause attribution, strongest first. A confirmed effort change
        # overrides everything (an observed fact, not a heuristic). A model
        # switch overrides TTL: a new cache key re-bills the whole prefix
        # regardless of idle. TTL applies when nothing else explains the loss.
        # Anything left is ``unattributed`` — the bad-behavior bucket — filtered
        # by a noise floor so breakpoint-granularity churn does not drown the signal.
        confirmed = effort_change_by_turn.get(turn_id)
        if confirmed is not None:
            break_type = "effort_switch"
            effort_from, effort_to = confirmed
            model_from = None
            model_to = None
        elif (
            prev_model_key is not None
            and model_key is not None
            and model_key != prev_model_key
        ):
            break_type = "model_switch"
            effort_from = None
            effort_to = None
            model_from = prev_model_key
            model_to = model_key
        else:
            break_type = (
                "ttl_confirmed"
                if idle >= ttl_confirmed_s
                else "ttl_likely"
                if idle >= ttl_likely_min_s
                else None
            )
            if break_type is None:
                # A measured boundary loss with no aligned effort change, no model
                # switch, and no TTL-sized idle - surface it as ``unattributed``
                # rather than dropping the miss. Covers glm-5.2 (idle ~0 by
                # construction, cache not coupled to effort) and the bad-behavior
                # class (tool reorder/removal, nondeterministic enumeration,
                # system-prompt churn, proxy session-affinity loss). Filter
                # breakpoint-granularity noise so the signal stays meaningful.
                if re_read <= NOISE_FLOOR_TOKENS:
                    prev_model_key = model_key or prev_model_key
                    continue

                break_type = "unattributed"
            effort_from = None
            effort_to = None
            model_from = None
            model_to = None
        prev_model_key = model_key or prev_model_key
        # Core prices the observable boundary cache-hit loss; the dashboard
        # only assigns a supported cause to that measured gap.
        waste = _optional_float(turn.get("cache_break_waste_usd"))
        records.append(
            CacheBreakRecord(
                turn_id=turn_id,
                type=break_type,
                idle_seconds=idle,
                re_read_tokens=re_read,
                cached_after_tokens=cached_after,
                est_cost_usd=waste,
                effort_from=effort_from,
                effort_to=effort_to,
                model_from=model_from,
                model_to=model_to,
            )
        )
    # Intra-turn collapses: a cache-hit drop between two provider calls *inside*
    # the same turn (below the turn-boundary detector's resolution). Emit one
    # ``unattributed`` record per turn whose largest intra-turn drop is > 0. This
    # is independent of the inter-turn gate above (``idle``/``re_read``), so a
    # turn with no boundary loss but a mid-turn invalidation still surfaces.
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or turn.get("turn_id") or "")
        if not turn_id or turn_id in skip_turns:
            continue
        intra_loss = _optional_int(turn.get("cache_intra_turn_loss_tokens")) or 0
        if intra_loss <= 0:
            continue
        records.append(
            CacheBreakRecord(
                turn_id=turn_id,
                type="unattributed",
                # Intra-call gap; not TTL-driven. ``wait_before_seconds`` only
                # covers the inter-turn think-time, so report the within-turn
                # collapse with a zero idle marker.
                idle_seconds=0.0,
                re_read_tokens=intra_loss,
                cached_after_tokens=None,
                est_cost_usd=_optional_float(turn.get("cache_intra_turn_waste_usd")),
                effort_from=None,
                effort_to=None,
            )
        )
    if not records:
        return None
    by_type: dict[str, int] = {}
    total_re_read = 0
    total_waste = 0.0
    has_waste = False
    for record in records:
        by_type[record.type] = by_type.get(record.type, 0) + 1
        total_re_read += record.re_read_tokens
        if record.est_cost_usd is not None:
            total_waste += record.est_cost_usd
            has_waste = True
    return CacheBreakSummary(
        count=len(records),
        floor_tokens=None,
        total_re_read_tokens=total_re_read,
        estimated_waste_usd=round(total_waste, 4) if has_waste else None,
        by_type=by_type,
        events=records,
    )


def _vendor_ttl_thresholds(vendor: str) -> tuple[float, float]:
    """Return ``(confirmed_seconds, likely_min_seconds)`` for prompt-cache TTL.

    OpenAI's automatic prompt cache TTL is 5–10 min, so >=600s is confirmed
    and the 300–600s band is ambiguous (``ttl_likely``). Anthropic's explicit
    ``cache_control`` TTL is 5 min, so >=300s is confirmed. Other vendors fall
    back to the conservative 300s threshold.
    """
    normalized = (vendor or "").strip().lower()
    if normalized in {"anthropic", "claude", "claude-code", "claude_code"}:
        return (300.0, 300.0)
    if normalized in {"openai", "codex", "codex_cli", "openai-codex"}:
        return (600.0, 300.0)
    return (300.0, 300.0)


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _post_compaction_turn_ids(
    usage: dict[str, Any],
    compaction: CompactionSummary | None,
) -> set[str]:
    """Turn ids whose start immediately follows a compaction event.

    Such turns re-read the freshly compacted context — a compaction signature,
    not an effort/TTL cache break — so the classifier skips them.
    """
    if compaction is None or not compaction.events:
        return set()
    compaction_ts = [
        ts
        for ts in (_parse_iso_timestamp(event.timestamp) for event in compaction.events)
        if ts is not None
    ]
    if not compaction_ts:
        return set()
    starts: list[tuple[datetime, str]] = []
    for turn in usage.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        runtime = turn.get("runtime") or {}
        start = _parse_iso_timestamp(runtime.get("start") or runtime.get("started_at"))
        if start is not None and (turn.get("id") or turn.get("turn_id")):
            starts.append((start, str(turn.get("id") or turn.get("turn_id"))))
    if not starts:
        return set()
    starts.sort()
    skip: set[str] = set()
    for comp_ts in compaction_ts:
        for start, turn_id in starts:
            if start >= comp_ts:
                skip.add(turn_id)
                break
    return skip


def _effort_changed_turns(
    usage: dict[str, Any],
) -> dict[str, tuple[str | None, str | None]]:
    """Map each ``effort_changed`` observation to the turn that first uses the
    new effort.

    Prefer a usage turn whose runtime contains the observation timestamp. This
    matters for Codex, where ``turn_context`` can be written a few
    milliseconds after the turn's lifecycle start even though it belongs to
    that same turn. If no active turn contains the observation, fall back to
    the first later turn that actually re-processed tokens
    (``uncached_prompt > 0``).

    Skipping zero-usage turns matters for Claude Code: ``/effort`` lands on its
    own no-model-call turn(s), and the effort-caused re-read surfaces on the
    next turn with usage (the cache rebuild under the new key). For Codex the
    observation is on the turn_context of the collapse turn itself, which has a
    re-read, so it maps to the same turn. Returns ``turn_id ->
    (effort_from, effort_to)``; ``effort_from`` is ``None`` on Claude Code's
    first ``/effort`` switch (baseline unknown). Earlier change wins
    (``setdefault``).
    """
    block = usage.get("effort_changes") or {}
    events = [e for e in (block.get("events") or []) if isinstance(e, dict)]
    if not events:
        return {}
    change_ts: list[tuple[datetime, str | None, str | None]] = []
    for event in events:
        ts = _parse_iso_timestamp(event.get("timestamp"))
        if ts is None:
            continue
        # Form-agnostic: the CLI reshapes to ``from``/``to``; the raw api emits
        # ``effort_from``/``effort_to``. Read both so this works on either form.
        change_ts.append(
            (
                ts,
                event.get("from") or event.get("effort_from"),
                event.get("to") or event.get("effort_to"),
            )
        )
    if not change_ts:
        return {}
    # Candidate turns: those with a real re-read (uncached_prompt > 0), sorted
    # by start. A turn without a re-read cannot be an effort-caused break.
    candidates: list[tuple[datetime, datetime | None, str]] = []
    for turn in usage.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        runtime = turn.get("runtime") or {}
        start = _parse_iso_timestamp(runtime.get("start") or runtime.get("started_at"))
        end = _parse_iso_timestamp(runtime.get("end") or runtime.get("ended_at"))
        re_read = _usage_token(turn.get("usage"), "uncached_prompt") or 0
        if (
            start is not None
            and re_read > 0
            and (turn.get("id") or turn.get("turn_id"))
        ):
            candidates.append((start, end, str(turn.get("id") or turn.get("turn_id"))))
    if not candidates:
        return {}
    candidates.sort()
    mapping: dict[str, tuple[str | None, str | None]] = {}
    for ts, effort_from, effort_to in change_ts:
        # An initial ``/effort`` before the session's first provider request
        # configures that request; it has no already-warm, in-session prefix
        # to invalidate and must not be reported as a cache break.
        if not any(start < ts for start, _end, _turn_id in candidates):
            continue
        containing_turn = next(
            (
                turn_id
                for start, end, turn_id in candidates
                if start <= ts and (end is None or ts <= end)
            ),
            None,
        )
        if containing_turn is not None:
            mapping.setdefault(containing_turn, (effort_from, effort_to))
            continue
        for start, _end, turn_id in candidates:
            if start >= ts:
                mapping.setdefault(turn_id, (effort_from, effort_to))
                break
    return mapping


def _project_categories(stats: dict[str, Any]) -> list[ContextCategory]:
    context = stats.get("context") or {}
    leaves = list(_category_leaves(context.get("categories") or []))
    projected: list[ContextCategory] = []
    for index, category in enumerate(leaves):
        source_key = str(category.get("key") or f"category_{index}")
        key = _category_key(source_key)
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        confidence = _confidence(
            category.get("confidence"), fallback="estimated_tokens"
        )
        projected.append(
            ContextCategory(
                id=f"category:{source_key}:{index}",
                category=key,
                source_key=source_key,
                label=str(category.get("label") or source_key),
                tokens=TokenEvidence(
                    value=tokens,
                    confidence=confidence,
                    source=f"ct session stats:context.categories.{source_key}",
                ),
                percent=_optional_float(category.get("pct")),
                estimated_cost=_cost_evidence_from_estimate(
                    category.get("estimated_cost")
                ),
            )
        )
    return projected


def _project_provider_usage_buckets(
    stats: dict[str, Any],
) -> list[ContextCategory]:
    projected: list[ContextCategory] = []
    for index, category in enumerate(stats.get("provider_usage_buckets") or []):
        if not isinstance(category, dict):
            continue
        tokens = category.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        source_key = str(category.get("key") or f"provider_bucket_{index}")
        projected.append(
            ContextCategory(
                id=f"provider:{source_key}:{index}",
                category="unattributed",
                source_key=source_key,
                label=str(category.get("label") or source_key),
                tokens=TokenEvidence(
                    value=tokens,
                    confidence=_confidence(
                        category.get("confidence"), fallback="exact_usage"
                    ),
                    source=str(
                        category.get("source")
                        or "ct session stats:provider_usage_buckets"
                    ),
                ),
                percent=_optional_float(category.get("pct")),
                estimated_cost=_cost_evidence_from_estimate(
                    category.get("estimated_cost")
                ),
            )
        )
    return projected


def _project_expensive_items(
    tool_usage: dict[str, Any],
    *,
    session_id: str | None = None,
) -> list[ExpensiveItem]:
    items: list[ExpensiveItem] = []
    for index, item in enumerate(tool_usage.get("tool_items") or []):
        if not isinstance(item, dict):
            continue
        if session_id and str(item.get("session_id") or "") != session_id:
            continue
        real_cost = item.get("allocated_real_token_cost")
        if not isinstance(real_cost, dict):
            continue
        usage = _usage_dict(real_cost)
        estimate = _cost_evidence_from_estimate(item.get("estimated_cost"))
        if estimate is None:
            continue
        events = _tool_item_events(item, index=index)
        event = events[0] if events else None
        category = (
            event.category
            if event
            else _tool_category(str(item.get("tool_name") or ""))
        )
        label = event.label if event else str(item.get("tool_name") or "Tool")
        summary = event.summary if event else str(item.get("input_summary") or "")
        items.append(
            ExpensiveItem(
                item_id=str(item.get("item_id") or f"tool_item_{index}"),
                turn_id=str(item.get("turn_id") or ""),
                category=category,
                label=label,
                summary=summary,
                allocated_usage=usage,
                estimated_cost=estimate,
            )
        )
    return sorted(
        items,
        key=lambda item: (
            item.estimated_cost.value_usd,
            item.allocated_usage.get("uncached_prompt_tokens", 0),
            item.allocated_usage.get("completion_tokens", 0),
        ),
        reverse=True,
    )


def _category_leaves(categories: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for category in categories:
        if not isinstance(category, dict):
            continue
        children = category.get("children") or []
        if children:
            yield from _category_leaves(children)
        else:
            yield category


_STARTING_CONTEXT_KEYS = {
    "base_system",
    "developer_instructions",
    "agents_md",
    "skills",
    "mcp",
    "memory",
}
_USER_INPUT_KEYS = {"user_initial_request", "user_follow_up_requests"}
_AGENT_FILES_KEYS = {
    "context_readfile",
}
_AGENT_AGENT_KEYS = {
    "assistant_messages",
    "final_answer",
    "progress_update",
    "assistant_message",
    "reasoning",
    "editfile",
    "writefile",
    "todolist",
    "subagenttask",
    "sessionhandoff",
}


def _category_key(source_key: str) -> CategoryKey:
    if source_key in _STARTING_CONTEXT_KEYS:
        return "starting_context"
    if source_key in _USER_INPUT_KEYS:
        return "user_input"
    if source_key in _AGENT_FILES_KEYS:
        return "files"
    if source_key == "output" or source_key.startswith("output_"):
        return "output"
    if source_key in _AGENT_AGENT_KEYS or source_key.startswith(
        (
            "tool_editfile",
            "tool_writefile",
            "tool_todolist",
            "tool_subagenttask",
            "tool_sessionhandoff",
            "editfile",
            "writefile",
            "todolist",
            "subagenttask",
            "sessionhandoff",
        )
    ):
        return "agent"
    return "unattributed"


def _category_events(categories: list[ContextCategory]) -> list[ContextEvent]:
    return [
        ContextEvent(
            id=f"event:{category.id}",
            group="before_first_prompt",
            category=category.category,
            label=category.label,
            summary=f"Aggregate context category from {category.source_key}",
            tokens=category.tokens,
            source=category.tokens.source,
            confidence=category.tokens.confidence,
            detail_ref={"stats_category": category.source_key},
            terminal_visible=True,
        )
        for category in categories
        if category.source_key in _STARTING_CONTEXT_KEYS
    ]


def _trajectory_events(
    overview: dict[str, Any],
    usage: dict[str, Any],
    tool_usage: dict[str, Any],
    *,
    turn_id: str | None,
    session_id: str | None = None,
) -> list[ContextEvent]:
    turn_meta = {
        str(item.get("id")): item
        for item in usage.get("turns") or []
        if isinstance(item, dict) and item.get("id")
    }
    tool_events_by_turn = _tool_events_by_turn(tool_usage, session_id=session_id)
    events: list[ContextEvent] = []
    for session in overview.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        current_session_id = str(session.get("id") or overview.get("id") or "")
        if session_id and current_session_id != session_id:
            continue
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            current_turn_id = str(turn.get("id") or "")
            raw_tool_events = list(tool_events_by_turn.get(current_turn_id) or [])
            turn_item = turn_meta.get(current_turn_id) or {}
            turn_runtime = turn_item.get("runtime") or {}
            turn_usage = turn_item.get("usage") or {}
            idle_seconds = _optional_float(turn_runtime.get("wait_before_seconds"))
            re_read_tokens = _usage_token(turn_usage, "uncached_prompt")
            turn_start = len(events)
            request = turn.get("request") or {}
            request_text = _optional_text(request.get("text"))
            if request_text:
                events.append(
                    ContextEvent(
                        id=f"turn:{current_turn_id}:user",
                        group="turn",
                        turn_id=current_turn_id,
                        category="user_input",
                        label="User prompt",
                        summary=request_text,
                        tokens=TokenEvidence(
                            value=_visible_text_size(request_text).tokens,
                            confidence="estimated_tokens",
                            source="ct session overview:request.text length estimate",
                        ),
                        source="ct session overview:request.text",
                        confidence="exact_text",
                        detail_ref={
                            "session_id": current_session_id,
                            "turn_id": current_turn_id,
                        },
                    )
                )
            for index, activity in enumerate(turn.get("activity") or []):
                if not isinstance(activity, dict):
                    continue
                if not activity.get("text") and raw_tool_events:
                    count = activity.get("count")
                    take = count if isinstance(count, int) and count > 0 else 1
                    for _ in range(take):
                        if not raw_tool_events:
                            break
                        events.extend(raw_tool_events.pop(0))
                    continue
                event = _activity_event(
                    activity,
                    session_id=current_session_id,
                    turn_id=current_turn_id,
                    index=index,
                    turn_usage=turn_usage,
                )
                events.append(event)
            while raw_tool_events:
                events.extend(raw_tool_events.pop(0))
            for event in events[turn_start:]:
                event.idle_seconds = idle_seconds
                event.re_read_tokens = re_read_tokens
    if turn_id:
        events = [
            event
            for event in events
            if event.group == "before_first_prompt" or event.turn_id == turn_id
        ]
    return events


def _activity_event(
    activity: dict[str, Any],
    *,
    session_id: str,
    turn_id: str,
    index: int,
    turn_usage: dict[str, Any] | None,
) -> ContextEvent:
    if activity.get("text"):
        text = str(activity["text"])
        return ContextEvent(
            id=f"turn:{turn_id}:activity:{index}",
            group="turn",
            turn_id=turn_id,
            category="agent",
            label="Assistant message",
            summary=text,
            tokens=TokenEvidence(
                value=_visible_text_size(text).tokens,
                confidence="estimated_tokens",
                source="ct session overview:activity.text length estimate",
            ),
            source="ct session overview:activity.text",
            confidence="exact_text",
            detail_ref={"session_id": session_id, "turn_id": turn_id},
        )

    tool = str(activity.get("tool") or "Tool activity")
    summary = _activity_summary(activity)
    detail_ref = {"session_id": session_id, "turn_id": turn_id}
    if turn_usage:
        detail_ref["turn_usage_total"] = str(turn_usage.get("total") or 0)
    return ContextEvent(
        id=f"turn:{turn_id}:activity:{index}",
        group="turn",
        turn_id=turn_id,
        category=_tool_category(tool),
        label=tool,
        summary=summary,
        tokens=None,
        source="ct session overview:activity summary",
        confidence="structural",
        detail_ref=detail_ref,
    )


def _activity_summary(activity: dict[str, Any]) -> str:
    tool = str(activity.get("tool") or "Tool activity")
    count = activity.get("count")
    suffix = f" x{count}" if isinstance(count, int) and count > 1 else ""
    for key in ("cmd", "path", "query", "url"):
        if activity.get(key):
            return f"{tool}{suffix}: {activity[key]}"
    for key in ("paths", "queries", "urls", "targets"):
        values = activity.get(key)
        if isinstance(values, list) and values:
            return f"{tool}{suffix}: {', '.join(str(item) for item in values[:3])}"
    return f"{tool}{suffix}"


def _projection_warnings(events: list[ContextEvent]) -> list[str]:
    has_tool_token_events = any(
        event.id.startswith("tool:") and event.tokens is not None for event in events
    )
    warnings = [
        "Turn usage is cumulative model accounting and is retained as a detail reference, "
        "not presented as context added by one timeline event.",
    ]
    if has_tool_token_events:
        warnings.append(
            "Tool items combine input and output token evidence from session.tool_usage; USD cost remains a "
            "plugin-side estimate over allocated item usage."
        )
    else:
        warnings.append(
            "Timeline user and assistant token deltas estimate only the visible overview text; "
            "tool activity remains structural because overview does not expose per-item result text."
        )
    if not any(event.tokens for event in events):
        warnings.append("No event-level token evidence is available for this session.")
    return warnings


def _usage_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        "prompt_tokens": _optional_int(value.get("prompt_tokens")) or 0,
        "uncached_prompt_tokens": _optional_int(value.get("uncached_prompt_tokens"))
        or 0,
        "cached_prompt_tokens": _optional_int(value.get("cached_prompt_tokens")) or 0,
        "cache_write_tokens": _optional_int(value.get("cache_write_tokens")) or 0,
        "completion_tokens": _optional_int(value.get("completion_tokens")) or 0,
        "reasoning_tokens": _optional_int(value.get("reasoning_tokens")) or 0,
        "processed_tokens": _optional_int(value.get("processed_tokens")) or 0,
        "prompt_completion_tokens": _optional_int(value.get("prompt_completion_tokens"))
        or 0,
    }


def _overview_vendor(overview: dict[str, Any]) -> str | None:
    for session in overview.get("sessions") or []:
        if isinstance(session, dict) and session.get("vendor"):
            return str(session["vendor"])
    return None


def _token_evidence(
    value: Any,
    *,
    confidence: Confidence,
    source: str,
) -> TokenEvidence | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return TokenEvidence(value=value, confidence=confidence, source=source)


def _usage_token(usage: dict[str, Any] | None, short_key: str) -> int | None:
    """Read a usage token by short key (``cached_prompt``) with long-form
    (``cached_prompt_tokens``) fallback. ``ct session usage`` emits the short
    form; ``allocated_real_token_cost`` from tool usage emits the long form.
    """
    if not isinstance(usage, dict):
        return None
    raw = usage.get(short_key)
    if raw is None:
        raw = usage.get(f"{short_key}_tokens")
    return _optional_int(raw)


if __name__ == "__main__":
    raise SystemExit(main())
