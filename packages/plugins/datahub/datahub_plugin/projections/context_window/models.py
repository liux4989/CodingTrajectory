"""Context Window models and pure category ordering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CategoryKey = Literal[
    "starting_context",
    "user_input",
    "files",
    "output",
    "agent",
    "unattributed",
]
Confidence = Literal[
    "exact_usage", "exact_text", "estimated_tokens", "structural", "unknown"
]


@dataclass(frozen=True)
class _VisibleTextSize:
    tokens: int


def _visible_text_size(text: str) -> _VisibleTextSize:
    return _VisibleTextSize(tokens=max(1, (len(text) + 3) // 4) if text else 0)


CategoryKey = Literal[
    "starting_context",
    "user_input",
    "files",
    "output",
    "agent",
    "unattributed",
]
Confidence = Literal[
    "exact_usage", "exact_text", "estimated_tokens", "structural", "unknown"
]


class TokenEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int = Field(ge=0)
    confidence: Confidence
    source: str


class ContextCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: CategoryKey
    source_key: str
    label: str
    tokens: TokenEvidence
    percent: float | None = None
    estimated_cost: CostEvidence | None = None


class ContextEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: Literal["before_first_prompt", "turn", "post_turn"]
    turn_id: str | None = None
    category: CategoryKey
    label: str
    summary: str | None = None
    tokens: TokenEvidence | None = None
    source: str
    confidence: Confidence
    detail_ref: dict[str, str] = Field(default_factory=dict)
    terminal_visible: bool = True
    estimated_cost: CostEvidence | None = None
    # Wall-clock gap (``runtime.wait_before_seconds``) preceding this turn; the
    # prompt-cache TTL break is read off it together with ``re_read_tokens``.
    idle_seconds: float | None = None
    re_read_tokens: int | None = None


class ExpensiveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    turn_id: str
    category: CategoryKey
    label: str
    summary: str
    allocated_usage: dict[str, int]
    estimated_cost: CostEvidence


class CompactionEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    # Provider-native mechanism (``eviction_boundary`` for Claude Code,
    # ``context_compacted`` for Codex); controls which delta fields render.
    mechanism: str
    trigger: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    dropped_tokens: int | None = None


class CompactionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    cumulative_dropped_tokens: int | None = None
    events: list[CompactionEventRecord] = Field(default_factory=list)


class CacheBreakRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    # ttl_confirmed: idle gap exceeds the vendor's prompt-cache TTL max
    #   (OpenAI >=600s, Anthropic >=300s) — cache evicted by age.
    # ttl_likely: idle in the ambiguous band (OpenAI 300–600s); could be TTL
    #   but not certain.
    # effort_switch: an observed effort change aligns with an observed drop in
    #   cache-hit tokens across the turn boundary. It overrides TTL when both
    #   align with the same measured cache loss.
    # model_switch: the dominant (provider, model) changed across the turn
    #   boundary, so the prefix was re-processed under a new cache key.
    # unattributed: a measured cache-hit loss (boundary or intra-turn) with no
    #   aligned effort change, no model switch, and no TTL-sized idle gap.
    #   Surfaced instead of dropped so the miss is visible - the cause (e.g. a
    #   mid-turn cache invalidation, a cold start, a backend that doesn't couple
    #   cache to effort like glm-5.2, tool reorder/removal, nondeterministic
    #   enumeration, system-prompt churn, or a proxy dropping session affinity)
    #   is simply unknown.
    type: Literal[
        "ttl_confirmed", "ttl_likely", "effort_switch", "model_switch", "unattributed"
    ]
    idle_seconds: float
    re_read_tokens: int
    cached_after_tokens: int | None = None
    est_cost_usd: float | None = None
    # Populated only for a confirmed effort_switch — the resolved effort levels
    # from the aligned ``effort_changed`` observation. ``effort_from`` is ``None``
    # on Claude Code's first ``/effort`` switch (baseline unknown); always set
    # for Codex (per-turn effort, first turn establishes the baseline).
    effort_from: str | None = None
    effort_to: str | None = None
    # Populated only for a model_switch — the dominant model identities that
    # bracket the cache-key change. Either may be ``None`` on the first turn
    # after a reset where the prior context is unknown.
    model_from: str | None = None
    model_to: str | None = None


class CacheBreakSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    # Effort-independent static prefix that survives cache misses; the value
    # cached prefixes collapse toward on a break. ``None`` only when no turn
    # in the session reported a cached footprint (no cache accounting at all).
    floor_tokens: int | None = None
    total_re_read_tokens: int = 0
    estimated_waste_usd: float | None = None
    by_type: dict[str, int] = Field(default_factory=dict)
    events: list[CacheBreakRecord] = Field(default_factory=list)


class ContextSessionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    role: str
    label: str
    relationship: str | None = None
    parent_session_id: str | None = None
    used_tokens: TokenEvidence | None = None
    used_percent: float | None = None
    token_cost: CostEvidence | None = None


class CostEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_usd: float = Field(ge=0)
    confidence: Literal["reported", "estimated"]
    source: str
    effective_date: str | None = None


class ContextWindowProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    active_session_id: str
    vendor: str
    model: str | None = None
    context_window_tokens: TokenEvidence | None = None
    used_tokens: TokenEvidence | None = None
    used_percent: float | None = None
    token_cost: CostEvidence | None = None
    categories: list[ContextCategory]
    provider_usage_buckets: list[ContextCategory]
    session_sections: list[ContextSessionSection] = Field(default_factory=list)
    expensive_items: list[ExpensiveItem] = Field(default_factory=list)
    events: list[ContextEvent]
    compaction: CompactionSummary | None = None
    cache_breaks: CacheBreakSummary | None = None
    warnings: list[str]


def _category_sort_key(category: ContextCategory) -> tuple[float, int]:
    return (
        category.estimated_cost.value_usd if category.estimated_cost else 0.0,
        category.tokens.value,
    )
