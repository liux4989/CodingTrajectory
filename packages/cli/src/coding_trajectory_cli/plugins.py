"""Explicit CLI plugin registration and executable dispatch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from coding_trajectory.contracts import SERVICE_CONTRACTS

PLUGIN_REGISTRY_ENV = "CT_PLUGIN_REGISTRY"
RESERVED_PLUGIN_NAMES = {
    "list",
    "publish-local",
    "register",
    "register-builtins",
    "unregister",
}


class PluginTool(BaseModel):
    """One manifest-provided help row for a plugin-owned tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    summary: str = Field(min_length=1)

    @field_validator("name", "summary")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class PluginManifest(BaseModel):
    """Minimal manifest contract for external ct executable plugins."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = Field(alias="schemaVersion")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    run: list[str] = Field(min_length=1)
    requires_ct: str | None = Field(default=None, alias="requiresCt")
    requires_methods: dict[str, int] = Field(
        default_factory=dict, alias="requiresMethods"
    )
    tools: list[PluginTool] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        if any(ch.isspace() for ch in normalized):
            raise ValueError("name must not contain whitespace")
        return normalized

    @field_validator("version", "description", "requires_ct")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("run")
    @classmethod
    def _strip_run(cls, value: list[str]) -> list[str]:
        argv = [item.strip() for item in value if item.strip()]
        if not argv:
            raise ValueError("run must include at least one command token")
        return argv


class PluginRegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: str
    registered_at: datetime


class PluginRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    plugins: dict[str, PluginRegistryEntry] = Field(default_factory=dict)


@dataclass(frozen=True)
class LoadedPlugin:
    """Discovery status for one manifest-backed plugin."""

    source: Path
    manifest: PluginManifest | None = None
    error: str | None = None
    registered_name: str | None = None
    registered_at: datetime | None = None

    @property
    def loaded(self) -> bool:
        return self.error is None and self.manifest is not None

    @property
    def name(self) -> str | None:
        return self.manifest.name if self.manifest else self.registered_name


def plugin_registry_path() -> Path:
    override = os.environ.get(PLUGIN_REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "coding-trajectory" / "plugins.json"


def discover_plugins(*, current_dir: Path | None = None) -> list[LoadedPlugin]:
    """Load only explicitly registered plugin manifests."""
    del current_dir
    loaded: list[LoadedPlugin] = []
    registry = load_plugin_registry()
    for registered_name, entry in sorted(registry.plugins.items()):
        manifest_path = Path(entry.manifest)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            loaded.append(
                LoadedPlugin(
                    source=manifest_path,
                    error=str(exc),
                    registered_name=registered_name,
                    registered_at=entry.registered_at,
                )
            )
            continue
        error = _manifest_compatibility_error(manifest, manifest_path)
        if manifest.name != registered_name:
            error = (
                f"registered name {registered_name!r} does not match manifest name "
                f"{manifest.name!r}"
            )
        if error:
            loaded.append(
                LoadedPlugin(
                    source=manifest_path,
                    manifest=manifest,
                    error=error,
                    registered_name=registered_name,
                    registered_at=entry.registered_at,
                )
            )
            continue
        loaded.append(
            LoadedPlugin(
                source=manifest_path,
                manifest=manifest,
                registered_name=registered_name,
                registered_at=entry.registered_at,
            )
        )
    return loaded


def load_plugin_registry() -> PluginRegistry:
    path = plugin_registry_path()
    if not path.exists():
        return PluginRegistry()
    try:
        return PluginRegistry.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid plugin registry {path}: {exc}") from exc


def register_plugin(
    manifest_path: str | Path, *, replace: bool = False
) -> LoadedPlugin:
    source = Path(manifest_path).expanduser().resolve()
    try:
        manifest = PluginManifest.model_validate_json(
            source.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise ValueError(f"plugin manifest not found: {source}") from exc
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid plugin manifest {source}: {exc}") from exc

    error = _manifest_compatibility_error(manifest, source)
    if error:
        raise ValueError(error)

    registry = load_plugin_registry()
    if manifest.name in registry.plugins and not replace:
        raise ValueError(
            f"plugin already registered: {manifest.name}; pass --replace to update it"
        )
    registered_at = datetime.now(UTC)
    registry.plugins[manifest.name] = PluginRegistryEntry(
        manifest=str(source),
        registered_at=registered_at,
    )
    save_plugin_registry(registry)
    return LoadedPlugin(
        source=source,
        manifest=manifest,
        registered_name=manifest.name,
        registered_at=registered_at,
    )


def unregister_plugin(name: str) -> PluginRegistryEntry:
    registry = load_plugin_registry()
    try:
        removed = registry.plugins.pop(name)
    except KeyError as exc:
        raise ValueError(f"plugin is not registered: {name}") from exc
    save_plugin_registry(registry)
    return removed


def save_plugin_registry(registry: PluginRegistry) -> None:
    path = plugin_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        registry.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def builtin_plugin_manifests() -> list[Path]:
    manifests: dict[str, Path] = {}
    builtin_dir = _repo_builtin_plugin_dir()
    if builtin_dir is not None:
        for manifest in builtin_dir.glob("*/ct-plugin.json"):
            manifests[str(manifest.resolve())] = manifest
    for manifest in _installed_builtin_plugin_manifests():
        manifests[str(manifest.resolve())] = manifest
    return sorted(manifests.values())


def run_plugin(manifest: PluginManifest, source: Path, plugin_args: list[str]) -> int:
    """Execute a manifest-backed plugin with inherited stdio."""
    run = _resolve_run(manifest.run, source)
    if run is None:
        print(
            json.dumps(
                {"error": {"message": f"Plugin command not found: {manifest.run[0]}"}},
                indent=2,
            ),
            file=sys.stderr,
        )
        return 127
    try:
        completed = subprocess.run([*run, *plugin_args], check=False)
    except OSError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, indent=2), file=sys.stderr)
        return 1
    return completed.returncode


def plugin_payload(plugins: list[LoadedPlugin]) -> dict[str, Any]:
    """Structured payload for `ct plugin list`."""
    return {
        "plugins": [
            {
                "name": item.manifest.name if item.manifest else None,
                "version": item.manifest.version if item.manifest else None,
                "description": item.manifest.description if item.manifest else None,
                "run": item.manifest.run if item.manifest else [],
                "requires_ct": item.manifest.requires_ct if item.manifest else None,
                "requires_methods": item.manifest.requires_methods
                if item.manifest
                else {},
                "tools": [tool.model_dump() for tool in item.manifest.tools]
                if item.manifest
                else [],
                "source": str(item.source),
                "registered_at": item.registered_at.isoformat()
                if item.registered_at
                else None,
                "status": "loaded" if item.loaded else "failed",
                "error": item.error,
            }
            for item in plugins
        ]
    }


def _repo_builtin_plugin_dir() -> Path | None:
    candidate = Path(__file__).resolve().parents[4] / "packages" / "plugins"
    return candidate if candidate.is_dir() else None


def _installed_builtin_plugin_manifests() -> list[Path]:
    manifests: list[Path] = []
    for dist in distributions():
        name = dist.metadata.get("Name", "")
        if not name.startswith("ct-plugin-"):
            continue
        for file in dist.files or []:
            if Path(str(file)).name != "ct-plugin.json":
                continue
            manifest = Path(dist.locate_file(file))
            if manifest.is_file():
                manifests.append(manifest)
    return manifests


def _manifest_compatibility_error(manifest: PluginManifest, source: Path) -> str | None:
    if manifest.name in RESERVED_PLUGIN_NAMES:
        return f"reserved plugin name: {manifest.name}"
    if _resolve_run(manifest.run, source) is None:
        return f"plugin command not found: {manifest.run[0]}"
    if manifest.requires_ct:
        try:
            requirement = SpecifierSet(manifest.requires_ct)
        except InvalidSpecifier:
            return f"invalid requiresCt specifier: {manifest.requires_ct}"
        try:
            current_version = version("coding-trajectory")
        except PackageNotFoundError:
            current_version = "0.0.0"
        if current_version not in requirement:
            return (
                f"incompatible ct version: requires {manifest.requires_ct}, "
                f"found {current_version}"
            )
    for method, required_version in manifest.requires_methods.items():
        contract = SERVICE_CONTRACTS.get(method)
        if contract is None:
            return f"required ct method is unavailable: {method}"
        if contract.version < required_version:
            return (
                f"required ct method version is unavailable: {method} "
                f"needs {required_version}, found {contract.version}"
            )
    return None


def _resolve_run(run: list[str], source: Path) -> list[str] | None:
    run = [_resolve_relative_arg(item, source) for item in run]
    command = run[0]
    command_path = Path(command).expanduser()
    if command_path.is_absolute():
        return [str(command_path), *run[1:]] if command_path.exists() else None
    if os.sep in command or (os.altsep and os.altsep in command):
        resolved = (source.parent / command_path).resolve(strict=False)
        return [str(resolved), *run[1:]] if resolved.exists() else None
    for executable in (Path(sys.executable), Path(sys.executable).resolve()):
        tool_script = executable.parent / command
        if tool_script.exists():
            return [str(tool_script), *run[1:]]
    resolved_command = shutil.which(command)
    return [resolved_command, *run[1:]] if resolved_command else None


def _resolve_relative_arg(value: str, source: Path) -> str:
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)
    if os.sep in value or (os.altsep and os.altsep in value):
        return str((source.parent / path).resolve(strict=False))
    return value
