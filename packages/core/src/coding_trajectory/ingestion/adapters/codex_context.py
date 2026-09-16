"""Codex usage/context evidence: token counts, turn context, prompt blocks.

Owns the handlers that read Codex usage and context evidence off rollout
records and into the parse state (``codex_state.CodexParseState``): cumulative
``token_count`` snapshots with stale-re-emission filtering, ``turn_context``
snapshots with reasoning-effort/runtime-config change-points, and the
resident prompt-block (context source) classification whose first-emission
timestamps drive per-call cost attribution. ``codex.py`` dispatches these
records here; nothing in this module imports the adapter or reconstruction
modules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from coding_trajectory.ingestion.adapters.codex_state import (
    CodexParseState,
    _as_non_empty_str,
)
from coding_trajectory.ingestion.models import (
    ContextSourceObservation,
    RuntimeObservation,
    Vendor,
)
from coding_trajectory.ingestion.transcript import TranscriptRecord
from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    context_usage_observation,
    normalize_codex_token_count,
)

# Moved signatures keep their original ``_ParseState`` spelling.
_ParseState = CodexParseState

_CACHE_RELEVANT_TURN_CONTEXT_FIELDS = (
    "cwd",
    "workspace_roots",
    "current_date",
    "timezone",
    "approval_policy",
    "approvals_reviewer",
    "sandbox_policy",
    "permission_profile",
    "active_permission_profile",
    "file_system_sandbox_policy",
    "personality",
    "collaboration_mode",
    "multi_agent_version",
    "multi_agent_mode",
    "realtime_active",
)


def _codex_prompt_block_name(text: str, index: int) -> str:
    stripped = text.lstrip()
    if stripped.startswith("<") and ">" in stripped:
        tag = stripped[1 : stripped.index(">")].strip().split()[0]
        if tag:
            return tag
    return f"developer_block_{index}"


def _is_codex_agents_md_prompt(text: str) -> bool:
    return text.lstrip().startswith("# AGENTS.md instructions")


def _codex_user_prompt_block_name(text: str) -> str | None:
    if _is_codex_agents_md_prompt(text):
        return "agents_md"
    return None


_CONTEXT_SOURCE_LABELS = {
    "base_system": "Base instructions",
    "developer_instructions": "Developer instructions",
    "agents_md": "AGENTS.md",
    "skills": "Skills",
    "mcp": "Tools / MCP",
    "memory": "Memory",
}


def _codex_context_source_key(*, block: str, role: str, text: str) -> str:
    haystack = f"{block}\n{text}".lower()
    if block == "base_instructions":
        return "base_system"
    if _is_codex_agents_md_prompt(text):
        return "agents_md"
    if "skills_instructions" in block or "### available skills" in haystack:
        return "skills"
    if "plugins_instructions" in block or "### available plugins" in haystack:
        return "mcp"
    if (
        "memory_summary" in haystack
        or "memory layout" in haystack
        or "## memory" in haystack
    ):
        return "memory"
    if "mcp" in haystack or "tools are grouped" in haystack:
        return "mcp"
    if role == "developer":
        return "developer_instructions"
    return "base_system"


def _context_source_observation(
    *,
    timestamp: Any,
    block: str,
    role: str,
    text: str,
) -> ContextSourceObservation:
    key = _codex_context_source_key(block=block, role=role, text=text)
    return ContextSourceObservation(
        timestamp=timestamp,
        key=key,
        label=_CONTEXT_SOURCE_LABELS[key],
        text=text,
        source="codex_prompt_block",
    )


def _record_context_source(
    state: Any,
    observation: ContextSourceObservation,
    *,
    block: str,
    role: str,
) -> None:
    """Keep one observation per (role, block_name); first emission wins.

    Codex re-injects the base/developer/AGENTS.md prompt blocks after a context
    compaction. Each re-injection shares the same (role, block_name) identity as
    the resident prefix block, so per-block dedup collapses them. The first
    emission is kept: the block is resident from first injection through end of
    session (Codex re-attaches it after every compaction), so the earliest
    timestamp is what makes the accounting attribute its per-call cost across
    every API call that carried the block.
    """
    state.context_source_by_block.setdefault((role, block), observation)


def _runtime_config_hashes(payload: dict[str, Any]) -> dict[str, str]:
    """Hash named turn-context fields without retaining their raw values."""
    hashes: dict[str, str] = {}
    for key in _CACHE_RELEVANT_TURN_CONTEXT_FIELDS:
        if key not in payload:
            continue
        canonical = json.dumps(
            payload[key],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        hashes[key] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashes


def handle_token_count(
    payload: dict,
    ts: datetime,
    state: _ParseState,
    transcript: list[TranscriptRecord],
    *,
    turn_id: Any,
) -> None:
    """Project one cumulative ``token_count`` snapshot into usage evidence."""
    info = payload.get("info")
    # Codex occasionally re-emits a token_count snapshot whose
    # cumulative ``total_token_usage`` is byte-identical to the
    # prior event's (a stale re-emission, not a new model call);
    # its ``last_token_usage`` repeats too, so counting it would
    # double-charge the call. Drop it before any accounting.
    total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
    if isinstance(total_usage, dict) and total_usage == state.prev_total_token_usage:
        return
    if isinstance(total_usage, dict):
        state.prev_total_token_usage = total_usage
    normalized_metrics = normalize_codex_token_count(
        model=state.turn_context.get("model"),
        info=info,
    )
    usage_record = TranscriptRecord(
        sequence=len(transcript),
        timestamp=ts,
        vendor=Vendor.CODEX_CLI,
        role="runtime",
        kind="usage",
        data={
            "turn_id_raw": turn_id,
            "raw_type": "token_count",
            **normalized_metrics,
            "vendor_data": {
                "metrics": normalized_metrics.get("metrics"),
            }
            if normalized_metrics.get("metrics")
            else {},
        },
        fidelity="synthetic",
    )
    observation = context_usage_observation(
        timestamp=ts,
        source="codex_token_count",
        normalized=normalized_metrics,
        source_event_id=usage_record.record_id,
        provider="openai",
    )
    if observation is not None:
        if observation.context_window_tokens is None:
            observation.context_window_tokens = state.context_window_tokens
        state.context_usage.append(observation)
    transcript.append(usage_record)


def handle_turn_context(
    payload: dict,
    ts: datetime | None,
    state: _ParseState,
) -> None:
    """Record turn_context and detect reasoning-effort change-points.

    Codex emits a fresh turn_context per turn carrying the active
    ``effort``; a value differing from the prior turn's marks a cache-key
    change (the warm prefix is served from a different effort-bucket cache).
    """
    state.turn_context = payload
    if ts is not None:
        runtime_config_hashes = _runtime_config_hashes(payload)
        changed_runtime_config_hashes = (
            runtime_config_hashes
            if state.prev_runtime_config_hashes is None
            or runtime_config_hashes != state.prev_runtime_config_hashes
            else None
        )
        state.runtime_observations.append(
            RuntimeObservation(
                timestamp=ts,
                kind="turn_context_snapshot",
                turn_id_raw=_as_non_empty_str(payload.get("turn_id")),
                comp_hash=_as_non_empty_str(payload.get("comp_hash")),
                runtime_config_hashes=changed_runtime_config_hashes,
            )
        )
        state.prev_runtime_config_hashes = runtime_config_hashes
    multi_agent_version = _as_non_empty_str(payload.get("multi_agent_version"))
    if multi_agent_version is not None:
        state.multi_agent_version = multi_agent_version
    multi_agent_mode = _as_non_empty_str(payload.get("multi_agent_mode"))
    if multi_agent_mode is not None:
        state.multi_agent_mode = multi_agent_mode
    effort = _as_non_empty_str(payload.get("effort"))
    if (
        effort is not None
        and state.prev_effort is not None
        and effort != state.prev_effort
        and ts is not None
    ):
        state.runtime_observations.append(
            RuntimeObservation(
                timestamp=ts,
                kind="effort_changed",
                turn_id_raw=_as_non_empty_str(payload.get("turn_id")),
                effort_from=state.prev_effort,
                effort_to=effort,
            )
        )
    if effort is not None:
        state.prev_effort = effort
