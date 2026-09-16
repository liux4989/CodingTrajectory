# Indexed Historical Facts

- **Status:** Implemented
- **Depends on:** [Authority boundaries](../authority-boundaries.md) and [Direct Published Facts](direct-published-facts.md)
- **Wire impact:** None

## Decision

Local and remote historical repositories retain one `FactIndex` built from
validated `PublishedFactSet` rows. The index contains only canonical row order
and lookups by kind, fact ID, parent, graph ownership, and entry point. It does
not calculate summaries, metrics, coverage, or missing values and does not
repair rows.

Shared Python handlers remain the only semantic implementation. Session and
graph methods materialize only the selected canonical graph. `project.sessions`
is the irreducible aggregate caller: it applies project and vendor selection
from typed payloads, then materializes every remaining graph because existing
orchestration-run, visibility, runtime, and usage semantics operate on canonical
graphs. No reconstructed graph or `DocumentStore` is cached.

The local repository now owns batch reuse and availability checks directly.
`LocalHistoricalRepository`, `document_store_from_fact_sets`,
`session_graph_from_fact_set`, the reconstructed-store caches, and the unused
`connection_profile` qualifier were deleted. The public fact schema, row bytes,
digests, Worker validation, response contracts, source fences, checkpoints,
outboxes, staging protocol, and signed cursors are unchanged.

## Privacy-safe evidence

An owner-local, read-only 30-day collection covered 374 supported source files,
204 discovered graphs, and 12 project publications. Three graphs did not satisfy
the existing publication contract; distributions below use the 201 valid fact
sets and disclose no identifiers, names, paths, source values, or per-graph
rows.

| Aggregate | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Encoded fact-set bytes/graph | 229,200 | 1,835,702 | 3,763,810 | 7,077,894 | 7,556,009 |
| Rows/graph | 351 | 2,718 | 6,014 | 11,051 | 11,856 |
| Maximum row bytes/graph | 1,985 | 4,439 | 6,118 | 9,170 | 21,595 |
| Graphs/project publication | 1 | 54 | 108 | 108 | 108 |
| Total publication bytes | 817,894 | 35,340,250 | 71,239,324 | 71,239,324 | 71,239,324 |

Fixed logarithmic histograms:

- Fact-set bytes `[0,1KiB,4KiB,16KiB,64KiB,256KiB,1MiB,4MiB,8MiB,∞)`:
  `0, 6, 11, 27, 64, 60, 23, 10, 0`.
- Rows `[0,8,32,128,512,2048,8192,32768,131072,∞)`:
  `4, 13, 40, 65, 51, 21, 7, 0, 0`.
- Maximum row bytes `[0,256B,1KiB,4KiB,16KiB,64KiB,256KiB,512KiB,∞)`:
  `0, 13, 163, 24, 1, 0, 0, 0`.
- Graphs/publication `[0,1,2,4,8,16,32,64,128,256,512,∞)`:
  `0, 8, 0, 0, 1, 1, 1, 1, 0, 0, 0`.
- Publication bytes `[0,4KiB,16KiB,64KiB,256KiB,1MiB,4MiB,16MiB,∞)`:
  `0, 0, 1, 3, 2, 2, 1, 3`.

Cold canonical discovery took 24,085.768 ms. A cold collector parse/project pass
took 27,727.866 ms; an immediate unchanged second pass took 25,859.315 ms and
reported 0 normalization-cache hits out of 374 candidates. Process peak RSS was
3,778,691,072 bytes. The cache had no persisted hit telemetry, so a representative
scheduled-run hit rate could not be recovered. Persistent normalization caching
was therefore not justified and was deleted; canonical sessions are reparsed
under the existing source fences.

Active-graph update deltas were not available from a static read-only snapshot.
That gap, plus publications up to 71,239,324 bytes (well above the 16 MiB atomic
publication bound), is decisive against lowering graph/publication limits or
removing multi-batch staging. Staging and all hard limits remain unchanged.

## Synthetic performance evidence

On the same runner, 128 deterministic graphs / 2,048 fact rows were measured in
five fresh builds and twenty narrow `session.items` reads. The exact merged-base
implementation rebuilt a complete `DocumentStore`; the optimized path built one
`FactIndex` and materialized only the selected graph.

| Path | Build median | Build peak allocation | Narrow-read median |
| --- | ---: | ---: | ---: |
| Merged base | 41.344 ms | 3,109,042 bytes | 0.281 ms |
| Indexed | 5.709 ms | 603,440 bytes | 0.432 ms |

Index construction is 86.2% faster and uses 80.6% less peak traced allocation.
The intentionally uncached selected-graph materialization adds 0.151 ms to a
warm narrow read; build plus first narrow read falls from 41.625 ms to 6.141 ms.

## Residual complexity

- Canonical ingestion still owns `DocumentStore`/`SessionGraph` construction.
- Existing semantic handlers still require transient selected-graph
  materialization; moving individual summaries or metrics to fact-specific
  handlers would create the prohibited parallel semantic path.
- `project.sessions` necessarily materializes all graphs that survive indexed
  selection.
- Remote paging, independent Python/TypeScript validation, staging, source
  fencing, checkpoints, outboxes, atomic replacement, and signed cursors remain.
