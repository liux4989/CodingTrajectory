"""CLI plugin loading and helper utilities."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Callable, Protocol

from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch, resolve_store

PLUGIN_ENTRY_POINT_GROUP = "coding_trajectory.cli_plugins"

PluginHandler = Callable[[argparse.Namespace], dict[str, Any]]
PluginRenderer = Callable[[argparse.Namespace, dict[str, Any]], str]


class CtCliPlugin(Protocol):
    """Protocol implemented by external ct CLI plugins."""

    name: str

    def register(self, namespace_subparsers: argparse._SubParsersAction, ctx: "CtPluginContext") -> None:
        """Register plugin commands under the `ct plugin` namespace."""


@dataclass(frozen=True)
class LoadedPlugin:
    """Load status for one plugin entry point."""

    entry_point: str
    module: str
    plugin_name: str | None = None
    error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class CtPluginContext:
    """Stable helper surface exposed to ct CLI plugins."""

    render_json: PluginRenderer

    def bind_command(
        self,
        parser: argparse.ArgumentParser,
        *,
        handler: PluginHandler,
        renderer: PluginRenderer | None = None,
    ) -> None:
        """Attach a plugin handler and optional renderer to a parser."""
        parser.set_defaults(
            _plugin_handler=handler,
            _render_payload=renderer or self.render_json,
        )

    def dispatch_core(
        self,
        *,
        method: str,
        params: dict[str, Any],
        global_scope: bool = False,
        current_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Run a first-party ct core method using the standard discovery path."""
        effective_current_dir = current_dir or Path.cwd()
        cache = IndexCache.load()
        effective_global_scope = True if method == "project.list" else global_scope
        store, discovery_note = resolve_store(
            params,
            log_file=None,
            global_scope=effective_global_scope,
            current_dir=effective_current_dir,
            cache=cache,
        )
        result = dispatch(
            method,
            params,
            store=store,
            global_scope=effective_global_scope,
            current_dir=effective_current_dir,
            discovery_note=discovery_note,
            cache=cache,
        )
        cache.save()
        return result

    def resolve_document_store(
        self,
        *,
        params: dict[str, Any],
        global_scope: bool = False,
        current_dir: Path | None = None,
    ) -> tuple[DocumentStore, str]:
        """Expose targeted/full ct store resolution without dispatch."""
        effective_current_dir = current_dir or Path.cwd()
        cache = IndexCache.load()
        store, discovery_note = resolve_store(
            params,
            log_file=None,
            global_scope=global_scope,
            current_dir=effective_current_dir,
            cache=cache,
        )
        cache.save()
        return store, discovery_note


def load_plugins(
    namespace_subparsers: argparse._SubParsersAction,
    *,
    ctx: CtPluginContext,
) -> list[LoadedPlugin]:
    """Load and register installed ct CLI plugins from entry points."""
    loaded: list[LoadedPlugin] = []
    for ep in sorted(entry_points(group=PLUGIN_ENTRY_POINT_GROUP), key=lambda item: item.name):
        try:
            plugin = _coerce_plugin(ep)
            plugin.register(namespace_subparsers, ctx)
        except Exception as exc:
            loaded.append(
                LoadedPlugin(
                    entry_point=ep.name,
                    module=ep.value,
                    error=str(exc),
                )
            )
            continue
        loaded.append(
            LoadedPlugin(
                entry_point=ep.name,
                module=ep.value,
                plugin_name=plugin.name,
            )
        )
    return loaded


def _coerce_plugin(ep: EntryPoint) -> CtCliPlugin:
    candidate = ep.load()
    plugin = candidate() if callable(candidate) and not hasattr(candidate, "register") else candidate
    name = getattr(plugin, "name", None)
    register = getattr(plugin, "register", None)
    if not isinstance(name, str) or not name.strip():
        raise TypeError(f"{ep.name} must expose a plugin object with a non-empty name")
    if not callable(register):
        raise TypeError(f"{ep.name} must expose a plugin object with a callable register(...) method")
    return plugin
