# Benchmarks and artifacts

Commit reproducible inputs: fixtures, scripts, and example configuration.
Write generated reports to ignored `.artifacts/benchmarks/`, not `docs/`.
Review and sanitize a report before sharing it.

The audited corpus in `validation/metrics/` is acceptance evidence, not disposable
output. Never replace its expected values with benchmark results. See
[metrics validation](../docs/metrics-validation-quality-gate.md).

## Choose a workload

| Command | Scope |
| --- | --- |
| `uv run python scripts/benchmark-query.py` | Local store ingestion and projection |
| `uv run python scripts/benchmark-session-retrieval.py` | Synthetic retrieval qualification |
| `uv run python scripts/benchmark-local-api-artifacts.py --help` | Local API versus in-memory artifact-reader comparison |
| `uv run ct-bench LOGFILE` | Separate agent/judge experiment; can invoke external agents |

Agent/judge experiments are not the metric acceptance gate. Obtain authorization
before invoking external agents or sending private evidence.

## Run an isolated local comparison

From the repository root:

```sh
uv sync --package coding-trajectory-core
bench_home=$(mktemp -d)
HOME="$bench_home" uv run --package coding-trajectory-core python \
  scripts/benchmark-local-api-artifacts.py --graphs 29 --turns 100 --repeat 3 \
  --output .artifacts/benchmarks/local-api-artifacts-29.json
rm -rf "$bench_home"
```

The disposable home isolates discovery and locator caches. The harness checks
response equality, source hashes, and warm object-read reuse. It prohibits network
access and disables live pricing. Repeat with `--graphs 116` for a larger synthetic inventory.

For private Amp inputs, replace `--graphs 29 --turns 100` with
`--vendor amp --logs /path/to/frozen-amp-journals`. Use a frozen copy, not active journals.
Keep raw logs and archives outside Git and public reports.

For Codex, copy the frozen source tree into `$bench_home/.codex/sessions`.
Preserve directory structure, segments, and selected parent/child sources.
Use `--vendor codex_cli --logs "$bench_home/.codex/sessions"` with the same isolated
`HOME`. Do not point `CT_AMP_LOG_DIR` at Codex sources.

Select the corpus before measuring speed. A small corpus does not qualify production capacity.

## Interpret results

- Cold means a fresh runtime, not a cold OS cache.
- Artifact reads decode in-memory bytes. They exclude disk, network,
  authentication, and publication latency.
- Preparation time is separate from reads. This is not a collector-cache benchmark.
- The comparison artifact reader is benchmark-only. Production remote clients
  use the prepared API described in [architecture](../docs/architecture.md#remote-authority).
- Local reads already share graph preparation. Comparison results are not proof
  of a new local-cache improvement.
- Stage timings retain unassigned residual work. Median stage fractions need not sum to one.
- `--fact-projection-profile` adds instrumentation overhead. Cumulative profile
  times include callees; do not sum them across nested functions.
- `--expected-preparation-fingerprint JSON` checks a frozen uninstrumented baseline.
  Use identical source paths as well as bytes, because provenance affects identity.

Keep private retrieval reports in `.artifacts/session-retrieval-local/`.
Keep operational receipts in their private run directories, not benchmark output.
Git history retains removed reports; old measurements are not current guarantees.
