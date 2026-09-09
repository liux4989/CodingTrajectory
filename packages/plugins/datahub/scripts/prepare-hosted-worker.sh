#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$plugin_dir/../../.." && pwd)"
uv_exec="${UV_BIN:-uv}"
uv_exec="$(command -v "$uv_exec")"
uv_dir="$(cd "$(dirname "$uv_exec")" && pwd)"
uv_exec="$uv_dir/$(basename "$uv_exec")"
uv_work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ct-datahub-uv.XXXXXX")"
trap 'rmdir "$uv_work_dir" 2>/dev/null || true' EXIT

export UV_NO_EDITABLE=1
export PATH="$uv_dir${PATH:+:$PATH}"
cd "$plugin_dir"
rm -rf python_modules .venv-workers

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

# Wrangler flattens checkout-discovered Python modules, while the facade uses
# package-qualified imports. Install the project package only after deleting the
# complete staging tree, then prove every staged entrypoint matches this checkout.
"$uv_exec" run python "$repo_dir/scripts/record-datahub-worker-provenance.py" \
  --plugin-dir "$plugin_dir"
