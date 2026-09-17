# Per-graph preparation and snapshot reuse experiment, 2026-09-17

**The tested reuse boundary preserves outputs and substantially reduces incremental
work.** It does not make full builds cheap: candidate cold preparation is 3.55 s
at pilot scale and 17.38 s at 4×. Incremental preparation still parses the whole
inventory; selected-graph readers avoid loading the whole fact corpus.

This is disposable benchmark tooling, not a production cache or publication
implementation. No runtime edits, cloud requests, R2 migration, new unit tests,
deployments or pushes occurred. [Aggregate evidence](graph-reuse-benchmark-2026-09-17.json)
contains all 36 comparisons, per-stage wall/CPU, fresh-process peaks, input hashes,
response hashes, persistence bytes and recomputation counters.

## Attribution and the exact comparison

- Clean local main at start, pinned detached checkout and measured HEAD:
  `368d21be8330163d53ea6884f289205b3ae12226`.
- This pin includes the independently created local-history/discovery changes
  `45eb8f0` and `9f88215`, preserved without edits. It differs from the previous
  compute experiment's `52dfc9d` runtime and original contracted origin baseline
  `1d9bc2851ee7667969164016d36a504944bf9362`. **Compare baseline/candidate within
  this report, not their times against the earlier collector benchmark.**
- Apple M4, 16 GiB RAM, arm64 macOS 27.2; Python 3.12.11 / Clang 20.1.4, existing
  uv environment. No dependencies installed. `uv.lock` SHA-256 remains
  `2d958d9f82e7c6d8900f155dce882735bc1b845121e0eec6217d32a00c148abe`.
- Tracked core source aggregate SHA-256:
  `e4c09b0a3675ded1a9bc59c6b43e39c16a7238ca7f422bd9cc5cbe7e52f5a4ff`.
  Harness SHA-256: `b2f90674d7ecbe9dde47fd1fc4fcab7796bd0b6b2e4ab88700b5087ed8edfa9e`.
  Reused fixture/measurement harness SHA-256:
  `bc4aad5e834e9dcb47281599f1d4bf852896c9334ee14a80c0ce7dd7df4e8085`.
- Core equality to the pin is checked before and after every child subprocess.
  Fixtures, independent seed-cache copies and subprocess imports are outside stage
  timings. Full subprocess elapsed time including imports is also recorded.

Both preparation paths call actual `discover_store_from_files` with trajectory
retention, reparse every present log, assemble every connected component, hash the
complete canonical graph, and use existing fact/metric/service implementations.
They persist identical content-addressed fact artifacts, `graph.stats` and
`session.summary` outputs, and a snapshot manifest. Both skip writing files that
already exist. **Neither runs collector fencing, outbox flushing, remote checks,
authentication or a network publication.** This isolates preparation/reuse costs.

Baseline always projects every graph, builds a corpus-wide FactIndex, and computes
summaries through existing dispatch. Candidate reuses a previous manifest entry
only when graph ID and full graph-content hash match under the pinned contract.
Changed/new graphs are projected independently; their summary computation indexes
each FactIndex once and memoizes graph reconstruction. All currently discovered
roots define the new manifest; missing roots are not copied forward.

Reader baseline validates/indexes all facts once per distinct snapshot, but uses
ordinary dispatch for each query. Candidate reads persisted summaries with response
validation and lazily validates/indexes only selected graph artifacts for detail.
Graph reconstruction is memoized by FactIndex identity plus graph ID; indexes and
summaries are retained by immutable artifact hash, not mutable root ID. Old indexes
stay alive, preventing identity reuse from aliasing another snapshot. The prototype
uses process-local wrappers/subclasses solely inside benchmark subprocesses.

## Inputs and invalidation probes

The same supported synthetic Amp logs as the previous phase are used: 29 or 116
independent 100-turn sessions, yielding 29,087 or 116,348 facts. No private pilot
export is available. Initial input-tree hashes match the previous experiment:

| Input | SHA-256 |
| --- | --- |
| Pilot cold/unchanged/split | `586397c29d8277bda706803e529168685e9552ba559b4ee111178337e6707e9b` |
| Pilot append | `20e7012cf28f451dd83a5f4d07af00fa086e3933838ec9d486d7f0e215fd3292` |
| Pilot changed | `e2ece2765e1cc941f17f19808c8bdae453202fc1bad182f973e95fe41120c479` |
| Pilot delete | `9870327d5bc93db4ab4c5665f671008d94930b3360dbe322bb7803076659a326` |
| Pilot merge | `f63b4070e42d9d27edaf3b80a3c64bd8b1b30ebf3ef06dd666e3ada063cbab50` |
| Empty | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` |
| 4× cold/unchanged | `f3a3965974587ad936c79133a957cc9fa271d6ae9fe7b391bc0061f5146b3337` |
| 4× append | `0453e38a2cc3387c1c1ca689bbb123372f977ef5532426f2f9962bd6d7ea9c3e` |
| 4× changed | `d3bb3a08355c5886c8aee5ed4c453d375f1940bd938681568db1e62f071f306c` |

Hashes use the previous sorted filename/content-hash/size scheme. Event IDs still
depend on absolute source paths, so independently generated temporary roots change
artifact hashes; baseline and candidate share identical paths within each run.

Append adds one turn (+10 facts). Changed replaces the last shell exit code with
23 and changes the assistant message. Delete removes the selected independent root.
Merge replaces a shell call/result with supported `create_thread` evidence linking
root 0 to captured child 1. Split removes that link, starting from the merged seed.
Empty deletes every input. **All file mtimes stay fixed**, so a timestamp-only
invalidation scheme would fail these content-change probes.

Each case/repeat starts from an independent copied cold cache, except split, which
starts from the merged cache. Copying is fixture setup, not a proposed cache update.
Three repeats, sequential baseline then candidate, fresh preparation and reader
processes. OS caches are not evicted; CPU/thermal conditions and order are not
randomized. No concurrent heavy validation was launched during measurement.

## Preparation: incremental gains exceed cold gains

Wall seconds: median [min–max]. CPU columns are medians; RSS columns are maximum
fresh-process `ru_maxrss` across repeats, decimal MB, including imports and Python
allocations. Total stages include object cleanup after preparation returns.

| Case | Baseline wall s | Candidate wall s | CPU s baseline → candidate | Peak MB baseline → candidate | Graphs projected baseline → candidate |
| --- | --- | --- | --- | --- | --- |
| Pilot cold | 4.336 [4.267–4.389] | 3.550 [3.521–3.555] | 4.331 → 3.540 | 264.5 → 155.8 | 29 → 29 |
| Pilot unchanged | 4.250 [4.218–4.256] | **0.929 [0.923–0.932]** | 4.249 → 0.929 | 264.4 → 104.4 | 29 → 0 |
| Pilot append | 4.243 [4.234–4.250] | **1.148 [1.139–1.158]** | 4.242 → 1.147 | 264.6 → 151.5 | 29 → 1 |
| Pilot changed | 4.255 [4.252–4.265] | **1.132 [1.130–1.149]** | 4.254 → 1.131 | 265.0 → 151.4 | 29 → 1 |
| 4× cold | 22.220 [22.147–22.297] | 17.381 [17.063–17.678] | 22.185 → 17.346 | 762.1 → 293.6 | 116 → 116 |
| 4× unchanged | 22.164 [21.932–22.296] | **4.317 [4.298–4.338]** | 22.161 → 4.317 | 763.8 → 254.9 | 116 → 0 |
| 4× append | 22.167 [21.914–22.218] | **4.614 [4.501–4.688]** | 22.164 → 4.613 | 762.3 → 287.9 | 116 → 1 |
| 4× changed | 21.147 [21.108–21.400] | **4.456 [4.431–4.477]** | 21.146 → 4.455 | 762.1 → 287.8 | 116 → 1 |

An appended graph projects 1,013 rows and a changed graph 1,003, versus the full
29,097/29,087 or 116,358/116,348. Candidate unchanged preparation spends **0.845 s
parsing/assembling + 0.075 s hashing** at pilot scale, and **3.986 + 0.302 s** at 4×.
Those costs form its remaining floor. Cold 4× fact projection still costs **9.878 s**;
summary preparation drops from **6.799 to 2.173 s**. GC stays enabled and can charge
allocation cleanup to adjacent stages; nested times should not be added to totals.

Pilot delete: **4.099 → 0.920 s**, 28 → 0 graphs projected. Merge:
**4.387 → 1.206 s**, 28 → 1 (2,006 rows) projected. Split:
**4.263 → 1.204 s**, 29 → 2 (2,006 rows) projected. Empty preparation is about
**0.6 ms** for either path, excluding imports. These are bounded topology probes,
not coverage of every vendor's delegation/identity semantics.

## Persistence and selected reads are included

Files receive `flush` + `fsync` when first written. This is local file-write cost,
**not crash-safe atomic publication**: no directory fsync, lock, CAS/current pointer,
interrupted-write recovery or concurrent writer protocol is implemented.

- Pilot cold writes **17,575,574 bytes**, including a **9,433-byte manifest**.
  Candidate artifact persistence is **21.4 ms**, versus baseline **15.8 ms**.
  4× writes **70,302,098 bytes**, manifest **37,534 bytes**; candidate persistence
  is **81.6 ms**. Manifest writes add about **0.2–0.3 ms**.
- Seed manifest loading is about **0.1 ms**. Unchanged writes zero bytes.
  Pilot append writes **621,131 bytes**, changed **615,165 bytes**; 4× adds the
  larger manifest, giving **649,232 / 643,266 bytes**. Both variants write the
  same bytes: gains come from skipping computation, not avoiding redundant writes.
- Delete writes only **9,110 bytes** of manifest; merge writes **1,217,755 bytes**.
  Split writes zero: its original artifacts/manifest still exist in the seed.
  It nevertheless recomputes two graphs because preparation consults only the
  previous manifest, not a historical graph-input-to-artifact index.

Reader sequence is **old snapshot cold → current first → current warm → old again**.
Each step queries roots 0, 1, and last with stats, summary and overview(limit=10):
**nine API responses**, not one query. Cold/unchanged current equals old; other
cases change the snapshot. All timings include load, hash verification, necessary
Pydantic validation/indexing, dispatch and response digest encoding.

| Selected-read phase | Baseline median ms | Candidate median ms | Candidate range ms |
| --- | ---: | ---: | --- |
| Pilot cold, three roots | 1,525.3 | **138.9** | 136.0–139.3 |
| 4× cold, three roots | 6,295.7 | **138.1** | 137.6–140.1 |
| Pilot current after append | 1,754.9 | **46.2** | 46.2–46.5 |
| Pilot current after change | 1,753.5 | **44.6** | 44.5–45.0 |
| 4× current after append | 7,954.2 | **46.2** | 45.0–47.3 |
| 4× current after change | 7,592.2 | **44.9** | 44.5–45.3 |
| Pilot warm after change | 92.6 | **3.2** | 3.1–3.3 |
| 4× warm after change | 183.7 | **3.2** | 3.1–3.2 |

Reader CPU is approximately equal to wall time (individual CPU measurements in
JSON). Candidate cold loads **1,826,620 / 1,854,721 bytes** at pilot/4×, validates
**3,009 rows**, and reconstructs three graphs. Baseline loads **17,348,011 /
69,391,846 bytes**, validates the entire corpus and reconstructs nine graphs.
After one changed graph, candidate loads **615,165 / 643,266 bytes**, validates
**1,003 rows** and reconstructs one graph. Warm and old-again candidate steps load
zero bytes, validate zero rows and reconstruct zero graphs; detail dispatch and
response serialization still run.

Fresh reader peak RSS, including retained old/current snapshots: pilot cold
**233.0 → 71.5 MB**, pilot changed **352.5 → 77.5 MB**, 4× cold
**610.2 → 71.6 MB**, 4× changed **1,110.2 → 77.5 MB**. This workload selects three
small graphs; loading every graph, huge single graphs or retaining many snapshots
would grow candidate memory. No eviction/capacity claim follows from these peaks.

## Correctness gates passed; scope remains deliberately narrow

All **36 baseline/candidate comparisons** passed. Entire manifest hashes match,
covering each graph's fact/summary content hashes, input digest and counts.
Selected-detail response hashes match ordinary full-corpus dispatch.
Unchanged responses equal old; every mutation changes the selected response set.
Old-snapshot responses remain identical after current queries. Warm candidate work
counters are explicitly zero. Expected projection counts distinguish stale reuse
from full invalidation: 0 unchanged/deleted, 1 append/change/merge, 2 split.

The root catalog excludes deleted/merged-away roots; querying such a root returns
an explicit missing sentinel in both benchmark readers. The merged root's facts
and summaries include the captured child. **Child-session/turn/item alias routing
into that graph is not implemented by the artifact reader**; this is graph-root
selection only, not a replacement for the full service API. Other limits:

- Three fixed methods/parameters, one project, synthetic Amp, and a two-session
  merge/split. No arbitrary filters, whole-workspace query semantics, multi-vendor
  topology, conflicting parent claims, source renames or graph-size extremes.
- Complete inventory parsing remains required. Empty/deleted inputs represent
  intentional inventory removal; the benchmark does not distinguish temporarily
  unavailable storage from deletion. That distinction needs an authority contract.
- Trusted local, single-writer cache. Reads check artifact hashes and typed values,
  but preparation trusts unchanged manifest entries without re-reading artifacts.
  Corrupt/missing files, malicious metadata, schema migration, parameter/version
  invalidation beyond the fixed contract, and recovery require separate work.
- Old artifacts and snapshots are retained, not garbage-collected. No storage
  growth/retention policy, authentication, R2 transport or atomic publication exists.

Existing direct-facts qualification, Amp live/replay validation and all four
committed metrics baselines passed on the pin; Ruff and whitespace checks passed.
No expected metrics were changed. The exact private pilot remains pending.

**Decision:** this evidence supports the per-graph artifact/prepared-summary and
snapshot-reader reuse boundary for the next design step. Reuse the existing parser,
facts and metric code. Do not promise instant full builds or replace service-wide
semantics with these three prepared responses. If faster incremental preparation
is required, the next named bottleneck is full-inventory parsing/assembly, not
compression. Before runtime adoption, specify complete-inventory/deletion semantics,
alias routing, cache trust/versioning, retention and crash-safe manifest publication,
then exercise those contracts. No acceptance threshold was supplied or invented.

## Reproduction

From the main checkout, using existing dependencies:

```sh
ROOT="$PWD"
git worktree add --detach .artifacts/graph-reuse-baseline 368d21be8330163d53ea6884f289205b3ae12226
cp scripts/benchmark-graph-reuse.py .artifacts/graph-reuse-baseline/scripts/
(
  cd .artifacts/graph-reuse-baseline
  export UV_PROJECT_ENVIRONMENT="$ROOT/.venv" PYTHONPATH=packages/core/src
  uv run --no-sync python scripts/benchmark-graph-reuse.py \
    --output "$ROOT/docs/graph-reuse-benchmark-2026-09-17.json"
  uv run --no-sync python scripts/qualify-direct-published-facts.py
  uv run --no-sync python scripts/validate-amp-live.py
  uv run python scripts/validate-metrics-baselines.py
)
uv run --no-sync ruff check scripts/benchmark-graph-reuse.py
uv run --no-sync ruff format --check scripts/benchmark-graph-reuse.py
scripts/check-metrics-quality-gate.sh
git diff --check
```

Use `--smoke --repeats 1` for three short graphs and all eight invalidation cases.
Fixture generation and seeded-cache copies are outside measurements; temporary
data are removed automatically. Remove the copied harness and generated validation
report from the disposable worktree, then remove that worktree after inspection.
