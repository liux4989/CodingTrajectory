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

Private local retrieval reports use `.artifacts/session-retrieval-local/`.
Local rollout receipts are retained separately under `.artifacts/reset-rollout/`.
Neither directory is a source of public expected values.

Old checked-in dashboard/query/retrieval reports and the standalone June HTML
report were removed during the September 5 cleanup. Git history retains them;
dated measurements in design notes remain historical observations. New runs
must not overwrite committed acceptance fixtures or silently establish a new
baseline. Review and sanitize any report before deliberately publishing it.
