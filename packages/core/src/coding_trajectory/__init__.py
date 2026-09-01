"""Public API for the coding_trajectory core package.

Submodules remain the canonical import paths; this module lazily re-exports
the supported public surface so importing the root package does not pay the
import cost of the service stack up front.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

_LAZY_EXPORTS: dict[str, str] = {
    # ingestion: canonical models
    "AgentMessageItem": "coding_trajectory.ingestion",
    "ClaudeCodeExtensions": "coding_trajectory.ingestion",
    "CodexExtensions": "coding_trajectory.ingestion",
    "CommandExecutionItem": "coding_trajectory.ingestion",
    "Event": "coding_trajectory.ingestion",
    "EventType": "coding_trajectory.ingestion",
    "FileChangeItem": "coding_trajectory.ingestion",
    "Item": "coding_trajectory.ingestion",
    "ItemBase": "coding_trajectory.ingestion",
    "PiExtensions": "coding_trajectory.ingestion",
    "PlanItem": "coding_trajectory.ingestion",
    "ReasoningItem": "coding_trajectory.ingestion",
    "Session": "coding_trajectory.ingestion",
    "SessionStatus": "coding_trajectory.ingestion",
    "SessionGraph": "coding_trajectory.ingestion",
    "SessionEdge": "coding_trajectory.ingestion",
    "SessionGraphSummary": "coding_trajectory.ingestion",
    "ToolCallItem": "coding_trajectory.ingestion",
    "Turn": "coding_trajectory.ingestion",
    "TurnStatus": "coding_trajectory.ingestion",
    "ToolStatus": "coding_trajectory.ingestion",
    "Vendor": "coding_trajectory.ingestion",
    "VendorExtensions": "coding_trajectory.ingestion",
    # ingestion: adapters
    "BaseAdapter": "coding_trajectory.ingestion",
    "ClaudeCodeAdapter": "coding_trajectory.ingestion",
    "CodexAdapter": "coding_trajectory.ingestion",
    "PiAdapter": "coding_trajectory.ingestion",
    # discovery
    "stabilize_session": "coding_trajectory.discovery",
    # query
    "DocumentStore": "coding_trajectory.query",
    "DocumentError": "coding_trajectory.query",
    "ResourceNotFoundError": "coding_trajectory.query",
    # contracts
    "SERVICE_CONTRACTS": "coding_trajectory.contracts",
    "service_contract": "coding_trajectory.contracts",
    "command_schema": "coding_trajectory.contracts",
    # service
    "IndexCache": "coding_trajectory.service",
    "ServiceContext": "coding_trajectory.service",
    "dispatch": "coding_trajectory.service",
    "resolve_store": "coding_trajectory.service",
    # runtime
    "ServiceRuntime": "coding_trajectory.runtime",
    "ServiceApiClient": "coding_trajectory.runtime",
    "PluginApiClient": "coding_trajectory.runtime",
    "PluginApiError": "coding_trajectory.runtime",
    "default_plugin_client": "coding_trajectory.runtime",
}

__all__ = sorted(_LAZY_EXPORTS)

if TYPE_CHECKING:
    from coding_trajectory.contracts import (
        SERVICE_CONTRACTS,
        command_schema,
        service_contract,
    )
    from coding_trajectory.discovery import stabilize_session
    from coding_trajectory.ingestion import (
        AgentMessageItem,
        BaseAdapter,
        ClaudeCodeAdapter,
        ClaudeCodeExtensions,
        CodexAdapter,
        CodexExtensions,
        CommandExecutionItem,
        Event,
        EventType,
        FileChangeItem,
        Item,
        ItemBase,
        PiAdapter,
        PiExtensions,
        PlanItem,
        ReasoningItem,
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
    from coding_trajectory.query import (
        DocumentError,
        DocumentStore,
        ResourceNotFoundError,
    )
    from coding_trajectory.runtime import (
        PluginApiClient,
        PluginApiError,
        ServiceApiClient,
        ServiceRuntime,
        default_plugin_client,
    )
    from coding_trajectory.service import (
        IndexCache,
        ServiceContext,
        dispatch,
        resolve_store,
    )


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
