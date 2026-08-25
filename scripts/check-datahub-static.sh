#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$script_dir/.." && pwd)"

cd "$repo"

if [[ ! -f pyproject.toml || ! -d packages/plugins/datahub ]]; then
  echo "error: scripts/check-datahub-static.sh must run from a CodingTrajectory checkout" >&2
  exit 2
fi

echo "+ uv run ruff check packages/plugins/datahub"
uv run ruff check packages/plugins/datahub

echo "+ uv run python -m py_compile packages/plugins/datahub/*.py"
uv run python -m py_compile packages/plugins/datahub/*.py
