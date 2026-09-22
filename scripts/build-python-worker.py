"""Seal a standalone Python Worker source/dependency tree and dry-run its upload.

Run the Worker check/sync first. Deployment uses the copied configuration with
the locked Wrangler executable, never a fresh dependency resolution.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/control-plane"


def build(destination: Path):
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("src", "python_modules"):
        if not (WORKER / name).is_dir():
            raise ValueError(
                f"missing {name}; run npm --prefix cloudflare/control-plane run check"
            )
        shutil.copytree(
            WORKER / name,
            destination / name,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.ts", "contracts.json", "validators.js"
            ),
        )
    for name in ("pyproject.toml", "uv.lock", "pylock.toml"):
        shutil.copyfile(WORKER / name, destination / name)
    config = json.loads((WORKER / "wrangler.jsonc").read_text())
    config.pop("$schema", None)
    (destination / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    subprocess.run(
        [
            str(WORKER / "node_modules/.bin/wrangler"),
            "deploy",
            "--config",
            str(destination / "wrangler.jsonc"),
            "--env",
            "staging",
            "--dry-run",
        ],
        cwd=destination,
        env={**os.environ, "WRANGLER_SEND_METRICS": "false", "CI": "true"},
        check=True,
    )
    # Wrangler's machine-local cache and logs are not upload inputs.
    shutil.rmtree(destination / ".wrangler", ignore_errors=True)
    print(f"Prepared standalone Python Worker at {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", required=True, type=Path)
    build(parser.parse_args().outdir)
