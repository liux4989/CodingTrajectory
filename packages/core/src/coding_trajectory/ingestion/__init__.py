from coding_trajectory.ingestion.adapters.amp import AmpAdapter
from coding_trajectory.ingestion.adapters.base import BaseAdapter
from coding_trajectory.ingestion.adapters.claude_code import ClaudeCodeAdapter
from coding_trajectory.ingestion.adapters.codex import CodexAdapter
from coding_trajectory.ingestion.adapters.pi import PiAdapter
from coding_trajectory.ingestion.graph import (
    assemble_project_session_graphs,
    build_edges,
    build_session_graph,
    build_session_graph_summary,
    decorate_sessions,
)
from coding_trajectory.ingestion.models import (
    AgentMessageItem,
    AmpExtensions,
    ClaudeCodeExtensions,
    CodexExtensions,
    CommandExecutionItem,
    ContextCategoryObservation,
    ContextSourceObservation,
    ContextUsageObservation,
    Event,
    EventType,
    FileChangeItem,
    Item,
    ItemBase,
    PiExtensions,
    PlanItem,
    ReasoningItem,
    RuntimeObservation,
    Session,
    SessionEdge,
    SessionGraph,
    SessionGraphSummary,
    SessionStatus,
    ToolCallItem,
    ToolStatus,
    Turn,
    TurnStatus,
    Vendor,
    VendorExtensions,
)
from coding_trajectory.ingestion.retention import CanonicalRetention

# ``incremental`` depends on discovery, and discovery in turn imports the
# canonical query store.  Loading it eagerly here makes a direct import of
# ``query`` (and therefore the CLI) circular.  Preserve the package-level
# compatibility surface while paying for incremental orchestration only when a
# caller actually asks for one of those names.
_INCREMENTAL_EXPORTS = {
    "GraphBuildIssue",
    "IncrementalGraphBuild",
    "MessagesForPath",
    "SourceGraphRelationship",
    "SourceGraphComponent",
    "SourceGraphComponentPlan",
    "SourceMessage",
    "SourceSnapshot",
    "SourceStatus",
    "rebuild_affected_session_graphs",
    "rebuild_affected_session_graphs_from_files",
    "plan_session_graph_components_from_files",
}


def __getattr__(name: str) -> object:
    if name not in _INCREMENTAL_EXPORTS:
        raise AttributeError(name)
    from coding_trajectory.ingestion import incremental

    value = getattr(incremental, name)
    globals()[name] = value
    return value


__all__ = [
    # models
    "AgentMessageItem",
    "AmpExtensions",
    "ClaudeCodeExtensions",
    "CanonicalRetention",
    "CodexExtensions",
    "CommandExecutionItem",
    "ContextCategoryObservation",
    "ContextSourceObservation",
    "ContextUsageObservation",
    "FileChangeItem",
    "GraphBuildIssue",
    "IncrementalGraphBuild",
    "Item",
    "ItemBase",
    "PiExtensions",
    "PlanItem",
    "ReasoningItem",
    "MessagesForPath",
    "Event",
    "EventType",
    "Session",
    "SessionStatus",
    "SourceGraphRelationship",
    "SourceGraphComponent",
    "SourceGraphComponentPlan",
    "SourceMessage",
    "SourceSnapshot",
    "SourceStatus",
    "RuntimeObservation",
    "SessionGraph",
    "SessionEdge",
    "SessionGraphSummary",
    "ToolCallItem",
    "Turn",
    "TurnStatus",
    "ToolStatus",
    "Vendor",
    "VendorExtensions",
    # graph
    "assemble_project_session_graphs",
    "build_edges",
    "build_session_graph",
    "build_session_graph_summary",
    "decorate_sessions",
    "rebuild_affected_session_graphs",
    "rebuild_affected_session_graphs_from_files",
    "plan_session_graph_components_from_files",
    # adapters
    "BaseAdapter",
    "AmpAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "PiAdapter",
]
