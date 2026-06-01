"""Estimated model pricing for token usage observations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from coding_trajectory.metrics.models import CostBreakdown, CostEstimate, TokenUsage, TokenUsageObservation

_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class PriceRule:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    reasoning_output_per_mtok: float | None = None
    pricing_source: str = "builtin"
    pricing_effective_date: str = "2026-05-01"


_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
_ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


DEFAULT_PRICE_RULES: dict[str, PriceRule] = {
    "gpt-5": PriceRule("gpt-5", input_per_mtok=1.25, cached_input_per_mtok=0.125, output_per_mtok=10.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5-codex": PriceRule("gpt-5-codex", input_per_mtok=1.25, cached_input_per_mtok=0.125, output_per_mtok=10.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5-mini": PriceRule("gpt-5-mini", input_per_mtok=0.25, cached_input_per_mtok=0.025, output_per_mtok=2.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5-nano": PriceRule("gpt-5-nano", input_per_mtok=0.05, cached_input_per_mtok=0.005, output_per_mtok=0.40, pricing_source=_OPENAI_SOURCE),
    "gpt-5-pro": PriceRule("gpt-5-pro", input_per_mtok=15.00, output_per_mtok=120.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.1": PriceRule("gpt-5.1", input_per_mtok=1.25, cached_input_per_mtok=0.125, output_per_mtok=10.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.1-codex": PriceRule("gpt-5.1-codex", input_per_mtok=1.25, cached_input_per_mtok=0.125, output_per_mtok=10.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.1-codex-max": PriceRule("gpt-5.1-codex-max", input_per_mtok=1.25, cached_input_per_mtok=0.125, output_per_mtok=10.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.1-codex-mini": PriceRule("gpt-5.1-codex-mini", input_per_mtok=0.25, cached_input_per_mtok=0.025, output_per_mtok=2.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.2": PriceRule("gpt-5.2", input_per_mtok=1.75, cached_input_per_mtok=0.175, output_per_mtok=14.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.2-codex": PriceRule("gpt-5.2-codex", input_per_mtok=1.75, cached_input_per_mtok=0.175, output_per_mtok=14.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.2-pro": PriceRule("gpt-5.2-pro", input_per_mtok=21.00, output_per_mtok=168.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.3-codex": PriceRule("gpt-5.3-codex", input_per_mtok=1.75, cached_input_per_mtok=0.175, output_per_mtok=14.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.3-codex-spark": PriceRule("gpt-5.3-codex-spark", input_per_mtok=0.00, cached_input_per_mtok=0.00, output_per_mtok=0.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.5": PriceRule("gpt-5.5", input_per_mtok=5.00, cached_input_per_mtok=0.50, output_per_mtok=30.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.4": PriceRule("gpt-5.4", input_per_mtok=2.50, cached_input_per_mtok=0.25, output_per_mtok=15.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.4-mini": PriceRule("gpt-5.4-mini", input_per_mtok=0.75, cached_input_per_mtok=0.075, output_per_mtok=4.50, pricing_source=_OPENAI_SOURCE),
    "gpt-5.4-nano": PriceRule("gpt-5.4-nano", input_per_mtok=0.20, cached_input_per_mtok=0.02, output_per_mtok=1.25, pricing_source=_OPENAI_SOURCE),
    "gpt-5.4-pro": PriceRule("gpt-5.4-pro", input_per_mtok=30.00, output_per_mtok=180.00, pricing_source=_OPENAI_SOURCE),
    "gpt-5.5-pro": PriceRule("gpt-5.5-pro", input_per_mtok=30.00, output_per_mtok=180.00, pricing_source=_OPENAI_SOURCE),
    "claude-opus-4-7": PriceRule("claude-opus-4-7", input_per_mtok=5.00, output_per_mtok=25.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-opus-4-6": PriceRule("claude-opus-4-6", input_per_mtok=5.00, output_per_mtok=25.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-opus-4-5": PriceRule("claude-opus-4-5", input_per_mtok=5.00, output_per_mtok=25.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-opus-4-1": PriceRule("claude-opus-4-1", input_per_mtok=15.00, output_per_mtok=75.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-sonnet-4-6": PriceRule("claude-sonnet-4-6", input_per_mtok=3.00, output_per_mtok=15.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-sonnet-4-5": PriceRule("claude-sonnet-4-5", input_per_mtok=3.00, output_per_mtok=15.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-sonnet-4": PriceRule("claude-sonnet-4", input_per_mtok=3.00, output_per_mtok=15.00, pricing_source=_ANTHROPIC_SOURCE),
    "claude-haiku-4-5": PriceRule("claude-haiku-4-5", input_per_mtok=1.00, output_per_mtok=5.00, pricing_source=_ANTHROPIC_SOURCE),
}


def estimate_observation_cost(
    observation: TokenUsageObservation,
    *,
    extra_billing: bool = False,
    price_rules: dict[str, PriceRule] | None = None,
) -> CostEstimate:
    rules = price_rules or DEFAULT_PRICE_RULES
    model = _normalize_model(observation.model)
    if model is None:
        return _missing(extra_billing=extra_billing, model=None, reason="missing model")

    rule = rules.get(model)
    if rule is None:
        return _missing(extra_billing=extra_billing, model=model, reason=f"no price rule for model {model}")

    usage = observation.usage
    breakdown = _estimate_usage(usage, rule)
    amount = (
        breakdown.input_usd
        + breakdown.cached_input_usd
        + breakdown.output_usd
        + breakdown.reasoning_output_usd
    )
    return CostEstimate(
        amount_usd=_round_usd(amount),
        extra_billing=extra_billing,
        pricing_source=rule.pricing_source,
        pricing_effective_date=rule.pricing_effective_date,
        model=model,
        complete=True,
        breakdown=breakdown,
    )


def _estimate_usage(usage: TokenUsage, rule: PriceRule) -> CostBreakdown:
    cached_input_rate = rule.cached_input_per_mtok
    reasoning_output_rate = rule.reasoning_output_per_mtok

    standard_input_tokens = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return CostBreakdown(
        input_usd=_price(standard_input_tokens, rule.input_per_mtok),
        cached_input_usd=_price(usage.cached_input_tokens, cached_input_rate),
        output_usd=_price(usage.output_tokens, rule.output_per_mtok),
        reasoning_output_usd=_price(usage.reasoning_output_tokens, reasoning_output_rate),
    )


def _missing(*, extra_billing: bool, model: str | None, reason: str) -> CostEstimate:
    return CostEstimate(
        extra_billing=extra_billing,
        model=model,
        complete=False,
        missing_reasons=[reason],
    )


def _price(tokens: int, rate_per_mtok: float | None) -> float:
    if tokens <= 0 or rate_per_mtok is None:
        return 0.0
    return _round_usd((tokens / _TOKENS_PER_MILLION) * rate_per_mtok)


def _round_usd(value: float) -> float:
    return round(value, 8)


def _normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    normalized = model.strip().lower()
    if normalized.startswith("openai/"):
        normalized = normalized.removeprefix("openai/")
    if normalized.startswith("anthropic."):
        normalized = normalized.removeprefix("anthropic.")
    if "." in normalized and "claude-" in normalized:
        tail = normalized.rsplit(".", maxsplit=1)[-1]
        if tail.startswith("claude-"):
            normalized = tail
    if normalized in DEFAULT_PRICE_RULES:
        return normalized
    compact_date = re.search(r"-\d{8}$", normalized)
    if compact_date is not None:
        candidate = normalized[: compact_date.start()]
        if candidate in DEFAULT_PRICE_RULES:
            return candidate
    dashed_date = re.search(r"-\d{4}-\d{2}-\d{2}$", normalized)
    if dashed_date is not None:
        candidate = normalized[: dashed_date.start()]
        if candidate in DEFAULT_PRICE_RULES:
            return candidate
    return normalized
