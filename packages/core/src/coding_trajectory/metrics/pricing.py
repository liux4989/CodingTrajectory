"""Single source of truth for model pricing and context-window resolution.

Owns per-model USD cost evidence (``cost_evidence_from_usage``), prompt-cache
break waste (``cache_break_waste_usd``), and context-window resolution
(``get_model_context_window``). The dashboard plugin and any other consumer
read cost off the ``ct`` JSON this module populates; no pricing math lives in
the plugin anymore.

Context-window resolution tiers: the Claude Code alias suffix
(e.g. ``glm-5.2[1m]`` -> 1_000_000), then a curated static map (offline-safe,
used by ingestion), then the live https://models.dev catalog (24h disk cache,
gated by ``CT_DISABLE_LIVE_PRICING`` so the ingestion hot path can stay
offline). Pricing is always the live catalog (with the OpenAI standard rules
as a static fallback) since static per-model rates are too coarse.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import (
    BaseModel,
    ValidationError,
    model_validator,
)

from coding_trajectory.metrics.models import CostEvidenceFlat

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
_PRICING_RULE_UNSET = object()

# Disable live models.dev fetches (offline ingestion / sandboxed runs). When
# set, only the static curated context-window map and OpenAI standard price
# rules are used.
_DISABLE_LIVE_ENV = "CT_DISABLE_LIVE_PRICING"
_LEGACY_DISABLE_LIVE_ENV = "CT_DASHBOARD_DISABLE_LIVE_PRICING"

OPENAI_STANDARD_PRICE_RULES: dict[str, "PriceRule"] = {}

# ---------------------------------------------------------------------------
# Static context-window catalog (offline fallback, curated from models.dev)
# ---------------------------------------------------------------------------

# Claude Code model-alias context suffix, e.g. "glm-5.2[1m]" -> 1_000_000.
_ALIAS_WINDOW_RE = re.compile(
    r"\[(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>[km])\]$", re.IGNORECASE
)

_PROVIDER_PREFIXES: Final[tuple[str, ...]] = (
    "anthropic/",
    "openai/",
    "z-ai/",
    "zai/",
    "zai-org/",
    "zhipu/",
    "moonshot/",
    "minimax/",
    "anthropic.",
)

_VARIANT_SUFFIX_RE = re.compile(
    r"(?:-(?:thinking|think|fast|latest|free|highspeed|flex|turbo|lightning|reasoning-distilled))+$"
)
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$|-\d{4}-\d{2}-\d{2}$")

# Curated from https://models.dev (Anthropic-native / Zhipu-native entries).
# Values are the model's max input context in tokens.
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # --- Claude (Anthropic) ---
    "claude-3-haiku": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-opus-4-0": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-0": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    # --- GLM (Zhipu) ---
    "glm-4.5": 131_072,
    "glm-4.6": 204_800,
    "glm-4.7": 204_800,
    "glm-5": 204_800,
    "glm-5.1": 200_000,
    "glm-5.2": 1_000_000,
    # --- Kimi (Moonshot) ---
    "kimi-k2.7-code": 262_144,
    # --- MiniMax ---
    "minimax-m3": 512_000,
}


def _usage_int(usage: dict[str, Any], primary: str, fallback: str) -> int:
    value = usage.get(primary, usage.get(fallback, 0))
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _estimate_cost_from_ints(
    input_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    *,
    model: str | None,
    provider: str | None,
    pricing_input_tokens: int | None = None,
    pricing_rule: PriceRule | None | object = _PRICING_RULE_UNSET,
) -> tuple[float, str, str] | None:
    """Fast-path cost estimate returning (amount_usd, pricing_source, effective_date).

    Takes token ints directly (avoiding per-call pydantic overhead) for the
    2M+ call hot path in tool-usage attribution.
    """
    if pricing_rule is _PRICING_RULE_UNSET:
        rule = _resolve_price_rule(model, provider=provider)
    else:
        rule = pricing_rule
    if rule is None:
        return None

    tier_input_tokens = (
        input_tokens
        if pricing_input_tokens is None
        else max(pricing_input_tokens, 0)
    )
    above_threshold = (
        rule.threshold_tokens is not None and tier_input_tokens > rule.threshold_tokens
    )
    input_rate = _threshold_rate(
        above_threshold, rule.input_per_mtok, rule.input_per_mtok_above_threshold
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
        above_threshold, rule.output_per_mtok, rule.output_per_mtok_above_threshold
    )
    standard_input_tokens = input_tokens
    if not _uses_net_input_convention(provider, rule.model):
        standard_input_tokens = max(
            input_tokens - cached_input_tokens - cache_creation_input_tokens, 0
        )
    amount = _round_usd(
        _price(standard_input_tokens, input_rate)
        + _price(cached_input_tokens, cached_rate)
        + _price(cache_creation_input_tokens, cache_creation_rate)
        + _price(output_tokens, output_rate)
        + _price(reasoning_output_tokens, rule.reasoning_output_per_mtok)
    )
    return (amount, rule.pricing_source, rule.pricing_effective_date)




def _cost_evidence_values_from_accum(
    input_tokens: int,
    uncached_input_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    *,
    model: str | None,
    provider: str | None,
    pricing_input_tokens: int | None = None,
    pricing_rule: PriceRule | None | object = _PRICING_RULE_UNSET,
) -> tuple[float, str, str | None] | None:
    """Return primitive cost evidence for allocation hot paths.

    The tuple preserves every request-tier and rounding decision made by
    ``_estimate_cost_from_ints`` while letting callers aggregate millions of
    slices without constructing one Pydantic model per slice.
    """
    if not (
        input_tokens
        or uncached_input_tokens
        or cached_input_tokens
        or cache_creation_input_tokens
        or output_tokens
        or reasoning_output_tokens
    ):
        return None
    estimate = _estimate_cost_from_ints(
        input_tokens,
        cached_input_tokens,
        cache_creation_input_tokens,
        output_tokens,
        reasoning_output_tokens,
        model=model,
        provider=provider,
        pricing_input_tokens=pricing_input_tokens,
        pricing_rule=pricing_rule,
    )
    if estimate is None:
        return None
    amount_usd, pricing_source, pricing_effective_date = estimate
    return (amount_usd, pricing_source, pricing_effective_date or None)


def cost_evidence_from_usage(
    usage: dict[str, Any] | None,
    *,
    model: str | None,
    provider: str | None,
    pricing_input_tokens: int | None = None,
) -> CostEvidenceFlat | None:
    """USD cost over a usage bucket, sourced from the pricing SoT.

    Prefers a vendor-reported cost (``usage["cost_usd"]`` — e.g. Pi's
    ``cost.total`` from its jsonl logs) over the pricing catalog's estimate,
    so sessions whose logs carry real cost are billed at that cost rather
    than an estimate. Returns ``None`` when neither is available (unknown
    model + no reported cost), so callers omit cost rather than report a
    misleading 0. Shared by ``analysis`` and ``composition``.
    """
    if not usage:
        return None
    reported = usage.get("cost_usd")
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        return CostEvidenceFlat(
            value_usd=round(float(reported), 8),
            confidence="reported",
            source="session log",
        )
    if not (
        usage.get("prompt_tokens", 0)
        or usage.get("uncached_prompt_tokens", 0)
        or usage.get("cached_prompt_tokens", 0)
        or usage.get("cache_write_tokens", 0)
        or usage.get("completion_tokens", 0)
        or usage.get("reasoning_tokens", 0)
    ):
        return None
    estimate = _estimate_cost_from_ints(
        _usage_int(usage, "prompt_tokens", "input"),
        _usage_int(usage, "cached_prompt_tokens", "cached"),
        _usage_int(usage, "cache_write_tokens", "cache_creation"),
        _usage_int(usage, "completion_tokens", "output"),
        _usage_int(usage, "reasoning_tokens", "reasoning"),
        model=model,
        provider=provider,
        pricing_input_tokens=pricing_input_tokens,
    )
    if estimate is None:
        return None
    amount_usd, pricing_source, pricing_effective_date = estimate
    return CostEvidenceFlat(
        value_usd=amount_usd,
        confidence="estimated",
        source=pricing_source,
        effective_date=pricing_effective_date or None,
    )


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


def cache_break_waste_usd(
    tokens: int,
    *,
    model: str | None,
    provider: str | None = None,
    now: datetime | None = None,
) -> float | None:
    """Extra cost paid because ``tokens`` were re-processed uncached instead of
    served from the prompt cache — ``(uncached_rate - cached_rate) × tokens``.

    The waste, not the gross re-read cost: tokens that *would* have been cheap
    (cached) are billed at the full input rate because the cache missed. Returns
    ``None`` when the model lacks a separate cached rate (cache not priced
    apart, e.g. some Pro tiers) so no break cost is attributed.
    """
    if tokens <= 0:
        return None
    normalized_model = _normalize_model_name(model)
    if normalized_model is None:
        return None
    rule = _resolve_price_rule(normalized_model, provider=provider, now=now)
    if rule is None or rule.cached_input_per_mtok is None:
        return None
    delta_rate = rule.input_per_mtok - rule.cached_input_per_mtok
    return _round_usd((tokens / TOKENS_PER_MILLION) * delta_rate)


def get_model_context_window(
    model: str | None,
    *,
    provider: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """Resolve a model's context window (tokens).

    Tiers: Claude Code alias suffix (``glm-5.2[1m]``), then the curated static
    map (offline-safe), then the live models.dev catalog (24h disk cache,
    skipped when ``CT_DISABLE_LIVE_PRICING`` is set). Unknown models return
    ``None``.
    """
    if not model:
        return None
    alias_window = _parse_alias_window(model)
    if alias_window is not None:
        return alias_window
    normalized_model = _normalize_model_name(model)
    if normalized_model is None:
        return None
    static_window = _MODEL_CONTEXT_WINDOWS.get(normalized_model)
    if static_window is not None:
        return static_window
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


def _threshold_rate(
    above_threshold: bool,
    default: float | None,
    threshold: float | None,
) -> float | None:
    return threshold if above_threshold and threshold is not None else default


def _uses_net_input_convention(provider: str | None, model: str | None) -> bool:
    """Anthropic reports cached/cache-creation tokens *on top of* the input
    total; OpenAI/Codex report input net of cache. Drives whether the input
    bucket is split before pricing. Single copy — ``analysis.py`` reuses it.
    """
    if provider:
        return provider.strip().lower() in {"anthropic", "claude", "claude-code"}
    return "claude" in (model or "").lower()


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
        MODELS_DEV_SOURCE, headers={"User-Agent": "coding-trajectory/1.0"}
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
    return not any(
        os.environ.get(name) in {"1", "true", "TRUE"}
        for name in (_DISABLE_LIVE_ENV, _LEGACY_DISABLE_LIVE_ENV)
    )


def _models_dev_cache_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_root) if cache_root else Path.home() / ".cache"
    return (
        base
        / "coding-trajectory"
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
        input_per_mtok_above_threshold=(
            threshold_cost.input if threshold_cost else None
        ),
        output_per_mtok_above_threshold=(
            threshold_cost.output if threshold_cost else None
        ),
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


def _resolve_price_rule(
    model: str | None,
    *,
    provider: str | None,
    now: datetime | None = None,
    cache: dict[tuple[str | None, str], PriceRule | None] | None = None,
) -> PriceRule | None:
    """Resolve one model price, optionally reusing a caller-owned cache.

    The live catalog itself is already cached for 24 hours. This smaller cache
    avoids repeating the locked catalog access and provider/model dictionary
    lookup for each item allocated from one provider request. A caller should
    scope it to one projection so all evidence in that projection uses one
    pricing snapshot.
    """
    normalized_model = _normalize_model_name(model)
    if normalized_model is None:
        return None
    key = (_normalize_provider(provider), normalized_model)
    if cache is not None:
        cached = cache.get(key, _PRICING_RULE_UNSET)
        if cached is not _PRICING_RULE_UNSET:
            return cached

    rules = _load_live_price_rules(now=now or datetime.now(UTC))
    rule = _lookup_price_rule(rules, provider=provider, model=normalized_model)
    if rule is None:
        rule = _lookup_price_rule(
            _openai_standard_price_rules(),
            provider=provider,
            model=normalized_model,
        )
    if cache is not None:
        cache[key] = rule
    return rule


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


def _parse_alias_window(model: str) -> int | None:
    match = _ALIAS_WINDOW_RE.search(model.strip())
    if not match:
        return None
    size = float(match.group("size"))
    unit = match.group("unit").lower()
    return int(size * (1_000 if unit == "k" else 1_000_000))


@lru_cache(maxsize=256)
def _normalize_model_name(model: str | None) -> str | None:
    """Canonical model id: strip provider prefixes, alias suffix, variant and
    date suffixes, and region/deployment markers. Single copy reused by both
    pricing-rule lookup and context-window resolution.
    """
    if not model:
        return None
    name = model.strip().lower()
    for prefix in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    if "claude-" in name:
        name = name[name.find("claude-") :]
    name = _ALIAS_WINDOW_RE.sub("", name)
    name = name.split("@", 1)[0]
    name = name.split(":", 1)[0]
    name = _VARIANT_SUFFIX_RE.sub("", name)
    name = _DATE_SUFFIX_RE.sub("", name)
    name = name.strip("-")
    return name or None


__all__ = [
    "MODELS_DEV_SOURCE",
    "CostEvidenceFlat",
    "PriceRule",
    "cache_break_waste_usd",
    "cost_evidence_from_usage",
    "get_model_context_window",
]
