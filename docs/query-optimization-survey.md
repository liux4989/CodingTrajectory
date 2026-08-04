# Query Optimization Survey & Benchmark

Survey, baseline measurement, profiling, and optimization of the
`@packages/core` query API.

## 1. The exposed API

The public query surface is the JSON-RPC-style **`ServiceRuntime`**
(`packages/core/src/coding_trajectory/runtime.py`) exposing 15 methods via
`runtime.call(method, params)` / `batch(requests)`:

| group | methods |
|-------|---------|
| collection | `project.list`, `project.sessions`, `project.logfile` |
| overview | `session.overview`, `graph.overview` |
| usage | `session.usage`, `graph.usage`, `session.turn_usage`, `session.model_usage`, `session.request_usage`, `session.tool_usage` |
| stats | `session.stats`, `graph.stats` |
| detail | `session.events`, `session.items` |

### Query path (per call)

```
ServiceRuntime.call
  └─ contract.validate_request
  └─ (short-circuit) project.list / project.sessions[no include]
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
optionally cProfiles named methods. Writes `benchmarks/results/query-baseline.json`.

## 3. Results

Target: largest graph on disk (`019f6e1a…`, 112 sessions / 213 turns /
19 284 items / 76 923 events).

### Projection (warm store, single dispatch) - before vs after

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

### Store build (unchanged)

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

## 5. Remaining bottlenecks

`session.stats` is now dominated by `_allocate_int_batch` (~2.8 s under
cProfile, 6,005 observations) and the remaining projection/output work. The
19.4M per-element Python accumulation iterations and their `_CostAccum`
construction have been removed.

`session.tool_usage` at ~15s remains dominated by `_cost_evidence_from_accum`
(2.4M calls), specifically per-item cost arithmetic and evidence construction.
Model pricing rules are now resolved through a projection-scoped cache; live
catalog lookup is no longer repeated for each item.

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

Baseline: `benchmarks/results/query-baseline.json`.
