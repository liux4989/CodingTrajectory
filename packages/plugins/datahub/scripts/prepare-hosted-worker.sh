#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_exec="${UV_BIN:-uv}"
uv_exec="$(command -v "$uv_exec")"
uv_dir="$(cd "$(dirname "$uv_exec")" && pwd)"
uv_exec="$uv_dir/$(basename "$uv_exec")"
uv_work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ct-datahub-uv.XXXXXX")"
trap 'rmdir "$uv_work_dir" 2>/dev/null || true' EXIT

export UV_NO_EDITABLE=1
export PATH="$uv_dir${PATH:+:$PATH}"
cd "$plugin_dir"

# Keep pywrangler's nested uv commands outside the parent workspace so uv honors
# the Python and Pyodide virtual environments selected by pywrangler.
"$uv_exec" run --package ct-plugin-datahub "$BASH" -c '
  export UV_WORKING_DIR="$1"
  shift
  exec pywrangler "$@"
' _ "$uv_work_dir" sync --force
"$uv_exec" pip install \
  --target python_modules \
  --no-deps \
  --refresh-package ct-plugin-datahub \
  "$plugin_dir"
