#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_exec="${UV_BIN:-uv}"

export UV_NO_EDITABLE=1
cd "$plugin_dir"

"$uv_exec" run --package ct-plugin-datahub pywrangler sync --force
"$uv_exec" pip install \
  --target python_modules \
  --no-deps \
  --refresh-package ct-plugin-datahub \
  "$plugin_dir"
