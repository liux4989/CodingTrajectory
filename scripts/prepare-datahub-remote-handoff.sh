#!/usr/bin/env bash
# Portable dependency setup and static qualification. Does not deploy or read secrets.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
for tool in uv node npm bun; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Required tool is missing: $tool (see docs/datahub-remote-handoff.md)" >&2
    exit 2
  fi
done
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { console.error("Node 22 or newer is required"); process.exit(2); }'

# Metric qualification also imports the CLI, so install the complete workspace.
uv sync --frozen --all-packages
(cd packages/plugins/datahub/web && bun install --frozen-lockfile)
uv run --frozen --package ct-plugin-datahub ruff check packages/plugins/datahub/datahub_plugin/hosted
uv run --frozen --package ct-plugin-datahub python scripts/generate-datahub-api-types.py --check
npm --prefix packages/plugins/datahub/web run check:worker
npm --prefix packages/plugins/datahub/web run build:hosted
bash scripts/check-metrics-quality-gate.sh
uv run --frozen python scripts/validate-metrics-baselines.py
git diff --check
echo "Portable static qualification passed. Python Worker packaging, live runtime, and deployment remain separate gates."
