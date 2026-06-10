"""Estimated model pricing for token usage observations."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, ValidationError, model_validator

from coding_trajectory.metrics.models import CostBreakdown, CostEstimate, TokenUsage, TokenUsageObservation

_TOKENS_PER_MILLION = 1_000_000
_MODELS_DEV_SOURCE = "https://models.dev/api.json"
_MODELS_DEV_TTL = timedelta(hours=24)
_MODELS_DEV_TIMEOUT_SECONDS = 5
_MODELS_DEV_CACHE_VERSION = 1
_LIVE_RULES_LOCK = threading.Lock()
_LIVE_RULES_CACHE: tuple[datetime, dict[str, "PriceRule"]] | None = None

_THRESHOLD_OVERRIDES: dict[str, int] = {
    "gpt-5.4": 272_000,
    "gpt-5.5": 272_000,
}


@dataclass(frozen=True)
class PriceRule:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    reasoning_output_per_mtok: float | None = None
    threshold_tokens: int | None = None
    input_per_mtok_above_threshold: float | None = None
    output_per_mtok_above_threshold: float | None = None
    cached_input_per_mtok_above_threshold: float | None = None
    pricing_source: str = "builtin"
    pricing_effective_date: str = "2026-05-01"


class ModelsDevContextOver200KCost(BaseModel):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = Field(default=None, alias="cache_read")
    cache_write: float | None = Field(default=None, alias="cache_write")


class ModelsDevCost(BaseModel):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = Field(default=None, alias="cache_read")
    cache_write: float | None = Field(default=None, alias="cache_write")
    context_over_200k: ModelsDevContextOver200KCost | None = Field(
        default=None, alias="context_over_200k"
    )


class ModelsDevLimit(BaseModel):
    context: int | None = None


class ModelsDevModel(BaseModel):
    id: str | None = None
    name: str | None = None
    cost: ModelsDevCost | None = None
    limit: ModelsDevLimit | None = None


class ModelsDevProvider(BaseModel):
    id: str | None = None
    name: str | None = None
    models: dict[str, ModelsDevModel]

    @model_validator(mode="after")
    def _fill_model_ids(self) -> "ModelsDevProvider":
        normalized: dict[str, ModelsDevModel] = {}
        for key, model in self.models.items():
            model_id = model.id or key
            normalized[key] = model.model_copy(update={"id": model_id})
        self.models = normalized
        return self


class ModelsDevCatalog(BaseModel):
    providers: dict[str, ModelsDevProvider]

    @model_validator(mode="before")
    @classmethod
    def _normalize_root(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "providers" in value and isinstance(value["providers"], dict):
            return value
        return {"providers": value}


class ModelsDevCacheArtifact(BaseModel):
    version: int = _MODELS_DEV_CACHE_VERSION
    fetched_at: datetime
    catalog: ModelsDevCatalog


def get_default_price_rules(*, now: datetime | None = None) -> dict[str, PriceRule]:
    return _load_live_price_rules(now=now or datetime.now(UTC))


def get_model_context_window(
    model: str | None,
    *,
    provider: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """Lookup a model's context window (in tokens) from the cached models.dev catalog."""
    if not model:
        return None
    artifact = _load_models_dev_cache(now=now or datetime.now(UTC))
    if artifact is None:
        return None
    normalized_model = _normalize_model_name(model)
    normalized_provider = _normalize_provider(provider)
    for provider_key, prov in artifact.catalog.providers.items():
        prov_id = _normalize_provider(prov.id or provider_key)
        if normalized_provider and prov_id != normalized_provider:
            continue
        for map_key, dev_model in prov.models.items():
            candidate = _normalize_model_name(dev_model.id or map_key)
            if candidate != normalized_model:
                continue
            if dev_model.limit and dev_model.limit.context:
                return dev_model.limit.context
    return None


def estimate_observation_cost(
    observation: TokenUsageObservation,
    *,
    extra_billing: bool = False,
    price_rules: dict[str, PriceRule] | None = None,
) -> CostEstimate:
    rules = price_rules or get_default_price_rules()
    model = _normalize_model_name(observation.model)
    if model is None:
        return _missing(extra_billing=extra_billing, model=None, reason="missing model")

    rule = _lookup_price_rule(rules, provider=observation.provider, model=model)
    if rule is None:
        if not rules:
            return _missing(
                extra_billing=extra_billing,
                model=model,
                reason=f"pricing unavailable for model {model} (models.dev catalog not cached)",
            )
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
    uses_threshold_pricing = (
        rule.threshold_tokens is not None and usage.input_tokens > rule.threshold_tokens
    )
    cached_input_rate = (
        rule.cached_input_per_mtok_above_threshold
        if uses_threshold_pricing and rule.cached_input_per_mtok_above_threshold is not None
        else rule.cached_input_per_mtok
    )
    input_rate = (
        rule.input_per_mtok_above_threshold
        if uses_threshold_pricing and rule.input_per_mtok_above_threshold is not None
        else rule.input_per_mtok
    )
    output_rate = (
        rule.output_per_mtok_above_threshold
        if uses_threshold_pricing and rule.output_per_mtok_above_threshold is not None
        else rule.output_per_mtok
    )
    reasoning_output_rate = rule.reasoning_output_per_mtok

    standard_input_tokens = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return CostBreakdown(
        input_usd=_price(standard_input_tokens, input_rate),
        cached_input_usd=_price(usage.cached_input_tokens, cached_input_rate),
        output_usd=_price(usage.output_tokens, output_rate),
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


def _normalize_model_name(model: str | None) -> str | None:
    if not model:
        return None
    normalized = model.strip().lower()
    if normalized.startswith("openai/"):
        normalized = normalized.removeprefix("openai/")
    if normalized.startswith("anthropic."):
        normalized = normalized.removeprefix("anthropic.")
    if "." in normalized and "claude-" in normalized:
        idx = normalized.find("claude-")
        if idx > 0:
            normalized = normalized[idx:]
    compact_date = re.search(r"-\d{8}$", normalized)
    if compact_date is not None:
        normalized = normalized[: compact_date.start()]
    dashed_date = re.search(r"-\d{4}-\d{2}-\d{2}$", normalized)
    if dashed_date is not None:
        normalized = normalized[: dashed_date.start()]
    return normalized


def _normalize_provider(provider: str | None) -> str | None:
    if not provider:
        return None
    normalized = provider.strip().lower()
    aliases = {
        "openai-codex": "openai",
        "openai": "openai",
        "anthropic": "anthropic",
        "deepseek": "deepseek",
    }
    return aliases.get(normalized, normalized)


def _lookup_price_rule(
    rules: dict[str, PriceRule],
    *,
    provider: str | None,
    model: str,
) -> PriceRule | None:
    normalized_provider = _normalize_provider(provider)
    if normalized_provider:
        provider_key = _provider_model_key(normalized_provider, model)
        if provider_key in rules:
            return rules[provider_key]
    return rules.get(model)


def _provider_model_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _load_live_price_rules(*, now: datetime) -> dict[str, PriceRule]:
    global _LIVE_RULES_CACHE

    with _LIVE_RULES_LOCK:
        if _LIVE_RULES_CACHE is not None:
            loaded_at, rules = _LIVE_RULES_CACHE
            if now - loaded_at < _MODELS_DEV_TTL:
                return rules

        artifact = _load_models_dev_cache(now=now)
        if artifact is None and _should_refresh_live_pricing():
            artifact = _refresh_models_dev_cache(now=now)
        rules = _catalog_to_price_rules(artifact) if artifact is not None else {}
        _LIVE_RULES_CACHE = (now, rules)
        return rules


def _should_refresh_live_pricing() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return os.environ.get("CT_DISABLE_LIVE_PRICING") not in {"1", "true", "TRUE"}


def _models_dev_cache_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_root) if cache_root else Path.home() / ".cache"
    return base / "coding-trajectory" / "model-pricing" / f"models-dev-v{_MODELS_DEV_CACHE_VERSION}.json"


def _load_models_dev_cache(*, now: datetime) -> ModelsDevCacheArtifact | None:
    cache_path = _models_dev_cache_path()
    try:
        payload = json.loads(cache_path.read_text())
        artifact = ModelsDevCacheArtifact.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        return None

    fetched_at = artifact.fetched_at.astimezone(UTC)
    if now - fetched_at > _MODELS_DEV_TTL:
        return None
    return artifact


def _refresh_models_dev_cache(*, now: datetime) -> ModelsDevCacheArtifact | None:
    request = Request(_MODELS_DEV_SOURCE, headers={"User-Agent": "coding-trajectory/1.0"})
    try:
        with urlopen(request, timeout=_MODELS_DEV_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None

    try:
        catalog = ModelsDevCatalog.model_validate(payload)
    except ValidationError:
        return None

    artifact = ModelsDevCacheArtifact(fetched_at=now, catalog=catalog)
    cache_path = _models_dev_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(artifact.model_dump_json(indent=2))
    return artifact


def _catalog_to_price_rules(artifact: ModelsDevCacheArtifact) -> dict[str, PriceRule]:
    pricing_date = artifact.fetched_at.date().isoformat()
    rules: dict[str, PriceRule] = {}
    for provider_key, provider in artifact.catalog.providers.items():
        provider_id = _normalize_provider(provider.id or provider_key)
        if provider_id is None:
            continue
        for map_key, model in provider.models.items():
            price_rule = _model_to_price_rule(
                model_id=model.id or map_key,
                model=model,
                pricing_date=pricing_date,
            )
            if price_rule is None:
                continue
            rules[_provider_model_key(provider_id, price_rule.model)] = price_rule
            if price_rule.model not in rules:
                rules[price_rule.model] = price_rule
    return rules


def _model_to_price_rule(
    *,
    model_id: str,
    model: ModelsDevModel,
    pricing_date: str,
) -> PriceRule | None:
    if model.cost is None or model.cost.input is None or model.cost.output is None:
        return None
    normalized_model = _normalize_model_name(model_id)
    if normalized_model is None:
        return None
    context_over_200k = model.cost.context_over_200k
    threshold_tokens = 200_000 if context_over_200k is not None else None
    if normalized_model in _THRESHOLD_OVERRIDES:
        threshold_tokens = _THRESHOLD_OVERRIDES[normalized_model]

    return PriceRule(
        model=normalized_model,
        input_per_mtok=model.cost.input,
        output_per_mtok=model.cost.output,
        cached_input_per_mtok=model.cost.cache_read,
        threshold_tokens=threshold_tokens,
        input_per_mtok_above_threshold=context_over_200k.input if context_over_200k else None,
        output_per_mtok_above_threshold=context_over_200k.output if context_over_200k else None,
        cached_input_per_mtok_above_threshold=context_over_200k.cache_read if context_over_200k else None,
        pricing_source=_MODELS_DEV_SOURCE,
        pricing_effective_date=pricing_date,
    )
