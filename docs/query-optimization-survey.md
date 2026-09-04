# Query Optimization Survey & Benchmark

Survey, baseline measurement, profiling, and optimization of the
`@packages/core` query API.

## 1. The exposed API

The public query surface is the JSON-RPC-style **`ServiceRuntime`**
(`packages/core/src/coding_trajectory/runtime.py`) exposing 13 methods via
`runtime.call(method, params)` / `batch(requests)`:

| group | methods |
|-------|---------|
| collection | `project.list`, `project.sessions` |
| overview | `session.overview`, `graph.overview` |
| usage | `session.usage`, `graph.usage`, `session.model_usage`, `session.request_usage`, `session.tool_usage` |
| stats | `session.stats`, `graph.stats` |
| detail | `session.events`, `session.items` |

### Query path (per call)

```
ServiceRuntime.call
  └─ contract.validate_request
  └─ (short-circuit) project.list
  └─ _store_for -> resolve_store
       ├─ targeted: cache path-index -> _build_store_targeted  (ingest N files)
       └─ fallback: discover_store (ingest ALL matching logs)
  └─ dispatch -> handler -> projection/metrics over DocumentStore
       └─ contract.validate_response (pydantic model_validate + model_dump)
```

Two distinct cost layers:
1. **Store build** - discovery + ingestion (`resolve_store` -> `DocumentStore`).
2. **Projection** - handler logic over a warm `DocumentStore`.

## 2. Benchmark harness

`scripts/benchmark-query.py` - isolates both layers, repeats measurements, and
optionally cProfiles named methods. Current runs write `.artifacts/benchmarks/query-baseline.json`.

## 3. Results

Target: largest graph on disk (`019f6e1a…`, 112 sessions / 213 turns /
19 284 items / 76 923 events).

### Current worktree benchmark (Phase 7 base vs Phase 8)

Measured on 2026-08-13 as three consecutive runs against the same retained
graph. Store timings use targeted discovery with a warm path-index cache;
projection timings use the already-built in-memory store.

| layer / method | Phase 7 base | Phase 8 | reduction | speedup |
|----------------|-------------:|--------:|----------:|--------:|
| targeted store build | 16.33 s | 10.56 s | 35.3% | 1.55x |
| `graph.stats` | 20.91 s | 9.88 s | 52.7% | 2.12x |
| `session.tool_usage` | 13.10 s | 6.18 s | 52.8% | 2.12x |

The optimized store retained the same 112 sessions, 213 turns, 19 284 items,
and 76 923 events. Canonical JSON output remained identical for the two hot
projections:

| method | canonical bytes | SHA-256 before and after |
|--------|----------------:|--------------------------|
| `graph.stats` | 1 325 598 | `659c7dcebf6f42e6979ae5754c5114d21e0a018a4f875d164f5f26919b2e97f8` |
| `session.tool_usage` | 12 496 335 | `86777deb9cdeabb504aa3178bb92b9af7071cec27852fb17a373706d5554f7ee` |

The ingestion changes were also checked through `session.overview` and
`graph.overview`; both canonical response hashes remained unchanged.

### Version 2 surface cleanup (Phase 9)

Version 2 makes diagnostic detail explicit instead of returning every nested
projection by default. Measured on the same 112-session graph, with minified
canonical JSON:

| method | v1 default | v2 default | reduction | warm median |
|--------|-----------:|-----------:|----------:|------------:|
| `graph.overview` | 1 636 444 B | 103 193 B | 93.7% | 0.023 s |
| `graph.stats` | 1 325 598 B | 282 897 B | 78.7% | 8.92 s |
| `graph.usage` | 577 547 B | 393 901 B | 31.8% | 0.95 s |
| `session.request_usage` | 5 872 614 B | 3 859 842 B | 34.3% | 0.42 s |
| `session.tool_usage` | 12 496 335 B | 6 127 572 B | 51.0% | 5.80 s |

The omitted projections remain available through typed `include` values. The
compact stats and tool-usage paths also avoid materializing the omitted
per-session category trees and all-item cost rows while retaining full primitive
allocation, request-tier pricing boundaries, and reconciliation assertions.

The public registry now has 13 versioned methods. The unreachable
`project.logfile` method and duplicate `session.turn_usage` projection were
removed; `session.usage` with `turn_id` is the authoritative replacement for the
latter. All graph totals use canonical `runtime` and `total_usage` fields rather
than duplicate `graph_*` aliases.

### Historical projection benchmark (Phases 1-7)

| method | before | after | speedup |
|--------|--------|-------|---------|
| `session.stats` | **157.0 s** | **4.6 s** | 34.1x |
| `session.tool_usage` | **38.8 s** | **14.5 s** | 2.7x |
| `session.items` {tool_call} | **21.9 s** | **0.25 s** | 88x |
| `graph.usage` | 1.45 s | 0.90 s | 1.6x |
| `session.request_usage` | 0.81 s | 0.69 s | 1.2x |
| `session.usage` | 0.64 s | 0.31 s | 2.1x |
| `session.overview` | 0.34 s | 0.33 s | - |
| `session.events` | 0.32 s | 0.11 s | 2.9x |
| `session.model_usage` | 0.31 s | 0.16 s | 1.9x |
| `graph.overview` | 0.06 s | 0.06 s | - |

### Historical store-build benchmark (before Phase 8)

| case | median |
|------|--------|
| targeted (warm cache) | 16.3 s |
| global (all logs) | 59.7 s |

## 4. Optimizations applied

### Phase 1: `_allocate_int` sort-key precompute (`metrics/analysis.py`)

Replaced `sorted(range(n), key=lambda i: (raw[i]-floors[i], weights[i]))` with
a precomputed `keys` list and `keys.__getitem__` (DSU pattern). Added
`if remainder > 0` guard to skip the sort entirely when no remainder.
**Eliminated 83.5M lambda calls.**

### Phase 2: `AllocatedRealTokenCost` -> `_CostAccum` dataclass (`metrics/analysis.py`)

Introduced a lightweight mutable `@dataclass(slots=True)` (`_CostAccum`) for
the hot allocation/accumulation loop. The pydantic `AllocatedRealTokenCost`
(with custom `model_serializer`) was instantiated ~34M times in `session.stats`
alone. `_CostAccum` is used internally and converted to pydantic only at output
boundaries via `_cost_accum_to_allocated_cost`. `_add_allocated_real_token_cost`
mutates in-place instead of creating new objects.

### Phase 3: `present_entries` bisect + `output_weights` precompute (`metrics/analysis.py`)

- **Bisect**: Sort entries by `started_at` once, then use `bisect.bisect_right`
  per observation instead of O(observations × entries) list comprehension scans.
  Applied to `build_session_graph_stats_token_usage`,
  `_build_item_real_token_costs_for_session`, and
  `_sum_allocated_usage_for_present_observations`.
- **output_weights precompute**: Pass `output_weights: list[int]` directly to
  `_allocate_real_token_costs_for_entries` instead of `output_entries: list`,
  eliminating UUID set creation (`{entry.item_id ...}`) and membership checks
  (`entry.item_id in output_entry_ids`). **Reduced UUID hashes from 99M to 78M.**

### Phase 4: Pricing fast path (`metrics/pricing.py`)

- **`_estimate_cost_from_ints`**: Takes 5 token ints directly, bypassing
  `TokenUsage.model_validate` (with `_normalize_compact_usage` before-validator),
  `CostBreakdown` creation, and `CostEstimate` creation. Computes the amount as
  a plain float.
- **`_cost_evidence_from_accum`**: Calls `_estimate_cost_from_ints` directly
  with `_CostAccum` fields, skipping the intermediate dict conversion via
  `_allocated_cost_usage_dict`.
- **`_normalize_model_name` memoized** with `@lru_cache(maxsize=256)`.
- **`any(genexpr)` replaced** with inline `or`-chain for the non-zero check.

### Phase 5: `build_item_details` lazy index (`analysis/item_details.py`)

`build_item_details` called `build_session_graph_index(session_graph)` for every
item, but only needs it for rare `PLAN_SUBAGENT`/`SESSION_HANDOFF` items. Moved
the index build inside those two branches. **Eliminated 5991 unnecessary index
builds (226M UUID hashes) per `session.items` call.**

### Phase 6: `_allocate_int_batch` sparse path + dict mutation (`metrics/analysis.py`)

- **Batch**: Replaced 6 separate `_allocate_int` calls with 2
  `_allocate_int_batch` calls (4 for `all_weights`, 2 for `output_weights`),
  sharing `sum(weights)` computation.
- **Sparse path**: When `output_weights` has zero-weight entries (87.5% zeros),
  only non-zero entries participate in the sort, reducing sort size ~8x.
- **Dict mutation**: Replaced `dict[key] = _add(dict.get(key), cost)` with an
  explicit `if existing is None: dict[key] = cost else: _add(existing, cost)`,
  avoiding the redundant `dict[key]=` reassignment (and its UUID hash) when the
  key already exists.

### Phase 7: NumPy-backed stats accumulation (`metrics/analysis.py`)

`build_session_graph_stats_token_usage` now accumulates the seven allocated
token fields in one `int64` array per session. Context-source rows are grouped
by key after allocation, at the output boundary, instead of constructing and
mutating one `_CostAccum` per entry for every observation. The allocation
function retains its list-based default for `session.tool_usage`; only stats
requests array results.

On the largest graph this reduced `session.stats` from the documented 11.8 s to
4.6 s (4.0 s on a second warm run), with the committed metric baselines
unchanged.

### Phase 8: request-wide reuse and allocation-boundary materialization

Phase 8 removed repeated work at three distinct boundaries while retaining the
then-current public response contracts. Phase 9 subsequently versions and
shrinks that surface as described above.

#### Targeted store construction

- Discovery still scans every candidate header, but scans started-turn ids only
  for sessions that are actually referenced as parents. On the retained graph,
  this reduced full-file parent scans from 112 files to 8.
- Graph edge construction now builds the item/event lookup once per parent
  session instead of once per child edge (111 builds became 8).
- Transcript projection maintains a set beside the current turn's ordered event
  id list, preserving list order while making duplicate checks constant-time.

#### Graph stats

- The graph attribution pass retains each session's local allocation before it
  merges graph totals. Per-session sections reuse that allocation when the
  session and graph resolve to the same tokenizer.
- Sessions with a different tokenizer still run their original isolated
  attribution pass. This guard is required for mixed `cl100k_base` /
  `o200k_base` graphs, where reusing graph-scoped weights would change integer
  allocation.
- A request-scoped visible-content cache reuses exact token counts across graph
  attribution, context composition, and per-session presentation. Its key
  includes the effective tokenizer and any provider-reported token override,
  and the cache is discarded when the request finishes.

#### Tool-usage attribution

- Per-observation allocation now accumulates seven integer token fields in a
  NumPy array indexed by item instead of constructing and merging a Python
  dataclass for every observation/item slice.
- Per-slice price results accumulate as primitive floats and dates. One
  `CostEvidenceFlat` Pydantic model is materialized per final item instead of
  one per slice (about 2.4 million temporary models on this graph).
- Request-level pricing-tier selection, component and slice rounding, ordered
  final summation, missing-price behavior, and effective-date aggregation stay
  at their original boundaries.

## 5. Remaining bottlenecks

`graph.stats` is now dominated by visible-content entry construction and the
remaining integer allocation passes. Mixed-tokenizer sessions deliberately
retain their isolated attribution fallback for correctness.

`session.tool_usage` still performs request-tier price arithmetic for roughly
2.4 million observation/item slices. That arithmetic cannot be moved after
aggregation because pricing thresholds and rounding are request-specific; the
temporary Pydantic evidence construction around it has been removed.

Targeted store construction still parses and validates the session files that
belong to the selected graph. A persistent canonical-session cache could remove
more work, but it needs explicit file fingerprints, schema invalidation, and
parent-trimming dependency handling before it is safe.

## 6. Reproducing

```bash
# metrics quality gate (correctness)
uv run python scripts/validate-metrics-baselines.py

# store-build layer
uv run python scripts/benchmark-query.py --no-projection

# projection layer (per-method; slow methods individually)
uv run python scripts/benchmark-query.py --no-store-build \
    --graph-id 019f6e1a-b402-7e61-985d-dde827545977 \
    --methods session.stats

# cProfile a method
uv run python scripts/benchmark-query.py --no-store-build \
    --graph-id <uuid> --methods session.tool_usage --profile session.tool_usage
```

The original `benchmarks/results/query-baseline.json` is retained in Git history;
current runs write `.artifacts/benchmarks/query-baseline.json`.
