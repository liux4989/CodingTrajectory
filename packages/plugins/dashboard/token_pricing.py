"""Dashboard-owned token pricing and context metadata."""

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

TOKENS_PER_MILLION = 1_000_000
MODELS_DEV_SOURCE = "https://models.dev/api.json"
OPENAI_STANDARD_PRICING_SOURCE = (
    "https://developers.openai.com/api/docs/pricing?latest-pricing=standard"
)
OPENAI_STANDARD_PRICING_EFFECTIVE_DATE = "2026-06-30"
_MODELS_DEV_TTL = timedelta(hours=24)
_MODELS_DEV_TIMEOUT_SECONDS = 5
_MODELS_DEV_CACHE_VERSION = 1
_LIVE_RULES_LOCK = threading.Lock()
_LIVE_RULES_CACHE: tuple[datetime, dict[str, "PriceRule"]] | None = None
_THRESHOLD_OVERRIDES = {
    "gpt-5.4": 272_000,
    "gpt-5.5": 272_000,
}


OPENAI_STANDARD_PRICE_RULES: dict[str, "PriceRule"] = {}


class TokenUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @model_validator(mode="before")
    @classmethod
    def _normalize_compact_usage(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            "input_tokens": value.get("prompt_tokens", value.get("input", 0)),
            "cached_input_tokens": value.get(
                "cached_prompt_tokens",
                value.get("cached", 0),
            ),
            "cache_creation_input_tokens": value.get(
                "cache_write_tokens",
                value.get("cache_creation", 0),
            ),
            "output_tokens": value.get("completion_tokens", value.get("output", 0)),
            "reasoning_output_tokens": value.get(
                "reasoning_tokens",
                value.get("reasoning", 0),
            ),
        }


class CostBreakdown(BaseModel):
    input_usd: float = 0.0
    cached_input_usd: float = 0.0
    cache_creation_input_usd: float = 0.0
    output_usd: float = 0.0
    reasoning_output_usd: float = 0.0


class CostEstimate(BaseModel):
    amount_usd: float = 0.0
    pricing_source: str
    pricing_effective_date: str
    model: str
    breakdown: CostBreakdown = Field(default_factory=CostBreakdown)


@dataclass(frozen=True)
class PriceRule:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    cache_creation_input_per_mtok: float | None = None
    reasoning_output_per_mtok: float | None = None
    threshold_tokens: int | None = None
    input_per_mtok_above_threshold: float | None = None
    output_per_mtok_above_threshold: float | None = None
    cached_input_per_mtok_above_threshold: float | None = None
    cache_creation_input_per_mtok_above_threshold: float | None = None
    pricing_source: str = MODELS_DEV_SOURCE
    pricing_effective_date: str = ""


class ModelsDevContextOver200KCost(BaseModel):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None


class ModelsDevCost(BaseModel):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    context_over_200k: ModelsDevContextOver200KCost | None = None


class ModelsDevLimit(BaseModel):
    context: int | None = None


class ModelsDevModel(BaseModel):
    id: str | None = None
    cost: ModelsDevCost | None = None
    limit: ModelsDevLimit | None = None


class ModelsDevProvider(BaseModel):
    id: str | None = None
    models: dict[str, ModelsDevModel]

    @model_validator(mode="after")
    def _fill_model_ids(self) -> "ModelsDevProvider":
        self.models = {
            key: model.model_copy(update={"id": model.id or key})
            for key, model in self.models.items()
        }
        return self


class ModelsDevCatalog(BaseModel):
    providers: dict[str, ModelsDevProvider]

    @model_validator(mode="before")
    @classmethod
    def _normalize_root(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("providers"), dict):
            return value
        return {"providers": value}


class ModelsDevCacheArtifact(BaseModel):
    version: int = _MODELS_DEV_CACHE_VERSION
    fetched_at: datetime
    catalog: ModelsDevCatalog


def estimate_cost(
    usage: dict[str, Any] | None,
    *,
    model: str | None,
    provider: str | None = None,
    now: datetime | None = None,
) -> CostEstimate | None:
    normalized_model = _normalize_model_name(model)
    if normalized_model is None:
        return None
    rules = _load_live_price_rules(now=now or datetime.now(UTC))
    rule = _lookup_price_rule(rules, provider=provider, model=normalized_model)
    if rule is None:
        rule = _lookup_price_rule(
            _openai_standard_price_rules(),
            provider=provider,
            model=normalized_model,
        )
    if rule is None:
        return None
    breakdown = _estimate_usage(
        TokenUsage.model_validate(usage or {}), rule, provider=provider
    )
    amount = sum(breakdown.model_dump().values())
    return CostEstimate(
        amount_usd=_round_usd(amount),
        pricing_source=rule.pricing_source,
        pricing_effective_date=rule.pricing_effective_date,
        model=normalized_model,
        breakdown=breakdown,
    )


def get_model_context_window(
    model: str | None,
    *,
    provider: str | None = None,
    now: datetime | None = None,
) -> int | None:
    normalized_model = _normalize_model_name(model)
    if normalized_model is None:
        return None
    artifact = _load_models_dev_cache(now=now or datetime.now(UTC), refresh=True)
    if artifact is None:
        return None
    normalized_provider = _normalize_provider(provider)
    for provider_key, candidate_provider in artifact.catalog.providers.items():
        provider_id = _normalize_provider(candidate_provider.id or provider_key)
        if normalized_provider and provider_id != normalized_provider:
            continue
        for map_key, candidate_model in candidate_provider.models.items():
            if _normalize_model_name(candidate_model.id or map_key) != normalized_model:
                continue
            if candidate_model.limit and candidate_model.limit.context:
                return candidate_model.limit.context
    return None


def _estimate_usage(
    usage: TokenUsage,
    rule: PriceRule,
    *,
    provider: str | None,
) -> CostBreakdown:
    above_threshold = (
        rule.threshold_tokens is not None and usage.input_tokens > rule.threshold_tokens
    )
    input_rate = _threshold_rate(
        above_threshold,
        rule.input_per_mtok,
        rule.input_per_mtok_above_threshold,
    )
    cached_rate = _threshold_rate(
        above_threshold,
        rule.cached_input_per_mtok,
        rule.cached_input_per_mtok_above_threshold,
    )
    cache_creation_rate = _threshold_rate(
        above_threshold,
        rule.cache_creation_input_per_mtok,
        rule.cache_creation_input_per_mtok_above_threshold,
    )
    output_rate = _threshold_rate(
        above_threshold,
        rule.output_per_mtok,
        rule.output_per_mtok_above_threshold,
    )
    standard_input_tokens = usage.input_tokens
    if not _uses_net_input_convention(provider, rule.model):
        standard_input_tokens = max(
            usage.input_tokens
            - usage.cached_input_tokens
            - usage.cache_creation_input_tokens,
            0,
        )
    return CostBreakdown(
        input_usd=_price(standard_input_tokens, input_rate),
        cached_input_usd=_price(usage.cached_input_tokens, cached_rate),
        cache_creation_input_usd=_price(
            usage.cache_creation_input_tokens,
            cache_creation_rate,
        ),
        output_usd=_price(usage.output_tokens, output_rate),
        reasoning_output_usd=_price(
            usage.reasoning_output_tokens,
            rule.reasoning_output_per_mtok,
        ),
    )


def _threshold_rate(
    above_threshold: bool,
    default: float | None,
    threshold: float | None,
) -> float | None:
    return threshold if above_threshold and threshold is not None else default


def _uses_net_input_convention(provider: str | None, model: str) -> bool:
    if provider:
        return provider.strip().lower() in {"anthropic", "claude", "claude-code"}
    return "claude" in model


def _price(tokens: int, rate_per_mtok: float | None) -> float:
    if tokens <= 0 or rate_per_mtok is None:
        return 0.0
    return _round_usd((tokens / TOKENS_PER_MILLION) * rate_per_mtok)


def _round_usd(value: float) -> float:
    return round(value, 8)


def _load_live_price_rules(*, now: datetime) -> dict[str, PriceRule]:
    global _LIVE_RULES_CACHE
    with _LIVE_RULES_LOCK:
        if _LIVE_RULES_CACHE is not None:
            loaded_at, rules = _LIVE_RULES_CACHE
            if now - loaded_at < _MODELS_DEV_TTL:
                return rules
        artifact = _load_models_dev_cache(now=now, refresh=True)
        rules = _catalog_to_price_rules(artifact) if artifact else {}
        rules = {**_openai_standard_price_rules(), **rules}
        _LIVE_RULES_CACHE = (now, rules)
        return rules


def _openai_standard_price_rules() -> dict[str, PriceRule]:
    global OPENAI_STANDARD_PRICE_RULES
    if OPENAI_STANDARD_PRICE_RULES:
        return OPENAI_STANDARD_PRICE_RULES
    rules = {
        _provider_model_key("openai", rule.model): rule
        for rule in [
            _openai_rule("gpt-5.5", input_rate=5.0, cached_rate=0.5, output_rate=30.0),
            _openai_rule(
                "gpt-5.5-codex", input_rate=5.0, cached_rate=0.5, output_rate=30.0
            ),
            _openai_rule(
                "gpt-5.5-pro", input_rate=30.0, cached_rate=None, output_rate=180.0
            ),
            _openai_rule("gpt-5.4", input_rate=2.5, cached_rate=0.25, output_rate=15.0),
            _openai_rule(
                "gpt-5.4-mini", input_rate=0.75, cached_rate=0.075, output_rate=4.5
            ),
            _openai_rule(
                "gpt-5.4-nano", input_rate=0.2, cached_rate=0.02, output_rate=1.25
            ),
            _openai_rule(
                "gpt-5.4-pro", input_rate=30.0, cached_rate=None, output_rate=180.0
            ),
            _openai_rule(
                "gpt-5.3-codex", input_rate=5.0, cached_rate=0.5, output_rate=30.0
            ),
            _openai_rule("gpt-4.1", input_rate=2.0, cached_rate=0.5, output_rate=8.0),
            _openai_rule(
                "gpt-4.1-mini", input_rate=0.4, cached_rate=0.1, output_rate=1.6
            ),
            _openai_rule(
                "gpt-4.1-nano", input_rate=0.1, cached_rate=0.025, output_rate=0.4
            ),
            _openai_rule("gpt-4o", input_rate=2.5, cached_rate=1.25, output_rate=10.0),
            _openai_rule(
                "gpt-4o-mini", input_rate=0.15, cached_rate=0.075, output_rate=0.6
            ),
            _openai_rule("o3", input_rate=2.0, cached_rate=0.5, output_rate=8.0),
            _openai_rule("o3-pro", input_rate=20.0, cached_rate=None, output_rate=80.0),
            _openai_rule("o4-mini", input_rate=1.1, cached_rate=0.275, output_rate=4.4),
        ]
    }
    OPENAI_STANDARD_PRICE_RULES = {
        **rules,
        **{rule.model: rule for rule in rules.values()},
    }
    return OPENAI_STANDARD_PRICE_RULES


def _openai_rule(
    model: str,
    *,
    input_rate: float,
    cached_rate: float | None,
    output_rate: float,
) -> PriceRule:
    return PriceRule(
        model=model,
        input_per_mtok=input_rate,
        cached_input_per_mtok=cached_rate,
        output_per_mtok=output_rate,
        pricing_source=OPENAI_STANDARD_PRICING_SOURCE,
        pricing_effective_date=OPENAI_STANDARD_PRICING_EFFECTIVE_DATE,
    )


def _load_models_dev_cache(
    *,
    now: datetime,
    refresh: bool,
) -> ModelsDevCacheArtifact | None:
    artifact = _read_models_dev_cache(now=now)
    if artifact is None and refresh and _should_refresh_live_pricing():
        artifact = _refresh_models_dev_cache(now=now)
    return artifact


def _read_models_dev_cache(*, now: datetime) -> ModelsDevCacheArtifact | None:
    try:
        artifact = ModelsDevCacheArtifact.model_validate_json(
            _models_dev_cache_path().read_text()
        )
    except (OSError, ValidationError):
        return None
    if now - artifact.fetched_at.astimezone(UTC) > _MODELS_DEV_TTL:
        return None
    return artifact


def _refresh_models_dev_cache(*, now: datetime) -> ModelsDevCacheArtifact | None:
    request = Request(
        MODELS_DEV_SOURCE, headers={"User-Agent": "coding-trajectory-dashboard/1.0"}
    )
    try:
        with urlopen(request, timeout=_MODELS_DEV_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        catalog = ModelsDevCatalog.model_validate(payload)
    except (OSError, URLError, TimeoutError, json.JSONDecodeError, ValidationError):
        return None
    artifact = ModelsDevCacheArtifact(fetched_at=now, catalog=catalog)
    cache_path = _models_dev_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(artifact.model_dump_json(indent=2))
    except OSError:
        pass
    return artifact


def _should_refresh_live_pricing() -> bool:
    return os.environ.get("CT_DASHBOARD_DISABLE_LIVE_PRICING") not in {
        "1",
        "true",
        "TRUE",
    }


def _models_dev_cache_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_root) if cache_root else Path.home() / ".cache"
    return (
        base
        / "coding-trajectory"
        / "dashboard"
        / "model-pricing"
        / f"models-dev-v{_MODELS_DEV_CACHE_VERSION}.json"
    )


def _catalog_to_price_rules(
    artifact: ModelsDevCacheArtifact,
) -> dict[str, PriceRule]:
    pricing_date = artifact.fetched_at.date().isoformat()
    rules: dict[str, PriceRule] = {}
    for provider_key, provider in artifact.catalog.providers.items():
        provider_id = _normalize_provider(provider.id or provider_key)
        if provider_id is None:
            continue
        for map_key, model in provider.models.items():
            rule = _model_to_price_rule(
                model_id=model.id or map_key,
                model=model,
                pricing_date=pricing_date,
            )
            if rule is None:
                continue
            rules[_provider_model_key(provider_id, rule.model)] = rule
            rules.setdefault(rule.model, rule)
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
    threshold_cost = model.cost.context_over_200k
    threshold_tokens = 200_000 if threshold_cost else None
    threshold_tokens = _THRESHOLD_OVERRIDES.get(normalized_model, threshold_tokens)
    return PriceRule(
        model=normalized_model,
        input_per_mtok=model.cost.input,
        output_per_mtok=model.cost.output,
        cached_input_per_mtok=model.cost.cache_read,
        cache_creation_input_per_mtok=model.cost.cache_write,
        threshold_tokens=threshold_tokens,
        input_per_mtok_above_threshold=threshold_cost.input if threshold_cost else None,
        output_per_mtok_above_threshold=threshold_cost.output
        if threshold_cost
        else None,
        cached_input_per_mtok_above_threshold=(
            threshold_cost.cache_read if threshold_cost else None
        ),
        cache_creation_input_per_mtok_above_threshold=(
            threshold_cost.cache_write if threshold_cost else None
        ),
        pricing_effective_date=pricing_date,
    )


def _lookup_price_rule(
    rules: dict[str, PriceRule],
    *,
    provider: str | None,
    model: str,
) -> PriceRule | None:
    normalized_provider = _normalize_provider(provider)
    if normalized_provider:
        rule = rules.get(_provider_model_key(normalized_provider, model))
        if rule is not None:
            return rule
    return rules.get(model)


def _provider_model_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _normalize_provider(provider: str | None) -> str | None:
    if not provider:
        return None
    normalized = provider.strip().lower()
    return {
        "openai-codex": "openai",
        "codex_cli": "openai",
        "claude_code": "anthropic",
    }.get(normalized, normalized)


def _normalize_model_name(model: str | None) -> str | None:
    if not model:
        return None
    normalized = model.strip().lower()
    normalized = normalized.removeprefix("openai/").removeprefix("anthropic.")
    if "." in normalized and "claude-" in normalized:
        normalized = normalized[normalized.find("claude-") :]
    normalized = re.sub(r"-\d{8}$", "", normalized)
    normalized = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", normalized)
    return normalized
