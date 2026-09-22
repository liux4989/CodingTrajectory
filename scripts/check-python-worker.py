"""Validate Python syntax and shared imports without starting a remote Worker."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/control-plane"


def main():
    source = WORKER / "src"
    files = sorted(source.rglob("*.py"))
    for path in files:
        ast.parse(path.read_text(), filename=str(path))
    config = json.loads((WORKER / "wrangler.jsonc").read_text())
    if (
        config["main"] != "src/index.py"
        or "python_workers" not in config["compatibility_flags"]
    ):
        raise ValueError("Worker must use the Python entrypoint and runtime")
    if not (WORKER / config["main"]).is_file():
        raise ValueError("Python Worker entrypoint missing")
    sys.path.insert(0, str(source))
    from coding_trajectory.contracts import service_contract
    from coding_trajectory.control_plane.artifact_protocol import (
        ArtifactPublicationRequest,
    )
    from coding_trajectory.control_plane.collector_protocol import (
        CollectorRecoveryRequest,
    )

    for model in (
        ArtifactPublicationRequest,
        CollectorRecoveryRequest,
        service_contract("session.overview").request_model,
    ):
        model.model_json_schema()
    forbidden = {"numpy", "tiktoken", "httpx"} & sys.modules.keys()
    if forbidden:
        raise ValueError(
            f"host dependencies imported by Worker contracts: {sorted(forbidden)}"
        )
    print(
        f"PASS Python Worker: {len(files)} source files; shared Pydantic contracts import without host dependencies"
    )


if __name__ == "__main__":
    main()
