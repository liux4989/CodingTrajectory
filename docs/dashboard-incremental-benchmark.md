# Incremental Dashboard Benchmark

Measured on 2026-08-13 from the repository root with the seven-day global
source inventory. Immutable JSONL remained the authority. The active Codex
transcript was excluded by thread identity so the benchmark did not invalidate
its own source fence while recording progress.

## Corpus and cold bootstrap

- Candidate sources: 576 JSONL files
- Stable measured sources: 575
- Stable source bytes: 2,483,861,242 (2.31 GiB)
- Planned parent/child components: 39
- Progressive publication batches: 14
- First useful Overview/Sessions revision: 1.800 seconds
- Complete core bootstrap wall time: 46.460 seconds
- SQLite derived state: 16,519,168 bytes
- Peak resident memory: 480,673,792 bytes
- Persisted source-message bodies: 0
- Final coverage: 575/575 sources and 39/39 components
- Final status: partial, with one retained inconclusive record

The cold bootstrap phase timings were:

| Phase | Seconds |
| --- | ---: |
| Source checkpoint reconciliation | 0.112 |
| Header topology planning | 0.811 |
| Component-scoped canonical measurement rebuild | 31.714 |
| Core route projection | 6.129 |
| Core canonical economics facts | 7.186 |
| Analytical model-usage projection | 0.063 |
| Fifteen SQLite derived-revision publications | 0.310 |

The previous full-trajectory component bootstrap completed in 50.513 seconds,
used a 16.48 MB database, and peaked at 984,006,656 bytes RSS. The earlier
checkpoint-only bootstrap completed in 107.101 seconds and peaked at 1.24 GB
RSS. The original transcript-copying bootstrap took 533.631 seconds, persisted
a 2.41 GB database, and peaked near 4.93 GB RSS.

Core adapters still perform canonical vendor parsing. After stable identifiers
are assigned, datahub readiness requests the neutral `measurements` retention
policy, which releases message text, reasoning text, tool bodies, unused event
payloads, context source text, and composition categories before sessions
accumulate into a component. It retains hierarchy, turn/item timing, tool
outcomes, usage observations, runtime observations, pricing inputs, and
reconciliation evidence. Full trajectory retention remains the default and is
used lazily for evidence routes.

## Economics acceptance evidence

On a real 229-session component backed by 953,200,289 source bytes:

- canonical core facts fell from 22.310 seconds to 2.524 seconds;
- all 688 fact rows were retained;
- root and child entrypoints exactly matched direct canonical
  `session.model_usage` and `session.usage` responses;
- the process peaked at 662,601,728 bytes while parsing the component and
  producing its facts.

On the same component, comparing the current full and measurement-retained
paths in fresh processes:

- all three datahub route entities and all 688 canonical fact rows had the
  identical SHA-256 digest
  `15ca207fd57432ec92ab005ffdd6bcd22faf486aa0f84dc2b904ba541c33ebdb`;
- peak resident memory fell from 672,284,672 to 404,062,208 bytes;
- canonical ingest fell from 11.474 to 11.034 seconds;
- retained event objects fell from 80,110 to 57,649 while session, turn, item,
  and usage-observation counts remained identical.

On a seven-session component, the lazy evidence bundle exactly matched direct
canonical responses for both root and child entrypoints across:

- `session.model_usage`;
- `session.usage`;
- `session.tool_usage`;
- `session.stats`;
- `session.overview`.

The full metrics baseline workflow passes all Codex, Claude Code, and Pi
fixtures, including the `billed_token_usage` reconciliation invariants.

## Progressive consistency

Core builds components smallest-first within each modification-day bucket and
publishes a revision after a bounded source/byte/time batch. Every partial
revision contains only completely assembled components and exposes coverage:

```json
{
  "state": "catching_up",
  "processed_sources": 5,
  "total_sources": 575,
  "processed_components": 5,
  "total_components": 39,
  "inconclusive_records": 0,
  "complete": false
}
```

The final measured revision reports `state: partial`, `complete: true`, and the
one retained inconclusive record. `complete` means the complete inventory was
processed; it does not erase or relabel inconclusive evidence.

## Storage and lifecycle

SQLite persists only compact source checkpoints, topology relationships, core
economics contributions, revision history, failures, and read models. It does
not persist a second canonical transcript. Historical entity versions expire
with the bounded revision window; lazy evidence and browser query caches use
shorter TTLs than core economics. Garbage collection now runs after both cold
bootstrap and normal inventory reconciliation, so an already-ready database
also removes legacy source-message copies and compacts only when at least 64 MB
and one quarter of its pages are reclaimable. The datahub retains 96 change
revisions (about 24 minutes at the normal 15-second reconciliation interval);
older browser cursors receive the existing reset-required response instead of
keeping unbounded entity and delivery history.

Applying the same GC to the existing long-lived datahub database reduced the
main file from 2,640,257,024 to 189,976,576 bytes. A post-vacuum WAL checkpoint
left the WAL at zero bytes. This operation removed only disposable derived
history; immutable JSONL sources were not modified.

The datahub now writes `read-models-v3.sqlite3` with a strict store-format
marker. Store initialization does not migrate old columns or decode legacy
payload envelopes. An obsolete v2 database is never opened as a fallback; v3
bootstraps progressively from immutable JSONL. After v3 completes, normal
reconciliation removes the exact v2 database/WAL/SHM family only after 24 hours
without a file update, avoiding interference with an older process that is
still shutting down.

When the revisioned runtime is present, HTTP routes no longer fall back to a
full-corpus service scan. A supported scope that is still materializing returns
503; an unsupported scope returns 400. The remaining service fallback is only
for embedding the handler without an incremental runtime and is not a database
compatibility path.

Filesystem notifications remain optional performance hints. Periodic inventory
reconciliation and source fencing remain the correctness authority.

## Compact-graph follow-up (2026-08-15, store format v3)

After the compact-graph plan landed (streaming measurements-retained ingestion,
byte-range provenance with lazy detail hydration, measured evidence rebuilds,
disposable store format 3 in `read-models-v4.sqlite3`), the same benchmark
method reports, on a slightly larger corpus:

- Candidate sources: 627 JSONL files (606-627 stable across runs)
- Complete core bootstrap wall time: 52-57 seconds (old code, same corpus:
  56.8 seconds)
- SQLite derived state: 168-184 MB, dominated by the current-only detail
  locator tables (`detail_events` 258-283k rows, `detail_items` 64-70k rows)
  that carry source byte ranges and digests for lazy hydration
- Peak resident memory during bootstrap: 528 MB after the streamed-locator
  follow-up (old code, same corpus: 462 MB; first compact-graph cut: ~710
  MB). Item provenance records constituent event ids instead of duplicating
  span objects, the stabilizer transfers its span map by ownership, and
  detail locators stream into SQLite in 4000-row chunks, so a giant root
  never materializes its full locator object graph. The residual over the
  old code is the provenance span map itself — the cost of verified lazy
  detail, which the old code did not provide. The gated measurement-retained
  rebuild of the 953 MB component stays at 270 MB (baseline full-trajectory
  rebuild: 404 MB)
- Persisted source-message bodies: 0 (the `source_messages` table no longer
  has payload columns)
- Hydrated item/event detail responses are byte-identical to full-trajectory
  responses; stale source bytes are refused by digest fences and trigger
  reconciliation

Bootstrap publishes pending entities, facts, and detail locators per graph
inside large components (5/10 roots, 32 MB, or 2 s triggers) rather than only
at component boundaries. Store format 3 adds partial `WHERE deleted = 0`
keyset indexes and runs `PRAGMA optimize` after bootstrap and major GC. Legacy
format files are rejected via the store-format marker; v3 databases are
retired after the same 24-hour inactivity grace period used for v2.
