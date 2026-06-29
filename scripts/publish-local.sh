#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/publish-local.sh [--dry-run]

Refresh the user-level uv tool install from this checkout.

Plugins are dispatched from source by `ct plugin NAME ...` and no longer
require installation or registration.
EOF
}

dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$script_dir/.." && pwd)"

cd "$repo"

if [[ ! -f packages/cli/pyproject.toml || ! -f packages/core/pyproject.toml ]]; then
  echo "error: scripts/publish-local.sh must run from a CodingTrajectory checkout" >&2
  exit 2
fi

install_cmd=(
  uv tool install --force --reinstall
  --editable "$repo/packages/cli"
  --with-editable "$repo/packages/core"
)

echo "+ ${install_cmd[*]}"
if [[ "$dry_run" == false ]]; then
  "${install_cmd[@]}"
fi

echo "Local publish complete."
