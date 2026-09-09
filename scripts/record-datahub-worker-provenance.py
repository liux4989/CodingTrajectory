#!/usr/bin/env python3
"""Verify and record the exact Python modules staged for the Datahub Worker."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModuleOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str
    source: str
    staged: str
    sha256: str


class WorkerProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_commit: str
    uv_lock_sha256: str
    pylock_sha256: str
    modules: list[ModuleOrigin]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-dir", type=Path, required=True)
    args = parser.parse_args()

    plugin_dir = args.plugin_dir.resolve()
    repo_dir = plugin_dir.parents[2]
    source_modules = (
        "datahub_plugin/api_models.py",
        "datahub_plugin/hosted/service.py",
        "datahub_plugin/hosted/transport.py",
    )
    modules: list[ModuleOrigin] = []
    for module in source_modules:
        source = plugin_dir / module
        staged = plugin_dir / "python_modules" / module
        if not source.is_file() or not staged.is_file():
            raise SystemExit(f"required Worker module is missing: {module}")
        source_hash = _digest(source)
        if source_hash != _digest(staged):
            raise SystemExit(
                f"stale Worker module detected: {module} differs from the checkout"
            )
        modules.append(
            ModuleOrigin(
                module=module,
                source=str(source.relative_to(repo_dir)),
                staged=str(staged.relative_to(repo_dir)),
                sha256=source_hash,
            )
        )

    core_dir = repo_dir / "packages/core/src"
    for module in (
        "coding_trajectory/control_plane/remote.py",
        "coding_trajectory/control_plane/remote_inventory.py",
        "coding_trajectory/service/handlers.py",
    ):
        source = core_dir / module
        staged = plugin_dir / "python_modules" / module
        if not source.is_file() or not staged.is_file():
            raise SystemExit(f"required bundled core module is missing: {module}")
        source_hash = _digest(source)
        if source_hash != _digest(staged):
            raise SystemExit(
                f"stale bundled core module detected: {module} differs from the checkout"
            )
        modules.append(
            ModuleOrigin(
                module=module,
                source=str(source.relative_to(repo_dir)),
                staged=str(staged.relative_to(repo_dir)),
                sha256=source_hash,
            )
        )

    receipt = WorkerProvenance(
        source_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True
        ).strip(),
        uv_lock_sha256=_digest(repo_dir / "uv.lock"),
        pylock_sha256=_digest(plugin_dir / "pylock.toml"),
        modules=modules,
    )
    output = repo_dir / ".artifacts/datahub-release/worker-provenance.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(receipt.model_dump_json(indent=2) + "\n")
    print(f"Worker provenance verified: {output}")


if __name__ == "__main__":
    main()
