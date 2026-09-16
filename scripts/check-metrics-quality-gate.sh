#!/usr/bin/env bash
set -euo pipefail

if (($#)); then
  changed_paths="$(git diff --name-only "$@")"
else
  working_paths="$(git status --porcelain=v1 --untracked-files=all | sed -E 's/^...//' | sed -E 's/.* -> //')"
  base_ref="${CT_METRICS_BASE_REF:-origin/main}"
  if git rev-parse --verify "$base_ref^{commit}" >/dev/null 2>&1; then
    merge_base="$(git merge-base HEAD "$base_ref")"
    committed_paths="$(git diff --name-only "$merge_base" HEAD)"
  elif git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    committed_paths="$(git diff --name-only HEAD^ HEAD)"
  else
    committed_paths=""
  fi
  changed_paths="$(printf '%s\n%s\n' "$committed_paths" "$working_paths" | sed '/^$/d' | sort -u)"
fi

trigger_pattern='^(packages/core/src/coding_trajectory/(ingestion|metrics|analysis)/|packages/core/src/coding_trajectory/(contracts|service|runtime)\.py$|docs/token-usage-glossary\.md$|validation/metrics/|scripts/validate-metrics-baselines\.py$)'

if ! grep -Eq "$trigger_pattern" <<<"$changed_paths"; then
  echo "metrics quality gate: skipped (no metric-sensitive paths changed)"
  exit 0
fi

echo "metrics quality gate: running committed baselines"
uv run python scripts/validate-metrics-baselines.py
