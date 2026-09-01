"""Markdown rendering for the Context Window projection."""

from __future__ import annotations

from typing import Any

from datahub_plugin.projections.context_window_models import (
    CacheBreakRecord,
    CacheBreakSummary,
    ContextEvent,
    ContextWindowProjection,
    _category_sort_key,
)


def render_markdown(projection: ContextWindowProjection) -> str:
    context_label = _format_tokens(
        projection.context_window_tokens.value
        if projection.context_window_tokens
        else None
    )
    used_label = _format_tokens(
        projection.used_tokens.value if projection.used_tokens else None
    )
    percent_label = (
        f" ({projection.used_percent:.1f}%)"
        if projection.used_percent is not None
        else ""
    )
    lines = [
        "# Context Window",
        "",
        f"Provider: {projection.vendor}",
        f"Model: {projection.model or '-'} ({context_label} context)",
        f"Used: {used_label}{percent_label}, {len(projection.events)} events",
    ]
    teaser = _cache_breaks_teaser(projection.cache_breaks)
    if teaser:
        lines.append(teaser)
    lines.extend(
        [
            (
                f"Token cost: ${projection.token_cost.value_usd:.4f} "
                f"({projection.token_cost.confidence}, {projection.token_cost.source}"
                f"{', ' + projection.token_cost.effective_date if projection.token_cost.effective_date else ''})"
                if projection.token_cost
                else "Token cost: unavailable"
            ),
            "",
            "Composition",
        ]
    )
    for category in sorted(
        projection.categories,
        key=_category_sort_key,
        reverse=True,
    ):
        cost_label = (
            _format_cost(category.estimated_cost.value_usd)
            if category.estimated_cost
            else "-"
        )
        lines.append(
            f"  {category.category:<20} {_format_delta(category.tokens.value):>8}  "
            f"{cost_label:>9}  {_one_line(category.label, 52)} [{category.tokens.confidence}]"
        )
    if projection.provider_usage_buckets:
        lines.extend(["", "Provider usage buckets"])
        for category in projection.provider_usage_buckets:
            lines.append(
                f"  {category.source_key:<20} {_format_delta(category.tokens.value):>8}  "
                f"{_one_line(category.label, 62)} [{category.tokens.confidence}]"
            )

    if projection.expensive_items:
        lines.extend(["", "Most Expensive Items"])
        for item in projection.expensive_items[:12]:
            usage = item.allocated_usage
            lines.append(
                f"  {_format_cost(item.estimated_cost.value_usd):>9}  "
                f"{_format_tokens(usage.get('uncached_prompt_tokens'))}/"
                f"{_format_tokens(usage.get('cached_prompt_tokens'))}/"
                f"{_format_tokens(usage.get('completion_tokens'))}/"
                f"{_format_tokens(usage.get('reasoning_tokens'))}  "
                f"{item.category:<10} {_one_line(item.label + ': ' + item.summary, 72)}"
            )

    breaks_by_turn = {
        record.turn_id: record
        for record in (
            projection.cache_breaks.events if projection.cache_breaks else []
        )
    }
    current_group: tuple[str, str | None] | None = None
    for event in projection.events:
        group = (event.group, event.turn_id)
        is_turn_start = group != current_group
        current_group = group
        if is_turn_start:
            lines.extend(["", _group_label(event)])
        delta = _format_delta(event.tokens.value) if event.tokens else "       -"
        summary = _one_line(event.summary or event.label, 74)
        # Show the break flag once per turn, on its first (user_input) event.
        flag = (
            _cache_break_flag(breaks_by_turn.get(event.turn_id))
            if is_turn_start
            else None
        )
        line = f"  {event.category:<20} {delta:>8}  {summary}"
        if flag:
            line += f"  {flag}"
        lines.append(line)

    if projection.cache_breaks and projection.cache_breaks.events:
        cb = projection.cache_breaks
        lines.extend(["", "Cache breaks"])
        lines.append(
            f"   #  {'turn':<10}  {'type':<15}  {'idle':>7}  "
            f"{'re-read':>9}  {'est. cost':>11}  cause"
        )
        for index, record in enumerate(cb.events, start=1):
            if record.effort_to:
                effort = (
                    f"{record.effort_from}→{record.effort_to}"
                    if record.effort_from
                    else f"→{record.effort_to}"
                )
            elif record.model_to:
                effort = (
                    f"{record.model_from}→{record.model_to}"
                    if record.model_from
                    else f"→{record.model_to}"
                )
            else:
                effort = "-"
            lines.append(
                f"  {index:>2}  {record.turn_id[:10]:<10}  {record.type:<15}  "
                f"{_format_idle(record.idle_seconds):>7}  "
                f"{_format_tokens(record.re_read_tokens):>9}  "
                f"{_format_cost(record.est_cost_usd):>11}  {effort}"
            )
        type_summary = ", ".join(
            f"{cb.by_type.get(key, 0)} {key}"
            for key in (
                "effort_switch",
                "model_switch",
                "ttl_confirmed",
                "ttl_likely",
                "unattributed",
            )
            if cb.by_type.get(key)
        )
        lines.append(
            f"  total: {cb.count} breaks · "
            f"{_format_tokens(cb.total_re_read_tokens)} re-read tokens · "
            f"{_format_cost(cb.estimated_waste_usd)} wasted"
            + (f" — {type_summary}" if type_summary else "")
        )

    if projection.compaction and projection.compaction.events:
        lines.extend(["", "Compaction timeline"])
        for index, event in enumerate(projection.compaction.events, start=1):
            mechanism = event.mechanism or "-"
            trigger = event.trigger or "-"
            pre = _format_tokens(event.pre_tokens)
            post = _format_tokens(event.post_tokens)
            delta = (
                f"{pre} -> {post}"
                if event.pre_tokens is not None and event.post_tokens is not None
                else "-"
            )
            dropped = (
                _format_tokens(event.dropped_tokens)
                if event.dropped_tokens is not None
                else "-"
            )
            timestamp = _one_line(event.timestamp, 19)
            lines.append(
                f"  {index:>2}  {timestamp:<19} {mechanism:<18} {trigger:<10} {delta:>15} {dropped:>10}"
            )

    if projection.warnings:
        lines.extend(["", "Warnings"])
        lines.extend(
            f"  - {_one_line(warning, 110)}" for warning in projection.warnings
        )
    return "\n".join(lines)


def _format_tokens(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _format_cost(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _format_delta(value: int) -> str:
    return f"+{_format_tokens(value)}"


def _format_idle(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{int(seconds)}s"


def _cache_break_flag(record: CacheBreakRecord | None) -> str | None:
    if record is None:
        return None
    icon = {
        "ttl_confirmed": "⏳",
        "ttl_likely": "⏳",
        "effort_switch": "⚡",
        "model_switch": "🔄",
        "unattributed": "❓",
    }[record.type]
    label = {
        "ttl_confirmed": "TTL break",
        "ttl_likely": "TTL break?",
        "effort_switch": "effort-switch",
        "model_switch": "model-switch",
        "unattributed": "cache miss",
    }[record.type]
    base = (
        f"{icon} {label}: {_format_idle(record.idle_seconds)} idle → "
        f"{_format_tokens(record.re_read_tokens)} re-read"
    )
    if record.effort_to:
        change = (
            f"{record.effort_from}→{record.effort_to}"
            if record.effort_from
            else f"→{record.effort_to}"
        )
        return f"{base} ({change} confirmed)"
    if record.model_to:
        change = (
            f"{record.model_from}→{record.model_to}"
            if record.model_from
            else f"→{record.model_to}"
        )
        return f"{base} ({change})"
    return base


def _cache_breaks_teaser(summary: CacheBreakSummary | None) -> str | None:
    if summary is None or summary.count == 0:
        return None
    type_summary = ", ".join(
        f"{summary.by_type.get(key, 0)} {key}"
        for key in (
            "effort_switch",
            "model_switch",
            "ttl_confirmed",
            "ttl_likely",
            "unattributed",
        )
        if summary.by_type.get(key)
    )
    confirmed = sum(1 for event in summary.events if event.effort_to)
    line = (
        f"Cache breaks: {summary.count} "
        f"({_format_tokens(summary.total_re_read_tokens)} re-read, "
        f"{_format_cost(summary.estimated_waste_usd)} wasted)"
    )
    if type_summary:
        line = f"{line} — {type_summary}"
    if confirmed:
        line = f"{line} ({confirmed} confirmed)"
    return line


def _group_label(event: ContextEvent) -> str:
    if event.group == "before_first_prompt":
        return "Before first prompt"
    if event.group == "post_turn":
        return "After final turn"
    return f"Turn {event.turn_id or '-'}"


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
