from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from coding_trajectory.metrics.models import MetricSource, TokenUsage, TokenUsageObservation
from coding_trajectory.metrics.pricing import ModelsDevCost, ModelsDevModel, PriceRule, estimate_observation_cost, _model_to_price_rule


def test_models_dev_rule_keeps_codex_long_context_threshold() -> None:
    rule = _model_to_price_rule(
        model_id="gpt-5.5",
        model=ModelsDevModel(
            id="gpt-5.5",
            cost=ModelsDevCost(
                input=5.0,
                output=30.0,
                cache_read=0.5,
            ),
        ),
        pricing_date="2026-06-02",
    )

    assert rule is not None
    assert rule.model == "gpt-5.5"
    assert rule.threshold_tokens == 272_000
    assert rule.pricing_source == "https://models.dev/api.json"
    assert rule.pricing_effective_date == "2026-06-02"


def test_estimate_observation_cost_uses_codex_above_threshold_rates() -> None:
    observation = TokenUsageObservation(
        scope_type="step",
        scope_id=uuid4(),
        timestamp=datetime(2026, 6, 2, tzinfo=UTC),
        usage=TokenUsage(
            input_tokens=300_000,
            cached_input_tokens=100_000,
            output_tokens=50_000,
        ),
        provider="codex_cli",
        model="gpt-5.5",
        source=MetricSource(vendor="codex_cli", source_type="step.vendor_data"),
    )

    result = estimate_observation_cost(observation)

    assert result.complete is True
    assert result.model == "gpt-5.5"
    assert result.amount_usd == 4.35
    assert result.breakdown.input_usd == 2.0
    assert result.breakdown.cached_input_usd == 0.1
    assert result.breakdown.output_usd == 2.25


def test_estimate_observation_cost_uses_provider_aware_deepseek_rule() -> None:
    observation = TokenUsageObservation(
        scope_type="step",
        scope_id=uuid4(),
        timestamp=datetime(2026, 6, 2, tzinfo=UTC),
        usage=TokenUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        ),
        provider="deepseek",
        model="deepseek-v4-flash",
        source=MetricSource(vendor="deepseek", source_type="step.vendor_data"),
    )

    result = estimate_observation_cost(
        observation,
        price_rules={
            "deepseek:deepseek-v4-flash": PriceRule(
                "deepseek-v4-flash",
                input_per_mtok=0.14,
                cached_input_per_mtok=0.0028,
                output_per_mtok=0.28,
                pricing_source="https://models.dev/api.json",
                pricing_effective_date="2026-06-01",
            )
        },
    )

    assert result.complete is True
    assert result.amount_usd == 0.42
    assert result.pricing_source == "https://models.dev/api.json"
