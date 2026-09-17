# Artifact publication local benchmark, 2026-09-17

This synthetic, disposable Miniflare run measures the committed implementation
at `3e7f03bee6b5f677d9c2cb3acc88c60463e95d01`. The two-graph raw report is
[`artifact-publication-benchmark-2026-09-17.json`](artifact-publication-benchmark-2026-09-17.json).
The independently transferred pinned Mac sampling precursor is
[`artifact-publication-benchmark-2026-09-17-mac.json`](artifact-publication-benchmark-2026-09-17-mac.json)
(SHA-256 `e8935576b891f1acd5b4a60b57c93171cb4f9672ec642967155099254a6e4d62`).
It uses local workerd SQL cursor counters and instrumented R2 calls. These are not
production billing counters, billed Worker CPU, or isolate-memory measurements,
and they do not guarantee Free-plan capacity.

Environment: Linux x64, Node v26.8.2, Miniflare
5.20260907.0-alpha, workerd 1.20260907.1. This orb could not sample the shared
workerd process CPU/RSS; the same pinned harness enables those samples on macOS.

The macOS 27.2 arm64 precursor reproduced its SQL, HTTP, R2, and retained-byte
counts and qualified process sampling. Shared-workerd samples were 10 ms / 78.1 MiB for initial
publication, 0 ms / 79.0 MiB unchanged, 10 ms / 81.3 MiB for one changed graph,
and 10 ms / 82.5 MiB for prepared-summary plus selected-detail reads. CPU has
10 ms process-time resolution, and RSS is the shared workerd process rather than
an isolate heap. It predates changed-only PUT suppression; the focused current
candidate Mac matrix is recorded separately.

| Scenario | HTTP | SQL reads | SQL writes | R2 calls | New bytes | Retained |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Initial, two graphs | 5 | 32 | 11 | 8 head, 4 put, 1 list | 1,432 | 4 objects / 1,432 B |
| Unchanged collector run | 0 | 0 | 0 | none | 0 | 4 objects / 1,432 B |
| One changed graph | 3 | 38 | 11 | 6 head, 2 put, 1 list | 716 | 6 objects / 2,148 B |
| Two summaries + one selected detail | 4 | 60 | 0 | 3 get | 0 | 6 objects / 2,148 B |

Payloads are deliberately tiny, so byte totals demonstrate accounting and
content-addressed reuse rather than a representative real export size. A changed
publication sends only the selected graph's two PUT requests. The Worker still
heads all complete-manifest references, preserving missing-object detection.

## 29/116-graph and cleanup operation matrix

The [29-graph](artifact-publication-benchmark-2026-09-17-29-graphs.json) and
[116-graph](artifact-publication-benchmark-2026-09-17-116-graphs.json) runs use
one-row synthetic graph artifacts. Registration and checkpoint publication are
outside the timed scenarios; these are artifact-path counts, not full collector
lifecycle totals. Pinned macOS reruns are available for
[29 graphs](artifact-publication-benchmark-2026-09-17-29-graphs-mac.json)
(SHA-256 `988ac5b086ede504f46013caa4142536371a6992e50d432e3b71d28dbca89581`)
and [116 graphs](artifact-publication-benchmark-2026-09-17-116-graphs-mac.json)
(SHA-256 `e61f6870531fd2ecc823d09e07f62d952ca78d605578d0f63a5820017780a1f4`).

| Graphs | Scenario | HTTP | SQL reads/writes | R2 calls | Uploaded / retained |
| ---: | --- | ---: | ---: | --- | ---: |
| 29 | Initial | 59 | 32 / 11 | 116 head, 58 put, 1 list | 20,764 / 20,764 B |
| 29 | One changed | 3 | 38 / 11 | 60 head, 2 put, 1 list | 716 / 21,480 B |
| 116 | Initial | 233 | 32 / 11 | 464 head, 232 put, 1 list | 83,056 / 83,056 B |
| 116 | One changed | 3 | 38 / 11 | 234 head, 2 put, 1 list | 716 / 83,772 B |

The Mac run measured initial/changed shared-workerd CPU at 80/20 ms for 29
graphs and 230/50 ms for 116 graphs. Absolute shared-process RSS samples rose
from 96.2 to 103.3 MB in the 29-graph process and from 125.6 to 143.7 MB in the
116-graph process. These are 10 ms-resolution process samples, not per-scenario
isolate heap or billed CPU.

The unchanged operation callback is zero by construction after the real
collector suppresses publication; it is not an end-to-end collector timing. The
32-check runtime qualification separately executes the production `LocalCollector`
and verifies no unchanged uploads/publication, two PUTs for one changed graph,
and complete reupload after an uncertain response.

The [cleanup run](artifact-cleanup-benchmark-2026-09-17.json) seeded 4,101
unreferenced objects. Initial publication stopped at the four-page bound with
103 orphans remaining; the next successful changed publication resumed from the
stored cursor with one list/delete page and reached exactly six retained objects.
The [pinned Mac cleanup run](artifact-cleanup-benchmark-2026-09-17-mac.json)
(SHA-256 `56f3199e546d2759725614f0673f066b8a58f620d171664b1a903443f5f027f5`)
reproduced those counts. Seeding occurred outside timing and drove the shared
simulator process to roughly 354 MB RSS; this is not production memory evidence.

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
  `76d554b1f4bc4c57c5660ca14dcbaa69ee01073f0cc20d2f425e07346ad2045b`
- `artifact-benchmark-worker.ts`:
  `ec8433688b99560170b01c223335f34d89f575e7f208870bbd5ee7f174c05290`
