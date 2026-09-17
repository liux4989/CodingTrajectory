# Artifact publication local benchmark, 2026-09-17

This synthetic, disposable Miniflare run measures the committed implementation
at `3180441e4e310b5449affcb0a91aedd2b4b62a75`. The raw report is
[`artifact-publication-benchmark-2026-09-17.json`](artifact-publication-benchmark-2026-09-17.json).
It uses local workerd SQL cursor counters and instrumented R2 calls. These are not
production billing counters, billed Worker CPU, or isolate-memory measurements,
and they do not guarantee Free-plan capacity.

Environment: Linux x64, Node v26.8.2, Miniflare
5.20260907.0-alpha, workerd 1.20260907.1. This orb could not sample the shared
workerd process CPU/RSS; the same pinned harness enables those samples on macOS.

| Scenario | HTTP | SQL reads | SQL writes | R2 calls | New bytes | Retained |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Initial, two graphs | 5 | 32 | 11 | 8 head, 4 put, 1 list | 1,432 | 4 objects / 1,432 B |
| Unchanged collector run | 0 | 0 | 0 | none | 0 | 4 objects / 1,432 B |
| One changed graph | 5 | 38 | 11 | 8 head, 2 put, 1 list | 716 | 6 objects / 2,148 B |
| Two summaries + one selected detail | 4 | 60 | 0 | 3 get | 0 | 6 objects / 2,148 B |

Local wall times were 46.7 ms initial, 0.04 ms unchanged, 26.3 ms changed,
and 18.9 ms for the three prepared-object reads. Payloads are deliberately tiny,
so byte totals demonstrate accounting and content-addressed reuse rather than a
representative real export size. A changed publication still checks all four
object references; only the changed graph's two objects cause R2 puts.

## Comparison and interpretation

The transferred graph-reuse benchmark is the same-boundary compute comparison:
unchanged pilot preparation fell from 4.250 s to 0.929 s, one changed graph from
4.255 s to 1.132 s, four changed iterations from 21.147 s to 4.456 s, and four
three-root/nine-response reads from 6.296 s to 138 ms. Its changed-reader peak
fell from 1,110 MiB to 77.5 MiB. That reduction comes from lazy selected-graph
loading and bounded reuse; it does not imply a smaller fact representation.

The earlier near-pilot SQL publication recorded 219,537 lifecycle writes and
2.59 million publication reads. Those are local cursor counters for a different,
larger workload, so they must not be divided into the tiny synthetic counts above
or treated as a billing ratio. The architectural comparison is still direct:
the old path performed SQL work per fact, while this run commits a small manifest
with 11 cursor writes independent of fact-row count and stores bytes in R2. A
pinned representative Mac run is required before approval of the real workload.

Harness SHA-256:

- `benchmark-artifact-publication.mjs`:
  `520d52498422d50f9ac9833a04f432c07ddcde693218e0f9f8a32a240e86e839`
- `artifact-benchmark-worker.ts`:
  `ec8433688b99560170b01c223335f34d89f575e7f208870bbd5ee7f174c05290`
