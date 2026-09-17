# Seven-day session query benchmark — 2026-09-17

**Execution update:** the combined fixes are now [deployed](quota-fixes-deployment-2026-09-17.md).
The findings and pre-deployment measurements below are historical evidence.

Measured locally against the exact frozen, privacy-reviewed export: **30 sessions,
29 graphs, 29,681 facts and 73 staging batches**. No network calls, credentials,
production writes or raw-session output. The private input remains outside Git;
[aggregate results](pilot-query-benchmark-2026-09-17.json) include its SHA-256.

## Follow-up: grouped validation selected

The next comparison on the same export selected grouped item/turn validation,
now implemented locally in `facts.ts`. Item validation fell from **2.655 s /
29,774,000 VM steps** to **35.0 ms / 300,100 steps** (about 99x fewer steps).
The index alternative took 17.0 ms / 187,400 steps, but adds persistent index
maintenance. Grouping avoids that write cost and requires no schema migration.
Turn validation fell from 9.48 ms to 1.58 ms.

Both paths accepted the full export. A transient local SQL equivalence experiment
matched the old and new rejection decisions for 2,916 combinations of integer/null
sequences, same/different parents and same/different fact IDs across both kinds.
The grouped query preserves order-index mismatches, distinct-fact duplicates and
NULL semantics; see [SQLite aggregates](https://www.sqlite.org/lang_aggfunc.html).

[Follow-up measurements](grouped-validation-benchmark-2026-09-17.json) identify the
working source by SHA-256; `source_commit` is its parent revision at measurement.
The earlier results below remain historical evidence. Neither repair is deployed.
TypeScript and lint checks passed, as did all **4,993 existing local Worker
integration checks**, including 107 byte-bounded pages and rejection/atomicity
scenarios. The metrics gate skipped because no metric-sensitive paths changed.
No unit tests were added.

## Results

| Path | Current / before | Local alternative / after | Meaning |
| --- | --- | --- | --- |
| Pagination | 2.266 s; 41,642,800 VM steps | 0.0383 s; 480,800 steps | About 87x fewer steps; identical 19 pages |
| Item-order validation | 2.478 s; 29,774,000 steps | 0.0149 s; 187,400 steps | About 159x fewer steps with temporary parent/sequence index |
| Turn-order validation | 8.69 ms; 58,100 steps | 1.40 ms; 9,300 steps | Improvement, but small absolute cost |
| All staging acknowledgments | 9.95 ms; 354,800 steps | 0.197 ms; about 1,300 steps | 115,697 normalized rows recounted versus 241 batch metadata rows |
| Three project reads, separate readers | 57 page calls; 89,043 facts; 57.30 MB; 6.86 s | Shared reader: 19 calls; 29,681 facts; 19.10 MB; 1.84 s | Existing cache eliminates two repeated downloads/materializations |
| One fresh write lifecycle | 29,681 staging inserts + 29,681 published inserts + 29,681 staging deletes | **89,043 logical fact-table mutations** | Excludes all metadata, retries, canary and index overhead |

The pagination repair is already committed locally, not deployed. The validation
index and metadata-only acknowledgment are benchmark alternatives only; they do
not change runtime code. Both validation variants accepted all actual graphs.
Acknowledgment variants returned the same completed-batch sets after each stage.
Metadata-only acknowledgment assumes intact transactional staging; it must not
replace publication integrity checks.

## Method and limits

- SQLite 3.49.1, in-memory database, schema extracted from current `facts.ts`.
  The item/turn and staging SQL are extracted verbatim from that source.
- Timings/VM steps are medians of three query runs. Staging figures sum the
  per-batch medians. VM progress callbacks count approximately every 100 steps;
  small-query counts are coarse. Timings include callback overhead and warm-cache
  effects. Input validation and database loading are excluded.
- Pagination replays the pre-fix SQL from commit `aec71b0` and mirrors the current
  indexed traversal. It compares every page including payload strings. The new
  traversal fetched 29,699 matching candidates for 29,681 facts (18 lookaheads).
  This excludes graph discovery, cursor signing, HTTP and empty absent-kind seeks;
  it is not an end-to-end Worker benchmark.
- Reader comparison uses the actual `CloudflareFactRepository` and Pydantic
  validation with an in-memory page source. Three same-scope `project.sessions`
  store requests are compared with separate versus shared repositories. It
  measures cache reuse and fact materialization, not HTTP latency, rendered
  response generation, mixed scopes, or the `core.batch` endpoint itself.
  MB are decimal and count row arrays, excluding envelope/digest metadata.
- Writes model a fresh lifecycle, not the prior canary/retry history. Published
  inserts/deletes are counted with SQLite `total_changes`; staging count equals
  the loaded corpus. Index maintenance is excluded from that counter. The
  temporary full index would contain another 29,681 entries and impose creation,
  insertion and deletion work; its write cost was not benchmarked.
- These measurements are **not Cloudflare billing counters**, do not establish
  which prior operation exhausted the account quota, and do not qualify deployed
  CPU, memory, latency or capacity. No new unit tests were added.

## Practical next changes

1. Deploy the already-verified pagination patch, then use a small read after quota
   recovery rather than repeating whole-project verification.
2. Grouped item/turn validation is now selected and implemented locally; include
   it with pagination in the next deployment. No additional index is needed.
3. Reuse existing readers/batching for related same-scope reads and narrow scopes
   where possible. The cache benefit is real without a new persistent cache.
4. Keep staging acknowledgment optimization lower priority for this small pilot:
   avoid needless scans, but its absolute measured latency is only about 10 ms.
   Keep collection manual while write usage is unmeasured.

## Reproduction

Run from the repository root, supplying the private reviewed export and a local
aggregate output path:

```sh
PYTHONPATH=packages/core/src uv run --no-sync python scripts/benchmark-pilot-query-cost.py \
  /path/to/reviewed-export.json --output /path/to/aggregate-report.json
```

The benchmark requires baseline commits `aec71b0` and `949f295` in local Git history. It never
connects to Cloudflare and creates no persistent database. It fails if compared
pages, accepted validations or acknowledgment results differ.
