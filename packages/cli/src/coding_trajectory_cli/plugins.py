"""Manifest-based CLI plugin discovery and executable dispatch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PLUGIN_MANIFEST_SCHEMA_VERSION = 1
PLUGIN_MANIFEST_ENV = "CT_PLUGIN_MANIFEST_PATH"
RESERVED_PLUGIN_NAMES = {"list"}


class PluginCommand(BaseModel):
    """One manifest-provided help row for a plugin-owned command."""

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
    command: str = Field(min_length=1)
    requires_ct: str | None = Field(default=None, alias="requiresCt")
    commands: list[PluginCommand] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        if any(ch.isspace() for ch in normalized):
            raise ValueError("name must not contain whitespace")
        return normalized

    @field_validator("version", "description", "command", "requires_ct")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("capabilities")
    @classmethod
    def _strip_capabilities(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


@dataclass(frozen=True)
class LoadedPlugin:
    """Discovery status for one manifest-backed plugin."""

    source: Path
    manifest: PluginManifest | None = None
    error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.error is None and self.manifest is not None

    @property
    def name(self) -> str | None:
        return self.manifest.name if self.manifest else None


def plugin_manifest_dirs(*, current_dir: Path | None = None) -> list[Path]:
    """Return manifest directories in lookup order."""
    cwd = current_dir or Path.cwd()
    dirs: list[Path] = []
    env_value = os.environ.get(PLUGIN_MANIFEST_ENV)
    if env_value:
        dirs.extend(Path(item).expanduser() for item in env_value.split(os.pathsep) if item)
    dirs.extend([cwd / ".ct" / "plugins", Path.home() / ".ct" / "plugins"])
    return _dedupe_paths(dirs)


def discover_plugins(*, current_dir: Path | None = None) -> list[LoadedPlugin]:
    """Read all plugin manifests from known manifest directories."""
    loaded: list[LoadedPlugin] = []
    for manifest_path in _manifest_paths(plugin_manifest_dirs(current_dir=current_dir)):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            loaded.append(LoadedPlugin(source=manifest_path, error=str(exc)))
            continue
        if manifest.name in RESERVED_PLUGIN_NAMES:
            loaded.append(LoadedPlugin(source=manifest_path, error=f"reserved plugin name: {manifest.name}"))
            continue
        loaded.append(LoadedPlugin(source=manifest_path, manifest=manifest))
    return _dedupe_plugins(loaded)


def run_plugin(manifest: PluginManifest, source: Path, plugin_args: list[str]) -> int:
    """Execute a manifest-backed plugin with inherited stdio."""
    command = _resolve_command(manifest.command, source)
    if command is None:
        print(
            json.dumps(
                {"error": {"message": f"Plugin executable not found: {manifest.command}"}},
                indent=2,
            ),
            file=sys.stderr,
        )
        return 127
    try:
        completed = subprocess.run([command, *plugin_args], check=False)
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
                "command": item.manifest.command if item.manifest else None,
                "requires_ct": item.manifest.requires_ct if item.manifest else None,
                "commands": [
                    command.model_dump()
                    for command in item.manifest.commands
                ] if item.manifest else [],
                "capabilities": item.manifest.capabilities if item.manifest else [],
                "source": str(item.source),
                "status": "loaded" if item.loaded else "failed",
                "error": item.error,
            }
            for item in plugins
        ]
    }


def _manifest_paths(dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for manifest_dir in dirs:
        if not manifest_dir.is_dir():
            continue
        paths.extend(sorted(manifest_dir.glob("*.json")))
    return _dedupe_paths(paths)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        key = path.expanduser().resolve(strict=False)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _dedupe_plugins(plugins: list[LoadedPlugin]) -> list[LoadedPlugin]:
    seen: set[str] = set()
    unique: list[LoadedPlugin] = []
    for item in plugins:
        name = item.name
        if name is not None:
            if name in seen:
                unique.append(LoadedPlugin(source=item.source, error=f"duplicate plugin name: {name}"))
                continue
            seen.add(name)
        unique.append(item)
    return unique


def _resolve_command(command: str, source: Path) -> str | None:
    command_path = Path(command).expanduser()
    if command_path.is_absolute():
        return str(command_path) if command_path.exists() else None
    if os.sep in command or (os.altsep and os.altsep in command):
        resolved = (source.parent / command_path).resolve(strict=False)
        return str(resolved) if resolved.exists() else None
    return shutil.which(command)
