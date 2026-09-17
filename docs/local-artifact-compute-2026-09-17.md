# Local artifact compute pre-benchmark, 2026-09-17

**Reuse the existing parser, typed facts and metrics; evaluate per-graph preparation
and snapshot-level reader reuse before adopting an immutable-artifact design.**
Moving the existing whole-corpus pipeline locally is not enough to make incremental
updates cheap. No architecture, runtime, cloud storage or publication changes are
implemented here. All data are synthetic; all execution is offline.

## Frozen source and reproducible inputs

- Measured baseline and detached HEAD:
  `52dfc9dcead986d1da8b49e4bde204860c4cce87` (prior benchmark commit).
  Original contracted origin baseline: `1d9bc2851ee7667969164016d36a504944bf9362`.
  Intervening `08308bbadf291225f994c759cf6acefd5020ce4c` only changes CLI plugin
  dispatch; `52dfc9d` adds benchmark evidence. Core/Cloudflare runtime sources were
  identical to the contracted baseline at the start of this phase.
- Another session advanced shared local `main` during an initial run, through
  `45eb8f0` (local fact history) and `9f8821536f074a9ea46f20a5fa8fb31540297e9d`
  (discovery, metrics history and living queries). Those commits were preserved.
  **Mixed-source results were discarded.** The complete final matrix ran in an
  isolated detached baseline worktree. The harness now checks core source equality
  to the baseline before and after every child. These measurements do not qualify
  those newer changes.
- Apple M4, 16 GiB RAM, arm64, Darwin 27.2; Python 3.12.11, Clang 20.1.4.
  Existing uv environment, no dependency installation. `uv.lock` SHA-256:
  `2d958d9f82e7c6d8900f155dce882735bc1b845121e0eec6217d32a00c148abe`.
- Aggregate tracked source SHA-256 (core tree plus existing qualification helper):
  `eb4ceeb5cfb72957ea8a1a89b0f19ef6e6f7a3c76de6451a81957db57d935080`.
  Harness SHA-256: `bc4aad5e834e9dcb47281599f1d4bf852896c9334ee14a80c0ce7dd7df4e8085`.
  [Raw aggregate results](local-artifact-compute-2026-09-17.json) record every
  repeat, input hash, artifact hash, stage wall/CPU time and peak RSS.
- Exact reviewed pilot remains unavailable; expected private export SHA-256 is
  `bb64366e3c4c4a4cb7c078fd236bcf7c1d5e4f162c1647732114ae18632853c0`.
  The 29,087-fact fixture below is **not** the 29,681-fact production export.

The harness creates supported Amp JSONL, not prebuilt facts: user/assistant messages,
agent start/end observations, shell call/result, deterministic timestamps and file
mtimes. `CT_AMP_LOG_DIR` confines discovery to disposable logs. Fixture generation
is excluded from timings. Input hashes cover sorted filename/content-hash/size
tuples, encoded with the repository's canonical JSON function:

| Input | Graphs × turns | Input bytes | Input SHA-256 |
| --- | --- | ---: | --- |
| Pilot scale / unchanged | 29 × 100 | 4,391,296 | `586397c29d8277bda706803e529168685e9552ba559b4ee111178337e6707e9b` |
| Appended turn | 29; one extra turn | 4,392,816 | `20e7012cf28f451dd83a5f4d07af00fa086e3933838ec9d486d7f0e215fd3292` |
| Changed graph | 29 × 100 | 4,391,297 | `e2ece2765e1cc941f17f19808c8bdae453202fc1bad182f973e95fe41120c479` |
| 2× | 58 × 100 | 8,782,592 | `80427e9ce7234f0f94a15aeea5ff39f73660c69914e3f88fe88b578c1336270f` |
| 4× | 116 × 100 | 17,565,184 | `f3a3965974587ad936c79133a957cc9fa271d6ae9fe7b391bc0061f5146b3337` |
| Few large | 4 × 725 | 4,405,696 | `e7adb917fd98e5e34a6a8a90e0b01156e3ab8692a9ccd43fd95b023d4170b2f6` |
| Byte-heavy logs | 29 × 100 | 27,776,896 | `1a1545414109495a94ea53eb376ae0d434aad5c0733eb6f3e11cfc3e46dead5b` |

Existing `canonical_event_id` includes the absolute input path, and ingestion uses
it to derive further identifiers. Independent temporary roots therefore change
artifact hashes despite identical input-byte hashes. Exact canonical equality is
asserted within each run and unchanged rerun; cross-invocation artifact hashes
are evidence, not fixed goldens. The harness does not override runtime identity.

## Existing collection rebuilds unchanged data

Three sequential fresh-process repeats per case; seconds are median [min–max].
CPU uses `process_time`; RSS is maximum `ru_maxrss` across the three fresh children,
in decimal MB. Collector RSS is captured immediately after `collect` and close,
before the harness's artifact/view preparation. Imports count toward RSS, but not
the collector stage timer; full subprocess elapsed times are also in the JSON.

| Case | Facts projected | Collector wall seconds | CPU median seconds | Collector peak MB |
| --- | ---: | --- | ---: | ---: |
| Pilot cold | 29,087 | **4.428 [4.376–4.451]** | 4.417 | 434.8 |
| Pilot unchanged | 29,087 | **2.588 [2.587–2.591]** | 2.588 | 271.1 |
| One appended turn | 29,097 | 4.482 [4.467–4.483] | 4.468 | 434.8 |
| One changed graph | 29,087 | 4.474 [4.466–4.489] | 4.460 | 434.8 |
| 2× cold | 58,174 | 9.381 [9.369–9.401] | 9.365 | 773.6 |
| 4× cold | 116,348 | **19.902 [19.874–19.924]** | 19.872 | 1,450.4 |
| Few large cold | 29,012 | 4.699 [4.687–4.713] | 4.690 | 461.4 |
| Byte-heavy logs cold | 29,087 | 5.334 [5.323–5.355] | 5.324 | 476.7 |

Pilot cold breakdown: discovery **2.1 ms**, parsing/normalization **395 ms**, graph
assembly **0.2 ms**, fact projection **2.023 s**, outbox/client flush **1.717 s**.
Projection includes **1.665 s** of typed fact assembly, hashing and validation;
do not add those inclusive times together. At 4×, parsing is **1.978 s**, projection
**8.639 s**, and flush **7.930 s**. GC stays enabled and can move allocation costs
between adjacent stages; do not extrapolate a micro-stage from a single timing.

Every pilot rerun reparses 29 sessions and projects all 29 graphs. Unchanged input
stages zero facts, makes no publication, and produces identical views. An appended
turn adds ten facts; changing one exit code and assistant message alters exactly
one graph digest. Neither case avoids rebuilding the other 28 graphs.

The accepting offline sink exercises actual collector preparation, durable outbox,
client validation and request serialization, but reports all batches missing. It
stages all facts on changed publications. A real server can reuse batches: these
are **not measurements of remote retry traffic, SQL writes or network latency**.
No server validation is performed by the sink; socket connects are rejected.

"Cold" means a fresh process and empty collector database, not evicted OS caches.
Unchanged/appended/changed cases use independent copies of the same accepted cold
outbox and retain source inode identity. Reader cold starts in another fresh
process; its warm pass shares the same `FactIndex` and `IndexCache`. No competing
heavy verification was launched during the final matrix, but this is not a
dedicated-host, cache-flushed or statistically randomized experiment.

## Reader validation and repeated indexing dominate serialization

The reader consumes actual collector-produced facts, then dispatches `graph.stats`,
`session.summary`, and `session.overview(limit=10)` for **every graph**, twice.
These are aggregate batch costs, not isolated single-screen interaction timings.

| Cold shape | Artifact MB | JSON decode s | Fact validation s [min–max] | FactIndex s | Repeated entrypoint indexing s | Reader full peak MB |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| Pilot | 17.339 | 0.046 | **1.062 [1.061–1.066]** | 0.163 | **0.445** | 368.5 |
| 2× | 34.677 | 0.095 | 2.438 [2.429–2.487] | 0.353 | **2.081** | 613.9 |
| 4× | 69.354 | 0.200 | **5.677 [5.673–5.689]** | 0.762 | **9.043** | 1,126.2 |
| Few large | 17.336 | 0.045 | 1.194 [1.192–1.201] | 0.169 | 0.073 | 377.8 |
| Byte-heavy logs | 17.356 | 0.046 | 1.072 [1.064–1.095] | 0.163 | 0.455 | 365.6 |

Repeated indexing is the sum across six calls per graph (cold + warm, three APIs).
`dispatch` calls `IndexCache.index_facts` on every request; that method iterates all
canonical facts. Its total work is proportional to graph count × corpus size for
this all-graph matrix. The 4-graph shape has roughly the same facts as pilot but
far less indexing work. Reconstructing graphs across both passes separately costs
**1.718 / 3.882 / 6.683 s** at 1×/2×/4×. Sharing `IndexCache` does not cache those
materialized graphs. Warm calls consequently remain substantial.

Pilot reader cold totals per API: stats **0.539 s**, summary **0.426 s**, overview
**0.247 s**. Warm: **0.428 / 0.435 / 0.532 s**. At 4× cold:
**3.220 / 2.970 / 2.213 s**. Timings include request/response validation and
canonical response encoding. They include nested reconstruction/indexing, not
additional independent costs.

Producer-side pilot view preparation is **0.541 / 0.284 / 0.244 s**, producing
**83,027 / 143,521 / 168,374 bytes** across the three APIs. The harness first reads
and revalidates the persisted outbox (**0.063 + 1.159 s**), then builds a FactIndex
(**0.206 s**); this extra bridge is an experiment, not a proposed writer requirement.
Canonical artifact model dump / JSON encoding / SHA-256 costs **52 / 65 / 5 ms**.
At 4× JSON encoding + hashing is still only **258 + 20 ms**.

Optional gzip level 1 reduces the pilot artifact to **3,924,325 bytes** in **57 ms**;
level 6 produces **3,490,597 bytes** in **150 ms**. Decode takes **10–12 ms**.
At 4× these become **15,692,149 / 13,952,602 bytes**, encoding **229 / 618 ms**.
Compression is not the measured bottleneck. Its gains depend on these repetitive
synthetic facts; no R2 upload/download or storage-class decision is exercised.

The byte-heavy case increases raw tool output from 128 to 8,192 bytes per turn.
Privacy projection keeps fact bytes almost unchanged. This measures parsing and
projection of large logs, **not large retained measurement payloads or media**.

Full measured collector-process work, including post-collection artifact/views,
takes **7.062 / 16.175 / 35.922 s** (CPU **7.049 / 16.156 / 35.885 s**) at 1×/2×/4×;
full reader work takes **4.249 / 10.427 / 26.126 s**, with almost identical CPU.
Reader totals include both view passes, roundtrip verification and both compression
experiments. Collector full peaks are **491.5 / 877.5 / 1,654.9 MB**; reader peaks
also include verification and compression allocations. Do not call those totals
the latency or memory requirement of a future artifact-serving architecture.

## Validation coverage and the next decision

All 24 producer/reader pairs passed: original projection digests equal decoded
outbox digests; Pydantic model dumps preserve values; canonical bytes round-trip
exactly; producer/reader and cold/warm API digests and byte counts agree; gzip
decoding restores exact bytes; unchanged output matches cold; changed inputs
alter exactly one graph. These compare the same existing semantics, not a new
independent metrics implementation. Existing direct-facts, collector preparation,
Amp live/replay and all four committed metric baselines also passed on the frozen
source. Synthetic logs do not cover rich provider token/cost evidence, multimodal
payloads, deep delegation or every vendor's parser. No unit tests were added.

Existing local `PublishedFactSet` validation already performs row/fact-set hashes,
relationship/order/cardinality integrity, publication bounds and privacy-related
schema/projection restrictions. Those costs are included. Moving server-authority
checks for global ownership, overlap, source fences or publication sequencing
locally is **proposed work not exercised**. Artifact authentication, schema-version
compatibility, manifest atomicity, retention, recovery, R2 transport and reader
trust decisions are also unmeasured. This is not permission to remove cloud checks.

The tested offline batch envelope reaches **116,348 facts / 69.35 MB**, with a
**19.9 s / 1.45 GB** collector pipeline on this machine. It is not a maximum or a
safe cloud envelope. Whole-corpus cold reads require seconds of validation before
views; an unchanged collection still costs 2.59 seconds. No interactive acceptance
threshold was supplied, and individual on-demand graph loads were not timed.

**Next gate:** benchmark a separately approved per-graph preparation boundary and
per-snapshot index/materialized-view reuse against this baseline, using the same
parity checks and measuring invalidation after one changed graph. First target
`_queue_fact_publication`'s unchanged re-projection and `dispatch`/`index_facts`'s
repeated corpus scan, not compression or a new parser. Avoid requiring a full
corpus decode/validation for every detail request. Include newly relocated
authority checks and actual per-graph artifact loading before selecting the
architecture. No speedup for that unimplemented design is claimed.

## Reproduction

Run from the main checkout with the existing environment. The detached worktree
ensures a concurrently changing main cannot contaminate attribution:

```sh
ROOT="$PWD"
git worktree add --detach .artifacts/local-compute-baseline 52dfc9dcead986d1da8b49e4bde204860c4cce87
cp scripts/benchmark-local-artifact-compute.py .artifacts/local-compute-baseline/scripts/
(
  cd .artifacts/local-compute-baseline
  export UV_PROJECT_ENVIRONMENT="$ROOT/.venv" PYTHONPATH=packages/core/src
  uv run --no-sync python scripts/benchmark-local-artifact-compute.py \
    --output "$ROOT/docs/local-artifact-compute-2026-09-17.json"
  uv run --no-sync python scripts/qualify-direct-published-facts.py
  uv run --no-sync python scripts/qualify-collector-preparation.py
  uv run --no-sync python scripts/validate-amp-live.py
  uv run python scripts/validate-metrics-baselines.py
)
uv run --no-sync ruff check scripts/benchmark-local-artifact-compute.py
uv run --no-sync ruff format --check scripts/benchmark-local-artifact-compute.py
scripts/check-metrics-quality-gate.sh
git diff --check
```

`--smoke --repeats 1` substitutes two three-turn graphs for fast harness validation.
Default is three repeats. Temporary fixtures/outboxes are deleted automatically;
remove the copied harness and generated `.artifacts` from the disposable worktree,
then `git worktree remove .artifacts/local-compute-baseline` after review. The main
quality-gate path detector may also include other sessions' commits; the full
baseline workflow above runs directly regardless. No expected metrics were updated.
