# Remaining Free-plan risks: disposable local benchmarks, 2026-09-17

> Historical record: the retired fact-reader qualifier and free-plan benchmark scripts mentioned below were removed with the Python Worker replacement. Use the [current Worker validation commands](../cloudflare/control-plane/README.md#validation-and-releases).

**Write amplification and unbounded living history are the immediate constraints.**
This is benchmark evidence, not an optimization, deployment, billing attribution,
or maximum-capacity qualification. No production requests, credentials, private
payloads, R2 operations, or shared writes were used. No runtime files changed.

## Provenance and missing pilot input

- Contracted/fetched baseline: `1d9bc2851ee7667969164016d36a504944bf9362`.
- Local branch `main` was clean at start, HEAD
  `08308bbadf291225f994c759cf6acefd5020ce4c`: only nine CLI plugin-dispatch lines
  differ from that baseline. Cloudflare sources are identical.
- The harness bundles tracked Worker sources directly from the contracted Git
  revision. Generated, ignored `validators.js` is loaded locally and separately
  hashed. JSON reports record exact source, harness and input SHA-256 values.
- Apple M4, arm64, Darwin 27.2.0; Python 3.12.11; Node v22.22.0;
  Miniflare **5.20260907.0-alpha**, workerd **1.20260907.1**; compatibility date
  2026-09-10. The installed Miniflare requires its `convertV4MiniflareOptions`
  adapter. No packages were installed or upgraded.
- **Exact pilot replay is pending.** Documented references contain only a private
  export placeholder. Targeted thread/reference searches did not locate the file;
  the coordinator authorized continuing with synthetic inputs. Expected private
  SHA-256: `bb64366e3c4c4a4cb7c078fd236bcf7c1d5e4f162c1647732114ae18632853c0`.
  No filesystem-wide payload search or credential-store access was performed.

All measured facts below are **synthetic**. Row-oriented fixtures combine 68
independent qualification sessions per graph, with five events per session. They
are not the pilot's distribution (30 sessions, 29 graphs, 29,681 facts, 73 batches,
19,110,381 canonical bytes). The byte-oriented fixture reuses the existing exact
8 MiB graph generator, emphasizing large measurement rows instead of row count.

## Cursor-counted writes substantially exceed logical table mutations

| Synthetic workload | Graphs / rows / batches | Encoded bytes | Fresh stage | Publish + cleanup | Fresh total | Full staging retry, additional |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| [Small](free-plan-row-small-2026-09-17.json) | 10 / 10,210 / 20 | 5,896,810 | 34,100 | 41,607 | **75,707** | 44,300 |
| [Near-pilot row count](free-plan-row-medium-2026-09-17.json) | 29 / 29,609 / 58 | 17,100,749 | 98,890 | 120,647 | **219,537** | 128,470 |
| [Larger row count](free-plan-row-large-2026-09-17.json) | 58 / 59,218 / 116 | 34,201,498 | 197,780 | 241,287 | **439,067** | 256,940 |
| [Byte-heavy](free-plan-byte-large-2026-09-17.json) | 6 / 1,896 / 30 | 50,331,648 | 6,414 | 7,783 | **14,197** | 8,304 |

Units are **local `sql.exec` cursor `rowsWritten`**, including the indexes the
runtime counts. They are not SQLite `total_changes`, approximate VM steps, or
retrieved production billing metrics. Constructor/schema initialization and
project/source/checkpoint setup are excluded from these lifecycle totals.

The near-pilot fixture's fresh lifecycle has **89,091 logical changes** including
metadata, versus 219,537 cursor writes. Its fact-table-only logical model is
3 × 29,609 = 88,827. Source-based reconstruction, independently checked against
the cursor groups, is:

- Stage inserts: three writes per fact (table, primary-key index, graph index),
  plus one per event (partial sequence index), plus batch/generation metadata.
- Published inserts: three writes per fact (table, primary-key index, current
  index), plus one per session (partial session index).
- Cleanup: one cursor write per deleted staged fact; **do not multiply deletes
  by every index**. This runtime's index removals do not add cursor writes.
- With \(N\) facts, \(E\) events, \(S\) sessions, \(B\) batches and \(G\) graphs,
  this fresh scenario yields stage \(3N + E + 3B + G\) and publication
  \(4N + S + B + 6G + 7\).
  The formulas apply to this schema, fresh graph/generation state and one receipt;
  they are not a general billing calculator.

A direct calibration table with a text primary key and secondary index measured
INSERT/UPDATE/DELETE as **3/2/1 cursor writes**, each one logical change. An
intentionally rolled-back insert reported three attempted cursor writes and zero
remaining rows. `total_changes` also retains rolled-back attempts. Neither local
counter establishes whether failed production transactions were billed.

**Committed publication retry returned the identical receipt with zero SQL writes
and zero logical changes in every workload.** The near-pilot replay took 2.83 ms
locally. In contrast, restaging and submitting a new publication sequence for
unchanged facts costs **128,709 writes** (98,861 stage + 29,848 publication), despite
reusing all 29,609 published rows. The collector's unchanged-digest suppression
and receipt recovery remain important; this harness deliberately exercises the
server paths without those client suppressions.

### The recorded production success is still not reconciled

The earlier 89,043 figure is only three logical mutations per pilot fact. Applying
the reconstructed **current fresh-schema model** to the documented pilot counts
gives **224,185 cursor writes**, before setup, canary, failed attempts or retries.
This is an inference from aggregate counts, **not an exact pilot measurement**.

The recorded successful build `7ab67d765039f904a5df9e24e739496b2dbbe6e8` has the
same fact schema and lifecycle writes as the contracted baseline; subsequent
changes in `facts.ts` cover validation, acknowledgments and reads. Merely counting
seven canary rows as reused does not explain the difference. Building the event
index after initial staging changes when index work happens, not proof of its
production charge. The saved publication succeeded after a CPU failure, but no
account-level write usage, failed-attempt charging, timing distribution across
quota windows, or quota enforcement trace is available here. **Do not replace the
successful production record with a claim that publication must have failed.**

Cloudflare currently documents 100,000 written rows/day on Free, but local
Miniflare does not enforce that account allowance: the 439,067-write case succeeds
locally. Reconciling production requires the exact frozen input/journal and an
authorized read-only account usage investigation, not another upload.

## CPU, memory and requests: bounded local completion, not deployment headroom

| Workload | Publication wall ms | workerd process CPU ms | Sampled process RSS MB | Database bytes after publication |
| --- | ---: | ---: | ---: | ---: |
| Small | 535.3 | 530 | 215.7 | 11,223,040 |
| Near-pilot row count | 1,574.1 | 1,550 | 235.2 | 32,272,384 |
| Larger row count | 3,208.4 | 3,170 | 256.6 | 64,446,464 |
| Byte-heavy | 342.2 | 300 | 243.9 | 51,531,776 |

Publication runs **after an identical staging retry**, so these are warmed
measurements. They include validation, commit, receipt, serialization and local
dispatch. Row hashing and privacy checks occur during staging; the byte-heavy
fixture took 2,751.9 ms for fresh staging across 30 calls. Publication bytes alone
do not predict compute: large measurement payloads need fewer relational checks
than many session/item/event facts.

CPU is macOS `ps` cumulative **shared workerd process** CPU delta (10 ms resolution).
RSS is the maximum of 50 ms samples and request endpoints, not an absolute peak.
Both include simulator services, other benchmark objects, SQLite/native allocations
and instrumentation. RSS greater than 128 MB **does not demonstrate an isolate
memory violation**. Null fields on direct SQL/living probes mean not sampled.
No isolate heap peak or separate front-Worker billed CPU was obtained. SQL cursor
proxying and RSS sampling add overhead; local timings are not deployed CPU limits.

Twenty serial warm requests per workload measured median front-Worker-only
`ct_connection_status` latency of **1.72–2.00 ms**, versus **1.81–2.18 ms** for
`ct_workspace_snapshot` including a DO invocation. These tiny requests do not
qualify the Free front Worker's 10 ms CPU budget for a 2 MiB staging request.
Each non-connection-status call in this path makes one Worker request and one DO
RPC session; the near-pilot fresh lifecycle is 58 stages + one publication, before
setup/recovery/reads. Receipt replay adds one request to each layer, but no writes.

No timer, WebSocket, alarm or other non-hibernating activity is installed by the
authority. Deployed DO billable duration was not measured. Multiplying client wall
time by the 128 MB allocation would not correctly separate active DO time, Worker
time, simulator work or concurrent requests. Duration remains an observability
gate rather than an invented cost estimate.

## Living observations have a measured hard lifetime boundary

The harness calls the actual `livingWrite`/`livingRead` functions in a separate
disposable object, batching heartbeat generation into transactions to avoid 10,001
HTTP calls. This measures storage/limit behavior, **not heartbeat HTTP throughput**.
These direct-function heartbeat requests contain only fields consumed by
`livingWrite`, omitting wire-only workspace/time fields and outer schema validation.
Their byte sizes are a minimal synthetic shape, not full production heartbeat size.

- One heartbeat: **four logical mutations, ten cursor writes**: sequence update
  plus versioned lease, living observation and living-head records (three writes
  each). Explicit outer idempotency receipts would add further metadata writes.
- At 1,000 heartbeats: 1,028,096 database bytes; a one-row living read examines
  **4,003 cursor rows**. At 10,000: 9,486,336 bytes and **40,003 cursor rows**.
- At **10,001**: `413 workspace_query_limit`, before page-size/scope filtering.
  All records are heartbeats; there need not be a single canonical session change.
- Lease expiry only hides stale resources. It does not delete observations or
  their versioned leases. No retention/deletion path exists for `records`.

At an assumed one heartbeat/minute and no other observations, this boundary is
reached after about **6.95 days** per workspace; multiple agents share the count.
That is arithmetic, not a claim about the configured collection cadence.
10,000 same-day heartbeats also consume the documented entire 100,000-write daily
allowance before receipts or other activity. Keep living collection bounded until
a retention/query contract is chosen. Do not delete history as an ad hoc repair.

## Fact history grows even when current-row count stays fixed

A SQL-only probe uses the real `fact_rows` schema, 100 synthetic event identities
and 512-character padding, closes current versions and appends replacements:

| Versions per identity | Retained facts | Current facts | Database bytes |
| ---: | ---: | ---: | ---: |
| 1 | 100 | 100 | 151,552 |
| 20 | 2,000 | 100 | 1,318,912 |
| 100 | 10,000 | 100 | 6,246,400 |

Each changed non-session fact adds four cursor writes in this schema: one close
update and three insert writes. This probe excludes staging, manifests and full
graph validation; it is not a publication-capacity test. Runtime publication closes
versions rather than deleting them. There is no automatic fact/history retention.
The source thread independently measured increased historical read VM steps; this
benchmark does not duplicate its read-path correctness audit.

## Measured envelope and next gate

- **Only a conditional write-count envelope is supported:** 10,210 facts in the
  tested ten-graph shape used 75,707 writes for stage + publication. Setup, other
  account usage and retries must fit the remaining allowance. One full stage retry
  raises it to **120,007**, already beyond Free. This is not a daily safe capacity
  or a recommendation to run production collection.
- The near-pilot and larger row fixtures are outside that documented daily write
  envelope. The larger publication also examined 5,288,286 local cursor rows by
  itself; increasing write allowance alone would not qualify it for Free reads.
- Local completion through 59,218 rows / 34.20 MB and a separate 48 MiB byte-heavy
  publication supports only these shapes. No 96 MiB, 16 MiB-per-graph maximum,
  concurrency, sustained-load, cold-isolate or actual Free enforcement claim.
- R2: tracked production config now binds `ARTIFACTS`; source has **no R2 calls**.
  `env.d.ts` still says “no R2 artifact bucket” and omits the binding; staging has
  no R2 binding. These are config/type-documentation differences, not measured
  R2 traffic. No R2 benchmark is relevant to this SQL publication path.
- Next: obtain the frozen pilot locally and reproduce its write/retry lifecycle;
  reconcile recorded production success with authorized account telemetry; measure
  front-Worker CPU and DO isolate memory/duration before raising any workload cap.
  Define retention and historical-snapshot semantics before enabling continuous
  living observations. No deployment or optimization is included here.

## Reproduction and verification

From the repository root with existing dependencies/generated contracts:

```sh
mkdir -p .artifacts/free-plan-benchmark
PYTHONPATH=packages/core/src uv run --no-sync python scripts/prepare-free-plan-benchmark.py \
  .artifacts/free-plan-benchmark/row-medium.json --graphs 29 --sessions 68
node scripts/benchmark-free-plan.mjs .artifacts/free-plan-benchmark/row-medium.json \
  .artifacts/free-plan-benchmark/row-medium-result.json
```

The other exact fixture arguments are `--graphs 10 --sessions 68`,
`--graphs 58 --sessions 68`, and `--graphs 6 --graph-bytes 8388608`.
Each run creates an isolated in-memory/temporary Miniflare environment and disposes
it in `finally`; it reads no Wrangler credentials/configuration and has no remote
target argument. Synthetic fixture generation validates Pydantic fact sets and
reuses existing qualification helpers. Runtime graph validation, inserted/reused
counts, identical receipts, rollback, history counts and both sides of the living
boundary are checked. No unit tests were added. Reports contain aggregates only.

All four workloads completed. A separate near-pilot rerun reproduced every
scenario's write and logical-mutation counts exactly (publication wall 1,617.97 ms
versus 1,574.11 ms). The recorded tables use the first final-harness run, not best
of repetitions. `node --check`, Ruff, whitespace checks and all four committed
metric-baseline fixtures passed. `scripts/check-metrics-quality-gate.sh` skipped
because no metric-sensitive path changed; the full workflow was nevertheless run
directly with `uv run python scripts/validate-metrics-baselines.py` and passed.

Documentation fetched 2026-09-17:
[DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/),
[SQL cursor counters](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/),
[Worker limits](https://developers.cloudflare.com/workers/platform/limits/),
[DO limits](https://developers.cloudflare.com/durable-objects/platform/limits/).
The DO limits page has inconsistent 10 GB table versus 1 GB Free FAQ wording;
no storage-capacity conclusion here depends on selecting one of those values.
