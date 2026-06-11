#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    uv = shutil.which("uv")
    if not uv:
        print("error: uv was not found on PATH", file=sys.stderr)
        return 127
    try:
        summary = check_packaged_plugins(repo, uv=uv, keep_tmp=args.keep_tmp)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def check_packaged_plugins(repo: Path, *, uv: str, keep_tmp: bool) -> dict[str, Any]:
    package_dirs = _package_dirs(repo)
    package_names = [_project_name(path) for path in package_dirs]
    temp_context = _persistent_tmpdir() if keep_tmp else tempfile.TemporaryDirectory(prefix="ct-plugin-check.")
    with temp_context as raw_tmp:
        tmp = Path(raw_tmp)
        dist_dir = tmp / "dist"
        venv_dir = tmp / "venv"
        registry_path = tmp / "plugins.json"
        dist_dir.mkdir()
        for package_dir in package_dirs:
            _run([uv, "build", str(package_dir), "--wheel", "--out-dir", str(dist_dir)], cwd=repo)
        wheels = sorted(dist_dir.glob("*.whl"))
        _validate_wheels(wheels)
        _run([uv, "venv", str(venv_dir)], cwd=repo)
        python = _venv_python(venv_dir)
        _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--find-links",
                str(dist_dir),
                *package_names,
            ],
            cwd=repo,
        )
        ct = _venv_bin(venv_dir, "ct")
        env = {
            "CT_PLUGIN_REGISTRY": str(registry_path),
            "CT_TELEMETRY": "0",
        }
        _run([str(ct), "plugin", "register-builtins", "--output", "json"], cwd=repo, env=env)
        plugins = _json_run([str(ct), "plugin", "list", "--output", "json"], cwd=repo, env=env)[
            "plugins"
        ]
        failed = [plugin for plugin in plugins if plugin.get("status") != "loaded"]
        if failed:
            raise RuntimeError(f"packaged plugins failed to load: {failed!r}")
        names = sorted(plugin["name"] for plugin in plugins)
        expected_plugins = sorted(
            name.removeprefix("ct-plugin-")
            for name in package_names
            if name.startswith("ct-plugin-")
        )
        if names != expected_plugins:
            raise RuntimeError(f"registered plugins {names!r} did not match expected {expected_plugins!r}")
        for plugin in plugins:
            run = plugin.get("run") or []
            if not run:
                raise RuntimeError(f"plugin {plugin.get('name')} has no run command")
            executable = _venv_bin(venv_dir, run[0])
            _run([str(executable), "--manifest"], cwd=repo, env=env)
        _run([str(ct), "plugin", "dashboard", "web", "--help"], cwd=repo, env=env)
        return {
            "status": "ok",
            "tmp": str(tmp) if keep_tmp else None,
            "wheels": [wheel.name for wheel in wheels],
            "plugins": names,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and smoke-check packaged ct plugins.")
    parser.add_argument("--repo", default=Path.cwd(), help="Repository root.")
    parser.add_argument("--keep-tmp", action="store_true", help="Keep the temporary build directory.")
    return parser


def _package_dirs(repo: Path) -> list[Path]:
    package_dirs = [
        repo / "packages" / "core",
        repo / "packages" / "cli",
        *sorted((repo / "packages" / "plugins").glob("*")),
    ]
    result = [path for path in package_dirs if (path / "pyproject.toml").is_file()]
    if len(result) < 3:
        raise RuntimeError(f"repository package layout was not found under {repo}")
    return result


def _project_name(package_dir: Path) -> str:
    data = tomllib.loads((package_dir / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["name"])


def _validate_wheels(wheels: list[Path]) -> None:
    contents = {wheel.name: _wheel_names(wheel) for wheel in wheels}
    for wheel, names in contents.items():
        if wheel.startswith("ct_plugin_") and not any(name.endswith("/ct-plugin.json") for name in names):
            raise RuntimeError(f"{wheel} does not contain ct-plugin.json")
    dashboard = next((names for wheel, names in contents.items() if wheel.startswith("ct_plugin_dashboard-")), None)
    if dashboard is None:
        raise RuntimeError("dashboard plugin wheel was not built")
    required = {
        "ct_plugin_dashboard/ct-plugin.json",
        "ct_plugin_dashboard/dashboard_web.py",
        "ct_plugin_dashboard/web_services.py",
        "ct_plugin_dashboard/web/dist/index.html",
    }
    missing = sorted(required - dashboard)
    if missing:
        raise RuntimeError(f"dashboard plugin wheel is missing {missing!r}")


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _venv_bin(venv_dir: Path, name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / f"{name}{suffix}"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        env=None if env is None else {**os.environ, **env},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed with exit code {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _json_run(command: list[str], *, cwd: Path, env: dict[str, str]) -> Any:
    completed = _run(command, cwd=cwd, env=env)
    return json.loads(completed.stdout)


class _persistent_tmpdir:
    def __enter__(self) -> str:
        self.path = tempfile.mkdtemp(prefix="ct-plugin-check.")
        return self.path

    def __exit__(self, *_exc: object) -> bool:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
