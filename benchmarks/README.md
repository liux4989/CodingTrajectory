# Benchmarks and generated artifacts

Reproducible inputs belong in Git: fixtures, benchmark scripts, and example
configuration. The metric acceptance corpus remains in `validation/metrics/`,
including source evidence, provenance, audits, pinned pricing, and expected
responses. It is not disposable benchmark output.

Generated reports go to the ignored `.artifacts/benchmarks/` directory:

- `uv run python scripts/benchmark-query.py` measures local query performance.
- `uv run python scripts/benchmark-session-retrieval.py` runs synthetic retrieval checks.
- `uv run ct-bench LOGFILE` runs the separate agent/judge experiment and can
  invoke external agent processes; it is not the metric acceptance workflow.

For the current local historical API versus prepared artifact reads, run:

```sh
uv sync --package coding-trajectory-core
bench_home=$(mktemp -d)
HOME="$bench_home" uv run --package coding-trajectory-core python \
  scripts/benchmark-local-api-artifacts.py --graphs 29 --turns 100 --repeat 3 \
  --output .artifacts/benchmarks/local-api-artifacts-29.json
rm -rf "$bench_home"
```

Repeat with `--graphs 116` for the larger inventory. This reuses the synthetic
Amp journals from the older compute benchmark but calls current `ServiceRuntime`,
`LocalPublishedFactRepository`, and `CloudflareArtifactRepository`, without the
older benchmark's pinned prototype wrappers. It checks full response equality and
that warm artifact reads fetch no additional objects. Network access is prohibited.
The disposable home isolates provider discovery and the persisted locator cache.

To use real remote-orb sources without a Mac or production API calls, replace
`--graphs 29 --turns 100` with `--logs /path/to/frozen-private-amp-journals`.
The directory must contain a stable copy of Amp JSONL journals, not an actively
captured directory. Use the same disposable-home wrapper; real inputs are read
with global scope inside that isolated home. The benchmark verifies that input
hashes remain unchanged and emits only aggregate counts, timings, and hashes.
Keep raw journals and archives private and outside Git; do not publish them with
the aggregate report. A small captured corpus is not a production capacity test.

Cold timings mean fresh runtimes, not cold OS or locator caches. Artifact reads
decode in-memory objects, excluding disk, network, authentication, and publication.
Preparation (parse, project, prepare summaries, serialize) is reported separately;
this is not a durable collector reuse benchmark. **The ordinary local API does not
currently consume prepared artifact summaries.** This comparison measures the
potential read-side benefit, not a shipped local-cache speedup. The older
`benchmark-query.py` instead measures direct DocumentStore ingestion/projection,
not the full public historical API path.

Private local retrieval reports use `.artifacts/session-retrieval-local/`.
Local rollout receipts are retained separately under `.artifacts/reset-rollout/`.
Neither directory is a source of public expected values.

Old checked-in dashboard/query/retrieval reports and the standalone June HTML
report were removed during the September 5 cleanup. Git history retains them;
dated measurements in design notes remain historical observations. New runs
must not overwrite committed acceptance fixtures or silently establish a new
baseline. Review and sanitize any report before deliberately publishing it.
