"""Request-priced recorded cost and net prompt-cache savings for stats."""

from coding_trajectory.metrics.models import (
    CacheSavingsEvidenceFlat,
    CostEvidenceFlat,
    CostSummaryFlat,
    SessionGraphMetrics,
    SessionMetrics,
)
from coding_trajectory.metrics.pricing import cost_evidence_from_usage


def cost_summary_from_metrics(
    metrics: SessionGraphMetrics | SessionMetrics,
) -> CostSummaryFlat | None:
    sessions = metrics.sessions if isinstance(metrics, SessionGraphMetrics) else [metrics]
    observations = [
        observation
        for session in sessions
        for turn in session.turns
        for observation in turn.observations
    ]
    if not observations:
        return None
    totals: list[CostEvidenceFlat] = []
    savings: list[CostEvidenceFlat] = []
    no_cache_costs: list[CostEvidenceFlat] = []
    for observation in observations:
        usage = observation.usage.model_dump(mode="json")
        kwargs = {"model": observation.model, "provider": observation.provider}
        total = cost_evidence_from_usage(usage, **kwargs)
        if total is not None:
            totals.append(total)

        # Compare two list-price estimates, never a reported charge with a
        # hypothetical list price. Output and request pricing tier stay fixed.
        usage.pop("cost_usd", None)
        cached_cost = cost_evidence_from_usage(usage, **kwargs)
        uncached = observation.usage.uncached_input_tokens
        if uncached is None or cached_cost is None:
            continue
        input_tokens = (
            uncached
            + observation.usage.cached_input_tokens
            + observation.usage.cache_creation_input_tokens
        )
        no_cache_usage = {
            **usage,
            "prompt_tokens": input_tokens,
            "uncached_prompt_tokens": input_tokens,
            "cached_prompt_tokens": 0,
            "cache_write_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
        }
        no_cache_cost = cost_evidence_from_usage(
            no_cache_usage, **kwargs, pricing_input_tokens=input_tokens
        )
        if no_cache_cost is None:
            continue
        no_cache_costs.append(no_cache_cost)
        savings.append(
            CacheSavingsEvidenceFlat(
                value_usd=round(no_cache_cost.value_usd - cached_cost.value_usd, 8),
                source=cached_cost.source,
                effective_date=cached_cost.effective_date,
            )
        )

    count = len(observations)

    def aggregate(
        values: list[CostEvidenceFlat], *, signed: bool = False
    ) -> CostEvidenceFlat | None:
        # A partial sum is not a session total. Missing prices remain unknown.
        if len(values) != count:
            return None
        dates = {value.effective_date for value in values if value.effective_date}
        evidence_type = CacheSavingsEvidenceFlat if signed else CostEvidenceFlat
        return evidence_type(
            value_usd=round(sum(value.value_usd for value in values), 8),
            confidence=(
                "reported" if all(value.confidence == "reported" for value in values)
                else "estimated"
            ),
            source="request-priced recorded aggregate",
            effective_date=next(iter(dates)) if len(dates) == 1 else None,
        )

    return CostSummaryFlat(
        requests=count,
        priced_requests=len(totals),
        total_cost=aggregate(totals),
        cache_savings=aggregate(savings, signed=True),
        no_cache_cost=aggregate(no_cache_costs),
    )
