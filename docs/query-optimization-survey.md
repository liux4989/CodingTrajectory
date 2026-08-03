# Query Optimization Survey & Benchmark

Baseline measurement of the `@packages/core` query API, profiling the slowest
methods, and ranked optimization targets. No code changes yet — this is the
survey + benchmark phase.

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
1. **Store build** — discovery + ingestion (`resolve_store` → `DocumentStore`).
   Pydantic `model_validate` of every jsonl line. Amortized across calls with
   the same discovery params (cached by `_store_key`).
2. **Projection** — handler logic over a warm `DocumentStore`.

## 2. Benchmark harness

`scripts/benchmark-query.py` — isolates both layers, repeats measurements, and
optionally cProfiles named methods. Writes `benchmarks/results/query-baseline.json`.

```
uv run python scripts/benchmark-query.py --no-store-build --graph-id <uuid> --methods session.stats
uv run python scripts/benchmark-query.py --profile session.tool_usage
```

## 3. Baseline results

Target: largest graph on disk (`019f6e1a…`, 112 sessions / 213 turns /
19 284 items / 76 923 events). Single repetition.

### Layer 1 — store build (discovery + ingestion)

| case | median | store |
|------|--------|-------|
| targeted (warm cache) | 16.3 s | g=1 s=112 t=213 i=19284 e=76923 |
| global (all logs) | 59.7 s | g=718 s=1189 i=146808 e=457278 |

### Layer 2 — projection (warm store, single dispatch)

| method | median | resp |
|--------|--------|------|
| `session.stats` | **157.0 s** | 30 KB |
| `session.tool_usage` | **38.8 s** | 13 MB |
| `session.items` {tool_call} | **21.9 s** | 4.9 MB |
| `graph.usage` | 1.45 s | 0.1 KB |
| `session.request_usage` | 0.81 s | 6.2 MB |
| `session.usage` | 0.64 s | 0.1 KB |
| `session.overview` | 0.34 s | 0.1 KB |
| `session.events` | 0.32 s | 11 MB |
| `session.model_usage` | 0.31 s | 0.1 KB |
| `graph.overview` | 0.06 s | 0.1 KB |

`session.stats`/`graph.stats` and `session.tool_usage` are 2–3 orders of
magnitude slower than the rest. `session.items` is the next outlier.

## 4. Profiling the outliers

### `session.stats` → `build_session_graph_stats_token_usage` (analysis.py:937)

cProfile (partial, 170 s alarm). The two sub-builds split cleanly:
`build_session_graph_stats_token_usage` = **157 s**; `build_session_graph_context_stats` = 0.37 s.

Top cumulative:

| function | cumtime | ncalls |
|----------|---------|--------|
| `_allocate_real_token_costs_for_entries` (analysis.py:1506) | 124.6 s | 5 606 |
| `_allocate_int` (analysis.py:1590) | 68.7 s | 33 636 |
| `builtins.sorted` | 63.0 s | 27 699 |
| `<lambda>` sort-key (analysis.py:1604) | 51.9 s self | **83 555 366** |
| pydantic `BaseModel.__init__` | 51.3 s | **33 895 988** |
| `_add_allocated_real_token_cost` (analysis.py:1315) | 19.5 s | 16 938 942 |

**Root causes:**
- **O(observations × entries) filter** — for every usage observation the full
  `entries` list is rescanned to rebuild `present_entries`
  (analysis.py:957). Entries are time-ordered; this should be a single sort +
  per-observation prefix/bisect.
- **`AllocatedRealTokenCost` is a pydantic `BaseModel`** (models.py:718,
  with a custom `model_serializer`) instantiated **~33.9 M times** in the hot
  allocation loop. A frozen dataclass / namedtuple would remove ~50 s.
- **`_allocate_int` sorts per allocation** — `sorted(range(n), key=lambda…)`
  runs 6× per `_allocate_real_token_costs_for_entries` call; the lambda fires
  83.5 M times. The sort key can be precomputed once.

### `session.tool_usage` → `_build_item_real_token_costs_for_session` (analysis.py:1119)

cProfile (82 s under instrumentation):

| function | cumtime | ncalls |
|----------|---------|--------|
| `_build_item_real_token_costs_for_session` | 79.5 s | 1 |
| `cost_evidence_from_usage` (pricing.py:188) | 46.7 s | **2 431 057** |
| `estimate_cost` (pricing.py:315) | 38.1 s | 2 430 769 |
| pydantic `BaseModel.__init__` | 12.6 s | 12 274 931 |
| `uuid.__eq__` | 5.2 s self | **58 046 072** |

**Root causes:**
- Same **O(observations × entries)** `present_entries` filter (analysis.py:1131).
- **`cost_evidence_from_usage`/`estimate_cost` called 2.43 M times** — once per
  observation per item, each building a `CostEvidenceFlat` pydantic object and
  doing a price-rule lookup. Memoize by `(model, provider, usage-bucket)` or
  batch.
- **58 M `uuid.__eq__`** — UUID list-membership / repeated equality in a hot
  path where a `set`/`dict` keying would do.

## 5. Optimization targets (ranked)

| # | target | methods | est. impact |
|---|--------|---------|-------------|
| 1 | `AllocatedRealTokenCost` → frozen dataclass/namedtuple | stats, tool_usage | ~50 s on stats |
| 2 | `present_entries` O(n²) filter → one sort + bisect/prefix per observation | stats, tool_usage | removes 33.9 M / 12.3 M redundant iterations |
| 3 | `_allocate_int` precompute sort keys (drop per-call lambda) | stats, tool_usage | ~50 s (83.5 M lambda calls) |
| 4 | `cost_evidence_from_usage` memoization / batch pricing | tool_usage | ~47 s |
| 5 | UUID list-membership → set/dict | tool_usage | ~5 s (58 M `__eq__`) |
| 6 | ingestion `model_validate` cost (store build 16 s / 60 s) | all (store layer) | store-build latency |

Targets 1–3 alone should bring `session.stats` from ~157 s into the low
single-digit seconds on this graph; 1–2 + 4–5 should do the same for
`session.tool_usage` (~39 s → low seconds).

## 6. Reproducing

```bash
# store-build layer
uv run python scripts/benchmark-query.py --no-projection

# projection layer (per-method; slow methods individually)
uv run python scripts/benchmark-query.py --no-store-build \
    --graph-id 019f6e1a-b402-7e61-985d-dde827545977 \
    --methods session.stats

# cProfile a method (re-runs it under instrumentation)
uv run python scripts/benchmark-query.py --no-store-build \
    --graph-id <uuid> --methods session.tool_usage --profile session.tool_usage
```

Baseline: `benchmarks/results/query-baseline.json`.
