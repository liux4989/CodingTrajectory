# Remote query cost review — 2026-09-17

The user requested two agents: one to fix pagination and one to audit other
likely cost/failure risks. This work used local synthetic data only. No production
probes, uploads, resets, billing changes or deployments were performed.
The prior remote outage was confirmed as Free-tier Durable Objects row-read
quota exhaustion; see [the incident record](internal-pilot-2026-09-17.md).

## Completed pagination repair

`cloudflare/control-plane/src/facts.ts` now reads ordered primary-key prefixes
(graph, kind, fact ID), stopping at the row or UTF-8 byte budget with one matching
lookahead row. It removes the window queries over the entire remaining result
and the separate continuation scan. Existing indexes, cursor signatures,
snapshot visibility, filters and page bounds are preserved.

Local verification:

- TypeScript compilation and whitespace checks passed.
- Existing control-plane qualification passed **4,993 checks**, including access
  denial, cursor tampering, historical reads and **107 byte-bounded pages**.
- An independent SQLite comparison returned identical old/new pages for 31,500
  facts across 16 pages, plus history, future versions, kind filters, continuation
  positions and Unicode byte boundaries.
- The new traversal fetched 31,515 matching rows (one lookahead on each nonfinal
  page). SQLite VM steps fell from 39,093,700 to 513,100, approximately 76-fold;
  elapsed time fell from 1.564 to 0.0383 seconds in that local experiment.
- Query plans used the existing primary key with graph/kind/fact-ID constraints
  and no sort. Historical versions and graph discovery can still add work;
  this is not a strict bound on all storage rows examined.
- The metrics gate skipped because no metric-sensitive paths changed. No unit
  tests were added. Local capacity fixtures do not qualify deployed capacity.

These local measurements are not Cloudflare billing counters. The patch is
committed but not deployed; the deployed Worker and current quota remain unchanged.

## Other findings, in practical priority order

| Finding | Evidence | Smallest next action |
| --- | --- | --- |
| Item/turn duplicate-order validation repeats scans | `facts.ts`, `validateStagedGraph`: correlated comparisons filter parent/sequence after searching same-kind rows | Group duplicate validation or use a targeted index, weighing index writes; address before another sizable upload |
| Separate API calls download complete scopes repeatedly | `fact_repository.py` caches within a repository; `http_service.py` creates one per request; even project session listing loads full facts | Use existing `core.batch` and narrow session/graph scope; avoid repeated full-project checks |
| Each staging acknowledgment recounts earlier staged rows | `writeStagedRows` calls `missingFactRows`, which recounts normalized rows across completed batches | Use transactionally maintained batch metadata for acknowledgments, retaining publication integrity checks |
| Write allowance is a separate risk | Staging insert, published insert and staging deletion imply about 89,043 logical mutations for 29,681 facts before metadata/index work | Keep collection manual; avoid unchanged restaging; measure before the next upload |
| Error diagnosis loses detail | Workspace catch masks unexpected exceptions; Python RPC client drops structured non-2xx codes | Preserve safe error codes and bounded internal diagnostics without payloads or credentials |

The item/turn audit used the existing turn-order query with 100/500/1,000
synthetic rows: approximately 0.009/0.236/0.964 seconds and
141k/3.51m/14.01m SQLite VM steps. A temporary in-memory parent/sequence index
reduced the 1,000-row case to about 0.0019 seconds and 32k steps. The pilot has
5,752 items, with 1,265 in its largest graph; the sum of per-graph item counts
squared is 3,012,896 possible candidate examinations. These are workload models,
not measured billed reads.

Edge duplicate validation has a similar scan pattern, but the pilot has only one
edge, so defer that optimization. Startup showed no repeated full-fact scan:
idempotent schema setup and one-time index creation do not justify a redesign.

Cloudflare documents 5 million reads/day and 100,000 writes/day on Free; deletes
and index maintenance contribute to writes. See [pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
and [SQL storage accounting](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/).
The account-wide usage breakdown remains unverified.

## Recovery scope

Deploy the pagination candidate while preserving the two-grant registry, cursor
key, namespace and bucket. After quota reset or an explicitly approved upgrade,
check snapshot 63 and a small graph-only page first. Do not repeat the upload or
reset the workspace. Defer further whole-project reads until necessary; keep
collection paused and broader scaling work deferred.
