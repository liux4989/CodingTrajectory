"""Static model context-window catalog (no runtime network).

Used as a fallback when a vendor log does not report a model's context window.
Claude Code usage blocks omit the window (unlike Codex ``token_count`` events
which carry ``model_context_window``), so without this fallback every Claude
Code session reports ``0.0%`` of context. Values are curated from
https://models.dev and baked in; unknown models resolve to ``None`` (preserving
prior behavior) rather than a guessed value.
"""

from __future__ import annotations

import re
from typing import Final

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


def _parse_alias_window(model: str) -> int | None:
    match = _ALIAS_WINDOW_RE.search(model.strip())
    if not match:
        return None
    size = float(match.group("size"))
    unit = match.group("unit").lower()
    return int(size * (1_000 if unit == "k" else 1_000_000))


def _normalize_model_name(model: str | None) -> str | None:
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


def get_model_context_window(
    model: str | None,
    *,
    provider: str | None = None,
) -> int | None:
    """Resolve a model's context window (tokens) without network access.

    Returns the Claude Code alias-suffix window (e.g. ``glm-5.2[1m]``) when
    present, else a curated static value keyed by the normalized model id.
    ``provider`` is accepted for signature compatibility but the static map is
    keyed by model id only. Unknown models return ``None``.
    """
    if not model:
        return None
    alias_window = _parse_alias_window(model)
    if alias_window is not None:
        return alias_window
    normalized = _normalize_model_name(model)
    if not normalized:
        return None
    return _MODEL_CONTEXT_WINDOWS.get(normalized)


__all__ = ["get_model_context_window"]
