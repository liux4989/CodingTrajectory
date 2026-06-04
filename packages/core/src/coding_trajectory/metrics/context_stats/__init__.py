"""Per-vendor session context stats (used by `ct session stats`)."""

from typing import Any

from coding_trajectory.ingestion.models import SessionGraph, Vendor
from coding_trajectory.metrics.context_stats.claude_code import build_claude_code_context_stats
from coding_trajectory.metrics.context_stats.codex import build_codex_context_stats
from coding_trajectory.metrics.context_stats.pi import build_pi_context_stats


def build_session_graph_context_stats(session_graph: SessionGraph) -> dict[str, Any]:
    vendors = {session.vendor for session in session_graph.sessions if session.vendor}
    if not vendors:
        raise ValueError("session_graph has no vendor sessions")
    if len(vendors) > 1:
        names = ", ".join(sorted(vendor.value for vendor in vendors))
        raise NotImplementedError(
            f"session stats does not yet support multi-vendor session graphs; got: {names}"
        )

    vendor = next(iter(vendors))
    if vendor == Vendor.CODEX_CLI:
        return build_codex_context_stats(session_graph)
    if vendor == Vendor.CLAUDE_CODE:
        return build_claude_code_context_stats(session_graph)
    if vendor == Vendor.PI:
        return build_pi_context_stats(session_graph)

    raise NotImplementedError(f"session stats not implemented for vendor: {vendor.value}")


__all__ = [
    "build_session_graph_context_stats",
    "build_claude_code_context_stats",
    "build_codex_context_stats",
    "build_pi_context_stats",
]
