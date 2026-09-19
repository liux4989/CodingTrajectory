# Prepared API staging validation

This harness publishes fresh synthetic `session.overview` fixtures and measures
the dedicated `coding-trajectory-control-plane-staging` Worker. It does not
target production or the graph-overview-v4 Stage 3 Worker.

Generate and locally validate both producer shapes:

```sh
uv run python scripts/qualify-prepared-api.py \
  --shape representative --fixture-output /tmp/representative.json
uv run python scripts/qualify-prepared-api.py \
  --shape near-budget --fixture-output /tmp/near-budget.json
node scripts/qualify-prepared-api.mjs /tmp/representative.json
node scripts/qualify-prepared-api.mjs /tmp/near-budget.json

node scripts/run-prepared-api-staging.mjs plan \
  /tmp/representative.json /tmp/near-budget.json /tmp/plan.json
node scripts/qualify-prepared-api-staging.mjs /tmp/plan.json
uv run python scripts/analyze-prepared-api-staging-traces.py --self-test
```

The remote driver reads the URL, expected version, principal identifiers, and
token from `CT_STAGING_*` environment variables. Secret values must come from a
protected local credential store, never command arguments or evidence. Run
`publish` once, then capture `measure --preflight` with Wrangler 4.129.1 realtime
tail. Analyze the pretty-concatenated capture before considering `--full`.

The analyzer requires exact request-ID correlation for every fetch and an
explicit shared trace identifier for its Durable Object authority invocation.
Missing CPU fields, fetch records, authority records, or joins stop the run.
Client latency is not CPU. R2 counts remain estimates unless supported binding
spans provide observed counts. Exact peak memory remains unqualified.

The frozen workload is four preflight reads per shape, then—only after capture
qualification—one first-observed read, 100 sequential reads, and 25
concurrency-eight batches per shape. The full allocation is exactly 602 overview
requests with no retries.

Publication and measurement reports are saved before and after each request.
On a timeout or transport failure, preserve the partial report and treat the
request outcome as unknown. Do not retry or continue to trace/full workload
without a new review of remote state and the retry boundary.
