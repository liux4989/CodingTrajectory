"""Provider-aware token counting for visible content.

Replaces the rough ``char/4`` heuristic (``len(text) // 4``) used throughout
composition and cost attribution with the real provider tokenizer where
available, falling back to ``char/4`` when it is not.

Tokenization is provider-specific (OpenAI's BPE differs from Anthropic's,
and from routed models like GLM/Kimi/MiniMax). ``char/4`` undercounts dense
content (code, JSON) and overcounts sparse prose, so it drifts from the
provider-reported ``used_input_tokens`` as content accumulates.

Resolution:
  * OpenAI ``gpt-5*`` / ``gpt-4o*`` / ``o1`` / ``o3`` / ``o4`` → ``o200k_base``
    (the encoding those families use).
  * Any other model known to ``tiktoken.encoding_name_for_model`` → that
    encoding (e.g. ``gpt-4`` → ``cl100k_base``).
  * Claude / GLM / Kimi / MiniMax / unknown → ``cl100k_base`` as a proxy
    (Anthropic publishes no tokenizer; these routed models publish none
    either). ``cl100k_base`` is the closest broadly-available BPE.

Offline safety: ``tiktoken.get_encoding`` downloads the BPE rank file on first
use (a ~1.6 MB blob cached under ``~/.cache/tiktoken`` or ``$TIKTOKEN_CACHE_DIR``).
If the download fails (air-gapped, first-ever run with no network) the loader
records a one-time debug warning and the counter falls back to ``char/4`` for
every count, so ingestion never crashes.

The active counter is thread/async-safe via a ``contextvars`` token. Public
metrics builders scope a per-session counter (resolved from the session's
dominant model) around their work; leaf sizing functions read it implicitly.
Callers outside a scope get a process-wide default (``cl100k_base`` proxy).
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import threading
from typing import Any, Callable, TypeVar

from coding_trajectory import debug

_F = TypeVar("_F", bound=Callable[..., Any])

# OpenAI model families that ship with the newer o200k_base BPE.
_O200K_PREFIXES: tuple[str, ...] = (
    "gpt-5",
    "gpt-4o",
    "o1",
    "o3",
    "o4",
    "o200k",
)


class TokenCounter:
    """Count text tokens via a real tokenizer, with a ``char/4`` fallback."""

    __slots__ = ("_encoding", "_name")

    def __init__(self, encoding: Any | None, *, name: str) -> None:
        self._encoding = encoding
        self._name = name

    def count(self, text: str) -> int:
        if not text:
            return 0
        encoding = self._encoding
        if encoding is not None:
            try:
                # encode_ordinary treats the text as plain content (no special
                # tokens), which is what sizing visible tool output wants.
                return len(encoding.encode_ordinary(text))
            except Exception:
                pass
        return max(1, (len(text) + 3) // 4)

    @property
    def name(self) -> str:
        return self._name

    @property
    def uses_real_tokenizer(self) -> bool:
        return self._encoding is not None


_MISSING: object = object()
_CACHE: dict[str, Any | None] = {}
_CACHE_LOCK = threading.Lock()
_WARNED: set[str] = set()


def _load_encoding(name: str) -> Any | None:
    """Load (and cache) a tiktoken encoding by name, offline-safe."""
    cached = _CACHE.get(name, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]
    with _CACHE_LOCK:
        if name in _CACHE:
            return _CACHE[name]
        encoding: Any | None = None
        try:
            import tiktoken

            encoding = tiktoken.get_encoding(name)
        except Exception:
            if name not in _WARNED:
                _WARNED.add(name)
                debug.warn(
                    "tiktoken encoding could not be loaded; visible-content "
                    "sizing falls back to the char/4 estimate. Run once online "
                    "or set TIKTOKEN_CACHE_DIR to a pre-populated cache to "
                    "enable real tokenization.",
                    code="token_counter.tiktoken_unavailable",
                    severity="warning",
                    encoding=name,
                )
        _CACHE[name] = encoding
        return encoding


# Providers serving the Anthropic-compatible API. Claude Code always emits
# Anthropic-schema usage (input_tokens is uncached) regardless of the routed
# model, so provider is stamped "anthropic" for Claude-native *and* routed
# (glm/kimi/minimax) sessions.
_ANTHROPIC_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "claude", "claude_code", "claude-code"}
)


def _encoding_name_for(model: str | None, provider: str | None = None) -> str:
    # Anthropic publishes no tokenizer. Every model served via the
    # Anthropic-compatible API — Claude-native and routed glm/kimi/minimax
    # alike — is tokenized with the cl100k_base proxy uniformly, keyed on the
    # API provider rather than the underlying (often unknown) model tokenizer.
    provider_lower = (provider or "").strip().lower()
    if provider_lower in _ANTHROPIC_PROVIDERS:
        return "cl100k_base"
    model_name = (model or "").strip()
    if not model_name:
        return "cl100k_base"
    lowered = model_name.lower()
    if any(lowered.startswith(prefix) for prefix in _O200K_PREFIXES):
        return "o200k_base"
    try:
        import tiktoken

        return tiktoken.encoding_name_for_model(lowered)
    except Exception:
        return "cl100k_base"


def token_counter_for(
    model: str | None = None, *, provider: str | None = None
) -> TokenCounter:
    """Build a :class:`TokenCounter` for a model/provider pair."""
    name = _encoding_name_for(model, provider)
    encoding = _load_encoding(name)
    return TokenCounter(encoding, name=name if encoding is not None else "char/4")


def counter_for_session_graph(session_graph: Any) -> TokenCounter:
    """Resolve the dominant-model counter for a session graph.

    Uses the latest context-usage observation's model/provider, since that is
    the model that produced the resident context being sized.
    """
    latest: Any | None = None
    for session in getattr(session_graph, "sessions", ()) or ():
        for observation in getattr(session, "context_usage", ()) or ():
            if latest is None or observation.timestamp > latest.timestamp:
                latest = observation
    if latest is not None:
        return token_counter_for(latest.model, provider=latest.provider)
    return default_counter()


_current: contextvars.ContextVar[TokenCounter | None] = contextvars.ContextVar(
    "coding_trajectory_token_counter", default=None
)

_default_counter: TokenCounter | None = None
_default_lock = threading.Lock()


def default_counter() -> TokenCounter:
    """Process-wide default counter (``cl100k_base`` proxy, char/4 fallback)."""
    global _default_counter
    if _default_counter is None:
        with _default_lock:
            if _default_counter is None:
                _default_counter = token_counter_for(None)
    return _default_counter


def get_current_counter() -> TokenCounter:
    """Return the active scoped counter, or the process default."""
    counter = _current.get(None)
    return counter if counter is not None else default_counter()


@contextlib.contextmanager
def scoped_counter(counter: TokenCounter):
    """Set the active counter for the duration of the block."""
    token = _current.set(counter)
    try:
        yield counter
    finally:
        _current.reset(token)


def session_scoped(fn: _F) -> _F:
    """Decorator: scope the session-graph's dominant-model counter around ``fn``.

    For builders whose first positional argument is a ``SessionGraph``. The
    counter is resolved once per call and applies to every ``visible_text_size``
    / ``item_*_size`` leaf invoked within, so composition and cost attribution
    share one tokenizer. Outside such a builder, leaf sizing falls back to the
    process-wide default counter.
    """

    @functools.wraps(fn)
    def wrapper(session_graph: Any, *args: Any, **kwargs: Any) -> Any:
        with scoped_counter(counter_for_session_graph(session_graph)):
            return fn(session_graph, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


__all__ = [
    "TokenCounter",
    "counter_for_session_graph",
    "default_counter",
    "get_current_counter",
    "scoped_counter",
    "session_scoped",
    "token_counter_for",
]
