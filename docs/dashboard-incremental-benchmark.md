# Incremental Dashboard Benchmark

Measured on 2026-08-13 from the repository root with the seven-day global
source inventory. Immutable JSONL remained the authority; the benchmark used a
stable copy of the currently active Codex transcript so source fencing was not
invalidated by the benchmark's own progress messages.

## Corpus and bootstrap

- Sources: 589 JSONL files
- Source bytes: 2,552,219,101 (2.38 GiB)
- Complete JSONL records: 1,644,214
- SQLite derived state: 2,412,007,424 bytes
- Cold bootstrap wall time: 533.631 seconds
- Published revision: 590
- Canonical fact rows: 2,965
- Analytical rows: 310
- Read-model entities: 91
- Observable inconclusive records: 2 graphs with no visible canonical
  `project.sessions` row

The cold bootstrap phase timings were:

| Phase | Seconds |
| --- | ---: |
| Checkpoint and message ingestion | 213.706 |
| Canonical graph rebuild from checkpointed messages | 128.123 |
| Core projection | 8.146 |
| Canonical analytical facts | 181.409 |
| Analytical rollups | 0.055 |
| SQLite publication overhead | 1.513 |

Bootstrap remains intentionally expensive and is exposed as `catching_up`.
Supported dashboard routes do not launch concurrent legacy full-corpus scans
while this first consistent revision is being built.

## Hot HTTP routes

These are complete local HTTP request times, including SQLite query,
reconstruction, JSON serialization, and transfer to the local client. No
response used the legacy fallback.

| Route | Time (ms) | Response bytes |
| --- | ---: | ---: |
| Overview | 15.197 | 4,220 |
| Projects | 0.988 | 887 |
| Sessions | 1.203 | 28,631 |
| Session Timeline | 1.093 | 2,900 |
| Model Usage | 3.948 | 267,645 |
| Error Collection | 1.686 | 13,222 |
| Cache Breaks | 1.960 | 32,367 |
| Token Efficiency index | 1.559 | 3,208 |
| Token Efficiency project | 3.400 | 8,361 |
| Context Window | 394.057 | 49,139 |
| Snapshot | 7.098 | 418 |
| Revision changes | 7.507 | 8,205 |

All migrated hot routes are below the five-second acceptance threshold.

## Incremental acceptance evidence

On a real Codex source fixture, appending one 383-byte complete JSONL message:

- read exactly 383 new bytes and one new record;
- rebuilt only its connected graph from persisted messages;
- republished core and analytical rows in one revision in 274.260 ms;
- produced an unchanged follow-up refresh with zero parsed bytes in 1.572 ms.

The unchanged full-corpus reconciliation checked the authoritative 589-path
inventory, parsed zero source bytes, and completed in 16.259 ms.

The persisted canonical facts reproduced the direct canonical
`project.sessions` payload exactly (SHA-256
`9b2669d1d7733fc9d20a47526f0b221d7e1fa1ade3ef398a16229f3312901633`)
and reproduced the Context Window payload exactly (SHA-256
`d37ce531ca37c0c3c885d0e872127aaf69bad119459def4728b9cb496084b540`).
The committed metrics baseline workflow passed all four fixtures and the
`billed_token_usage` reconciliation invariants.

## Scope and constraints

- The durable default read-model horizon is seven days. Project/model filters
  at that horizon are lazily materialized from persisted facts. Other horizons
  retain the compatibility path; default dashboard traffic uses the revisioned
  runtime.
- The registry detects normal append, truncation, inode replacement, and
  same-size rewrite. Under the immutable-source contract, a file must not
  rewrite its unsampled middle and grow in the same operation; prefix/tail
  checkpointing cannot prove such a mutation was not an append.
- Filesystem notifications remain optional hints. Periodic authoritative
  inventory reconciliation is the correctness mechanism.
