"""Tool-usage projection with cache-aware per-item cost attribution."""

from __future__ import annotations

from typing import Any, Iterable

from coding_trajectory.analysis.content_size import scoped_content_size_cache
from coding_trajectory.ingestion.models import SessionGraph, is_tool_shaped_item
from coding_trajectory.metrics._build import (
    _build_full_metrics,
    turn_usage_observations,
)
from coding_trajectory.metrics.attribution.item_costs import (
    _build_item_real_token_cost_projection_for_session,
    _build_tool_items_for_turn,
)
from coding_trajectory.metrics.models import (
    AllocatedRealTokenCost,
    AttributionPolicy,
    ItemRealTokenCostFlat,
    SessionGraphMetrics,
    SessionGraphToolUsageFlat,
    ToolItemFlat,
)
from coding_trajectory.metrics.pricing import PriceRule
from coding_trajectory.token_counter import session_scoped



@session_scoped
def build_session_graph_tool_usage(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    include_item_real_token_costs: bool = True,
    include_advanced_causality: bool = True,
    precomputed_metrics: SessionGraphMetrics | None = None,
) -> dict[str, Any]:
    """Return tool usage plus turn-stable cache-aware item attribution.

    The all-item cost ledger and advanced causal diagnostics are independent
    payload details. Aggregate and per-tool allocation/pricing remain intact
    when either detail is omitted. Defaults retain both detail groups.
    """
    with scoped_content_size_cache():
        return build_session_graph_tool_usage_uncached(
            session_graph,
            turn_id=turn_id,
            include_item_real_token_costs=include_item_real_token_costs,
            include_advanced_causality=include_advanced_causality,
            precomputed_metrics=precomputed_metrics,
        )


def build_session_graph_tool_usage_uncached(
    session_graph: SessionGraph,
    *,
    turn_id: str | None = None,
    include_item_real_token_costs: bool = True,
    include_advanced_causality: bool = True,
    precomputed_metrics: SessionGraphMetrics | None = None,
) -> dict[str, Any]:
    full = precomputed_metrics or _build_full_metrics(session_graph)

    tool_items: list[ToolItemFlat] = []
    item_real_token_costs: list[ItemRealTokenCostFlat] = []
    selected_allocated_costs: list[AllocatedRealTokenCost | None] = []
    pricing_rule_cache: dict[tuple[str | None, str], PriceRule | None] = {}
    for session in session_graph.sessions:
        selected_turns = [
            turn
            for turn in session.turns
            if turn_id is None or str(turn.turn_id) == turn_id
        ]
        selected_turn_ids = {turn.turn_id for turn in selected_turns}
        selected_tool_item_ids = {
            item.item_id
            for turn in selected_turns
            for item in turn.items
            if is_tool_shaped_item(item)
        }
        cost_projection = _build_item_real_token_cost_projection_for_session(
            session,
            selected_turn_ids=selected_turn_ids,
            selected_item_ids=selected_tool_item_ids,
            include_items=include_item_real_token_costs,
            pricing_rule_cache=pricing_rule_cache,
        )
        item_real_token_costs.extend(cost_projection.items)
        selected_allocated_costs.append(cost_projection.allocated_real_token_cost)
        for turn in selected_turns:
            turn_observations = (
                turn_usage_observations(session, turn)
                if include_advanced_causality
                else []
            )
            tool_items.extend(
                _build_tool_items_for_turn(
                    turn,
                    session_id=session.session_id,
                    turn_observations=turn_observations,
                    allocated_real_token_cost_by_item=(
                        cost_projection.allocated_real_token_cost_by_item
                    ),
                    estimated_cost_by_item=cost_projection.estimated_cost_by_item,
                    include_advanced_causality=include_advanced_causality,
                )
            )

    payload = SessionGraphToolUsageFlat(
        root_session_id=full.root_session_id,
        tool_item_count=len(tool_items),
        tool_output_chars=sum(item.output_chars for item in tool_items),
        tool_output_original_tokens=sum(
            item.output_original_tokens or 0 for item in tool_items
        ),
        allocated_real_token_cost=_sum_allocated_real_token_costs(
            selected_allocated_costs
        ),
        item_real_token_costs=(
            item_real_token_costs if include_item_real_token_costs else []
        ),
        tool_items=tool_items,
        attribution_policy=AttributionPolicy(scope="turn_items"),
        warnings=full.warnings,
    ).model_dump(mode="json")
    if not include_item_real_token_costs:
        payload.pop("item_real_token_costs", None)
    return payload


def _sum_allocated_real_token_costs(
    costs: Iterable[AllocatedRealTokenCost | None],
) -> AllocatedRealTokenCost | None:
    present_costs = [cost for cost in costs if cost is not None]
    if not present_costs:
        return None
    return AllocatedRealTokenCost(
        input_tokens=sum(cost.input_tokens for cost in present_costs),
        uncached_input_tokens=sum(cost.uncached_input_tokens for cost in present_costs),
        cached_input_tokens=sum(cost.cached_input_tokens for cost in present_costs),
        cache_creation_input_tokens=sum(
            cost.cache_creation_input_tokens for cost in present_costs
        ),
        output_tokens=sum(cost.output_tokens for cost in present_costs),
        reasoning_output_tokens=sum(
            cost.reasoning_output_tokens for cost in present_costs
        ),
        total_tokens=sum(cost.total_tokens for cost in present_costs),
    )
