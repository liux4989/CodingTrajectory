# Internal seven-day pilot — 2026-09-17

**Completed:** the approved two-grant registry is active, the current Worker is
running, and the reviewed seven-day export is published and verified. The pilot
contains **30 sessions in 29 graphs**, with **29,681 fact rows**, at snapshot **63**.
Collection remains manual; maximum-capacity qualification is deferred.

## Subsequent read outage — 2026-09-17 03:58 UTC

The successful publication and read-back above are historical verification, not
current availability. A subsequent direct HTTPS check authenticated the reader
(200), but workspace snapshot and fact reads returned 503 `authority_unavailable`.
A live Wrangler trace identified the underlying Durable Object startup exception:

> Exceeded allowed rows read in Durable Objects free tier.

The exception originates in `State` initialization, before the requested read.
The Worker version remains `926c9693-4fcd-423c-ba54-8808eaab3ca1`. This is a
platform quota block, not a revoked reader grant. It does not establish data loss;
current data integrity cannot be reverified until access resumes. No reset,
re-upload, credential change or replacement deployment was performed.

[Cloudflare's Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
specifies 5 million rows read per day on Free and a reset at 00:00 UTC. The next
reset is **2026-09-18 00:00 UTC / 08:00 Asia/Shanghai**. Earlier recovery requires
a Workers Paid upgrade (minimum $5/month plus applicable usage), subject to user
approval. Subscription inspection through the available Cloudflare connector was
not authorized; no billing change was attempted. The runtime exception itself
confirms enforcement of the Free-tier limit.

After quota reset or an approved upgrade, first read the workspace snapshot and
29 graph records. Then verify the frozen export at snapshot 63 without publishing
again. The earlier inefficient publication scans may have contributed to quota
consumption, but an account-wide usage breakdown was not established.

## Deployment and credentials

| Item | Verified value |
| --- | --- |
| Worker | `coding-trajectory-control-plane` |
| Source commit | `7ab67d765039f904a5df9e24e739496b2dbbe6e8` |
| Bundle SHA-256 | `10818379e9692cf1f68f71baaade4350cd439dafa7fefa053a4c01f24c5fddfd` |
| Active Worker version | `926c9693-4fcd-423c-ba54-8808eaab3ca1`, 100% |
| Deployment | `706afa26-ec42-47b8-b4bb-1ead00403ad8`, 2026-09-17 03:46:46 UTC |
| WORKSPACES namespace | `867b720fe75947219811344c33645219` |
| ARTIFACTS bucket | `coding-trajectory-artifacts` |
| Authoritative encrypted registry revision | `internal-pilot-2026-09-17-v1` |
| Reader profile | `production-reader`, role exactly `read` |
| Collector profile | `production-collector`, role exactly `collect` |

The user explicitly approved replacing the complete registry and retiring all
other grants. The replacement retains the working reader and adds one collector,
with the existing collector agent UUID. Both tokens and the authoritative registry
are stored in macOS Keychain with secure read-back verified. The old collector and
estimator tokens now return **401**. The old `default` and `cloudflare` profiles
still reference retired credentials; use the named production profiles above.

The registry was activated in version `9c10a868-c0ef-4eae-89d9-c2a2dd6f0a2f` before
the publication fix. `CT_CURSOR_KEY`, the namespace and bucket were preserved
through both deployments. The deployment configuration explicitly retained the
ARTIFACTS binding, which is absent from the current tracked Wrangler config.

Live checks confirmed reader-write denial, collector-read denial, wrong-workspace
and wrong-agent denial (403), and malformed staging rejection (400), with snapshot
zero unchanged before publication. Both CLI connection checks subsequently passed.
No application owner credential or additional authentication system was introduced.

## Uploaded scope and privacy

The export covers the CodingTrajectory project's session activity during
**2026-09-10 03:36:33 UTC through 2026-09-17 03:36:33 UTC**, with complete canonical
graph structure. It was refreshed immediately before upload. Modification-time
discovery found 31 sources; one inactive graph outside the actual activity window
was excluded.

| Reviewed export | Count |
| --- | ---: |
| Sessions / source identities | 30 |
| Complete graphs | 29 |
| Fact rows | 29,681 |
| Canonical graph bytes, total | 19,110,381 |
| Largest graph bytes | 4,631,115 |
| Staging batches | 73 |

Narrative content, text previews, session previews and titles were replaced by an
explicit omission marker before recomputing and validating fact hashes. Structural
identities and measurements remain. The export passed local host-path and
credential-pattern checks and contains no known reader/collector bearer. Raw
sources, the reviewed artifact and immutable delivery journal remain private and
outside Git. No older history or unrelated projects were uploaded.

The pilot limits were 40 graphs, 24 MiB total, 8 MiB per graph and one publication
in flight. A first real canary published one complete graph with seven rows.
Its exact read-back and identical retry passed before the complete export began.
No synthetic project was published to the remote workspace.

## Publication fix and completion evidence

The first complete publication hit the Durable Object's **30,000 ms CPU limit**.
The runtime trace reported `exceededCpu`; recovery showed no publication receipt,
snapshot 62 and only the canary visible. The saved request and staged rows were
preserved. No partial full publication or application reset occurred.

The event-order validation query scanned all other events for each event because
its sequence expression lacked an index. On the largest reviewed graph, a local
SQLite comparison measured **22.5744 seconds before** and **0.018 seconds after**
adding a partial expression index. Query plans confirmed an indexed sequence
lookup. The four-line fix adds only that index; validation, publication semantics,
resource limits and payload bounds are unchanged. See
[SQLite expression indexes](https://www.sqlite.org/expridx.html) and the
[Cloudflare CPU limit reference](https://developers.cloudflare.com/durable-objects/platform/limits/).

TypeScript compilation, dry bundling, the existing four migration-interruption
scenarios and a fresh 12-row local publication/read/retry fixture passed. The
metrics gate reported no metric-sensitive paths changed. No new unit tests were
written. Index creation is additive and preserves staged and committed facts;
the existing staging-schema migration version was not changed.

The corrected deployment accepted the **same saved publication and idempotency
key**, using the already-staged data, in **8.533 seconds**:

| Result | Evidence |
| --- | --- |
| Receipt | `d45949cf-c1fa-4376-99ad-d39b4b57f4df` |
| Committed snapshot | **63** |
| Graphs published | 29 |
| Rows inserted / reused | 29,674 / 7 (canary rows reused) |
| Rows closed / graphs omitted | 0 / 0 |
| Full read-back | Every graph digest and total row count matches the frozen export |
| Identical retry | Same receipt; snapshot remains 63 |
| Independent HTTP reader | All 29 graph records at snapshot 63 |
| CLI shared reader | `project.sessions` succeeds using `production-reader` |

The CLI inventory presents collapsed orchestration cards and omits graphs without
visible overview content, so its card count is not the number of stored sessions.
All 30 session fact records are covered by the full digest-verified read-back.

## Operating state

The macOS collector launch job is **unloaded** with a **disabled** override; no
matching collector cron entry was found. No schedule was enabled. Legacy Datahub
Workers and their dedicated Access applications remain retired. Loop remains
local; the remote read verification used Core's supported CLI and HTTP consumers.

Keep the current registry when deploying any future build. Restoring an older
Worker version verbatim could reactivate retired grants. Prefer a forward fix if
another publication issue appears; never use a reset as a rollback. Exact saved
requests and receipts are available for recovery.

This completes the bounded internal collection/read pilot. Full 16 MiB graph /
96 MiB publication tests, sustained load, broad failure matrices and permanent
staging remain deferred until a real workload needs them.
