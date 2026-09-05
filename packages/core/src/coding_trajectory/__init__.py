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
    "AmpAdapter": "coding_trajectory.ingestion",
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
        SERVICE_CONTRACTS as SERVICE_CONTRACTS,
        command_schema as command_schema,
        service_contract as service_contract,
    )
    from coding_trajectory.discovery import (
        stabilize_session as stabilize_session,
    )
    from coding_trajectory.ingestion import (
        AgentMessageItem as AgentMessageItem,
        AmpAdapter as AmpAdapter,
        BaseAdapter as BaseAdapter,
        ClaudeCodeAdapter as ClaudeCodeAdapter,
        ClaudeCodeExtensions as ClaudeCodeExtensions,
        CodexAdapter as CodexAdapter,
        CodexExtensions as CodexExtensions,
        CommandExecutionItem as CommandExecutionItem,
        Event as Event,
        EventType as EventType,
        FileChangeItem as FileChangeItem,
        Item as Item,
        ItemBase as ItemBase,
        PiAdapter as PiAdapter,
        PiExtensions as PiExtensions,
        PlanItem as PlanItem,
        ReasoningItem as ReasoningItem,
        Session as Session,
        SessionEdge as SessionEdge,
        SessionGraph as SessionGraph,
        SessionGraphSummary as SessionGraphSummary,
        SessionStatus as SessionStatus,
        ToolCallItem as ToolCallItem,
        ToolStatus as ToolStatus,
        Turn as Turn,
        TurnStatus as TurnStatus,
        Vendor as Vendor,
        VendorExtensions as VendorExtensions,
    )
    from coding_trajectory.query import (
        DocumentError as DocumentError,
        DocumentStore as DocumentStore,
        ResourceNotFoundError as ResourceNotFoundError,
    )
    from coding_trajectory.runtime import (
        PluginApiClient as PluginApiClient,
        PluginApiError as PluginApiError,
        ServiceApiClient as ServiceApiClient,
        ServiceRuntime as ServiceRuntime,
        default_plugin_client as default_plugin_client,
    )
    from coding_trajectory.service import (
        IndexCache as IndexCache,
        ServiceContext as ServiceContext,
        dispatch as dispatch,
        resolve_store as resolve_store,
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
