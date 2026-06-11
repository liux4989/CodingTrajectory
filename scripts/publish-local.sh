#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/publish-local.sh [--check] [--dry-run] [--no-register]

Publish this checkout to the user-level uv tool install.

Options:
  --check        Run the packaged plugin smoke check before publishing.
  --dry-run      Print the commands without changing the uv tool or registry.
  --no-register  Skip built-in plugin registration after uv tool install.
  -h, --help     Show this help.
EOF
}

check=false
dry_run=false
register=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      check=true
      ;;
    --dry-run)
      dry_run=true
      ;;
    --no-register)
      register=false
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

if [[ ! -f packages/cli/pyproject.toml || ! -f packages/core/pyproject.toml || ! -d packages/plugins ]]; then
  echo "error: scripts/publish-local.sh must run from a CodingTrajectory checkout" >&2
  exit 2
fi

plugin_args=()
while IFS= read -r -d '' pyproject; do
  plugin_dir="$repo/$(dirname "$pyproject")"
  plugin_args+=(--with-editable "$plugin_dir")
done < <(find packages/plugins -mindepth 2 -maxdepth 2 -name pyproject.toml -print0 | sort -z)

install_cmd=(
  uv tool install --force
  --editable "$repo/packages/cli"
  --with-editable "$repo/packages/core"
  "${plugin_args[@]}"
)

check_cmd=(uv run python scripts/check_packaged_plugins.py)
register_cmd=(uv run ct plugin register-builtins --replace)

if [[ "$check" == true ]]; then
  echo "+ ${check_cmd[*]}"
  if [[ "$dry_run" == false ]]; then
    "${check_cmd[@]}"
  fi
fi

echo "+ ${install_cmd[*]}"
if [[ "$dry_run" == false ]]; then
  "${install_cmd[@]}"
fi

if [[ "$register" == true ]]; then
  echo "+ ${register_cmd[*]}"
  if [[ "$dry_run" == false ]]; then
    "${register_cmd[@]}"
  fi
fi

echo "Local publish complete."
