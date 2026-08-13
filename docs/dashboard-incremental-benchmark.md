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
- First useful Overview/Sessions revision: 1.845 seconds
- Complete core bootstrap wall time: 50.513 seconds
- SQLite derived state: 16,478,208 bytes
- Peak resident memory: 984,006,656 bytes
- Persisted source-message bodies: 0
- Final coverage: 575/575 sources and 39/39 components
- Final status: partial, with one retained inconclusive record

The cold bootstrap phase timings were:

| Phase | Seconds |
| --- | ---: |
| Source checkpoint reconciliation | 0.200 |
| Header topology planning | 0.822 |
| Component-scoped canonical graph rebuild | 33.455 |
| Core route projection | 6.971 |
| Core canonical economics facts | 8.500 |
| Analytical model-usage projection | 0.064 |
| Fifteen SQLite derived-revision publications | 0.310 |

The previous checkpoint-only bootstrap completed in 107.101 seconds, used a
16.17 MB database, and peaked at 1.24 GB RSS. The original transcript-copying
bootstrap took 533.631 seconds, persisted a 2.41 GB database, and peaked near
4.93 GB RSS.

## Economics acceptance evidence

On a real 229-session component backed by 953,200,289 source bytes:

- canonical core facts fell from 22.310 seconds to 2.524 seconds;
- all 688 fact rows were retained;
- root and child entrypoints exactly matched direct canonical
  `session.model_usage` and `session.usage` responses;
- the process peaked at 662,601,728 bytes while parsing the component and
  producing its facts.

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
shorter TTLs than core economics.

Filesystem notifications remain optional performance hints. Periodic inventory
reconciliation and source fencing remain the correctness authority.
