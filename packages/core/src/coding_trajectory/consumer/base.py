"""Base SessionEnricher and public enrich_session() factory."""

from __future__ import annotations

from abc import ABC, abstractmethod

from coding_trajectory.ingestion.models import Session, Vendor


class SessionEnricher(ABC):
    """Knows one vendor's payload conventions; populates typed fields on Events."""

    @abstractmethod
    def enrich(self, session: Session) -> None:
        """Mutate *session* in place — set category, event_group_id, parent_event_id,
        tool_call, llm, text on each Event."""


def enrich_session(session: Session) -> None:
    """Dispatch to the correct per-vendor enricher and enrich in place."""
    from coding_trajectory.consumer.amp import AmpSessionEnricher
    from coding_trajectory.consumer.claude_code import ClaudeCodeSessionEnricher
    from coding_trajectory.consumer.codex import CodexSessionEnricher

    enrichers: dict[Vendor, type[SessionEnricher]] = {
        Vendor.AMP: AmpSessionEnricher,
        Vendor.CLAUDE_CODE: ClaudeCodeSessionEnricher,
        Vendor.CODEX_CLI: CodexSessionEnricher,
    }
    cls = enrichers.get(session.vendor)
    if cls is not None:
        cls().enrich(session)
