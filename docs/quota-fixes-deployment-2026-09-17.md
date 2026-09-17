# Query-cost fixes deployed — 2026-09-17

The user authorized finishing the audit repairs and deploying them. Worker
version **738480ff-10ea-47c8-844d-d888198702b2** is live at **100%**. Reader and
collector authentication and workspace identity pass. Snapshot access remains
blocked by the existing Free-tier quota, now explicitly reported as
`database_read_quota_exceeded`; fresh data integrity verification is pending.

## Exact deployment

| Item | Value |
| --- | --- |
| Source commit | `a6e59ea6ac8f3de7d0e76eda60642fa9383ae1cd` |
| Bundle SHA-256 | `83d46ba706943d27a3baa82415751ab098c225cf882ca0ae0290e4cd77f22d9a` |
| Deployment | `baace7b4-d7f6-4ac4-b4b5-fa7f36e8e805` |
| Deployed UTC | 2026-09-17 04:25:56 |
| Post-deployment check UTC | 2026-09-17 04:26:28 |
| WORKSPACES namespace | `867b720fe75947219811344c33645219` |
| ARTIFACTS bucket | `coding-trajectory-artifacts` |

The frozen bundle was uploaded, its bindings inspected, then its exact version
activated. `CT_PRINCIPALS` and `CT_CURSOR_KEY` were inherited without replacement.
The encrypted registry's active-version metadata was updated; grants are unchanged.
The existing ARTIFACTS binding is now explicit in tracked Wrangler configuration.
No data reset, real upload, namespace migration, paid upgrade or schedule change
was performed. Collection stays manual and paused.

## Audit disposition

- **Pagination:** indexed page traversal stops at row/byte bounds with one lookahead.
- **Item/turn ordering:** grouped validation replaces repeated sibling scans,
  retaining rejection semantics without an extra index.
- **Edge identity:** grouped JSON identity validation likewise replaces its
  correlated duplicate scan, preserving distinct fact and null semantics.
- **Staging acknowledgments:** read atomic batch metadata rather than recounting
  normalized rows after every batch. Explicit missing-batch recovery and final
  publication retain integrity checks. The missing-batch response also now matches
  its strict Python contract (no stage-only `staged_batches` field).
- **Repeated HTTP reads:** a process-local, per-factory cache holds at most 32 MiB /
  four entries, keyed by credential digest, workspace, snapshot and scope. A fresh
  authenticated snapshot read precedes each reuse. Deserialization isolates
  mutable caller views. No disk cache, stored plaintext token or revoked-access
  fallback. Separate CLI processes do not share it; concurrent cold requests can
  still duplicate work. These Python changes are local client/service code, not
  part of the JavaScript Worker deployment; existing Python servers must restart
  to load them.
- **Lost-response write amplification:** collector retries check for an existing
  publication receipt before staging. If present, replay the exact publication
  directly, letting the server verify idempotency identity. If absent, normal
  staging still runs. Existing unchanged-publication digest suppression remains.
- **Diagnosis:** Worker and Durable Object error boundaries emit fixed sanitized
  diagnostics and distinct read/write quota codes. Python retains bounded
  structured error codes and HTTP status without exposing raw server messages.

The inherent write volume is not eliminated: staging, publication, deletion and
indexes consume storage work. Free-plan write headroom remains a measured-workload
planning constraint, not a solved capacity guarantee. No automatic collection is
enabled. See [the workload benchmark](pilot-query-benchmark-2026-09-17.md).

## Validation

- Existing local Worker qualification passed **4,993 checks**, including 107
  byte-bounded pages, historical reads, access denial and atomicity scenarios.
- After correcting the missing-batch wire shape, targeted typed local reads before
  and after staging and the 12-row stage receipt passed.
- Fact-index qualification passed **19** parity/filter/order/index checks;
  staging migration passed **four** interruption phases; collector preparation
  passed **five** deterministic checks.
- Offline collector journal replay: recovered receipt -> **zero** stage calls;
  missing receipt -> **73** stage calls; conflicting exact replay remains pending
  and is not marked accepted. No real-data network calls in these experiments.
- Cache experiments: two independent requests -> two auth/snapshot checks and
  one fact download. Credential/snapshot/scope isolation, revocation, mutation
  isolation and byte/entry eviction passed. The pilot fits the byte budget.
- Error handling passed local Worker classification and nine mocked HTTP cases;
  existing 401/403/409 faults remain intact.
- TypeScript, Ruff, whitespace and dry bundle checks passed. The metrics gate
  skipped because no metric-sensitive paths changed. No unit tests were added.
- Live post-deployment: reader200/read, collector200/collect, workspace/version
  match; one snapshot request503/`database_read_quota_exceeded`. No further data
  reads were attempted once the quota block was confirmed.

## Next verification

The documented Free daily reset is 2026-09-18 00:00 UTC / 08:00 Shanghai. After
reset, verify snapshot63 and one small graph-only page. Do not reset or re-upload.
No scheduled retry was created. Roll forward for further issues; if rolling code
back, preserve the current registry and bindings rather than reactivating old
credentials. Restoring code does not revert stored data.
