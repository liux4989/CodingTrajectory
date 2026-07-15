#!/usr/bin/env bash
set -euo pipefail

if (($#)); then
  changed_paths="$(git diff --name-only "$@")"
else
  changed_paths="$(git status --porcelain=v1 --untracked-files=all | sed -E 's/^...//' | sed -E 's/.* -> //')"
  if [[ -z "$changed_paths" ]] && git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    changed_paths="$(git diff --name-only HEAD^ HEAD)"
  fi
fi

trigger_pattern='^(packages/core/src/coding_trajectory/(ingestion|metrics|analysis)/|packages/core/src/coding_trajectory/(contracts|service|runtime)\.py$|docs/token-usage-glossary\.md$|validation/metrics/|scripts/validate-metrics-baselines\.py$)'

if ! grep -Eq "$trigger_pattern" <<<"$changed_paths"; then
  echo "metrics quality gate: skipped (no metric-sensitive paths changed)"
  exit 0
fi

echo "metrics quality gate: running committed baselines"
uv run python scripts/validate-metrics-baselines.py
