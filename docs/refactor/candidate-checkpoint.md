# Upload candidate checkpoint

Recorded 2026-09-10 from the paused upload agent's handoff. Candidate code is
uncommitted and has not been deployed. These details describe the prototype,
not the complete target contract or an independent qualification result.

## Implemented in the candidate

Additive operations use `ct.core.v1` at `/v1/core`:

| Operation | Candidate behavior |
| --- | --- |
| `ct_collector_missing_chunks` | Scoped digest negotiation, at most 128 requested digests |
| `ct_collector_upload_chunks` | Scoped content-addressed node upload |
| `ct_collector_stage_chunk_manifest` | Bounded reconstruction and legacy stage receipt plus root digest |
| `ct_publication_watermark` | Published sequence for workspace/project, optionally pinned |
| `ct_artifact_chunk_manifest` | Selected committed revision, hashes, provenance, transport kind |
| `ct_artifact_chunks` | Committed-manifest membership authorization and bounded node response |

The existing checkpoint and `publish_artifacts` transaction/idempotency path is
retained. Staging does not advance the publication watermark. Legacy gzip reads
remain available through a materialized compatibility artifact.

The chunk format preserves JSON numeral spelling and uses object children, array
children, and concatenated array blocks. Candidate limits are 64 KiB per node,
256 KiB per batch/response, 128 nodes per request, 8 MiB reconstruction, 16,384
traversal visits, and depth 32. These are candidate limits to reconcile and qualify;
they do not establish manifest-native support for larger graphs.

Local SQLite state retains source snapshots, consumed and acknowledged progress,
prepared/sending/acknowledged batches, frozen request plans, and receipts. Sending
runs through an independent database connection. An attempted batch remains frozen;
new observations are prepared separately. Replacement/truncation captures are not
coalesced into older pending work.

Candidate CLI: `ct collector sync --mode prepare|publish|serve|status`.
`--automatic` opts into delivery; manual serve prepares only, and publish drains
already prepared work. Candidate defaults are a 10-second poll, 60-second batch
interval, 256 KiB batch budget, and batch count 16. The count currently limits
batch draining; it is **not** the target changed-resource flush threshold.
Existing `collector run --chunked` is an opt-in immediate publication path.
No supervisor or scheduler was installed by this candidate.

## R0 reconciliation checklist

| Gap / decision | Required disposition | Phase |
| --- | --- | --- |
| Changed poll rebuilds the full project canonical graph | Connect to durable canonical revisions; qualify true adapter-specific incremental work separately | R1, R5 |
| No canonical repository/change-journal integration | Freeze capture interface and real transaction boundaries before coupling services | R1, R2 |
| Simplified outbox phases | Map durable candidate states to retry, blocked, commit recovery, and operator status semantics | R2 |
| Completion/resource-count flush incomplete | Implement source-fenced completion and configured resource flush; distinguish drain limits | R2 |
| Cursor reset and stale/superseded recovery incomplete | Preserve attempted batches; reconcile inventory/hash/source epochs without false ACK | R2 |
| Multi-process preparation and lifecycle incomplete | Add owner fencing, startup credential handling, graceful shutdown, and restart behavior | R2, R6 |
| Source ownership and partial omission need audit | Prove a partial host publication cannot remove another host's history; qualify transfer fences | R3 |
| Read RPCs are not a complete live reader | Add indexed change feed, pinned paging, retention reset, and Datahub adapter | R3, R4 |
| Existing query-side publication remains | Retire before-read publication coupling and qualify offline/empty local success | R4 |
| Whole-graph materialization ceiling remains | Reject above-limit graphs until bounded manifest-native validation and reads land | R5 |
| Presence absent from new sync service | Keep manual mode quiet; define separately configured presence and honest expiry | R6 |
| Backlog quotas and reference-safe retention incomplete | Add visible backpressure and qualify staged/committed/rollback reference races before deletion | R6 |
| Adversarial size and authorization coverage incomplete | Reproduce malformed DAG, cross-principal, stale epoch, and budget rejection scenarios | R2, R3 |
| CLI target names differ from candidate | Freeze one lifecycle contract; document compatibility and persist explicit mode | R0, R6 |

The target 200-resource automatic flush threshold is a proposed policy, not a
candidate capability. The target state machine may use different storage labels
if their recovery semantics are equivalent and documented.

## Agent-reported evidence

The agent reported 62 passing legacy Worker checks, 26 incremental/crash checks,
passing full metrics baselines, and passing TypeScript/schema checks. Its append
fixture reused 287 of 295 nodes; the largest observed upload was 38,756 bytes.
These fixture results do not prove production capacity or all Q01-Q22 scenarios.
The parent has not rerun those checks as part of this docs-only task.

The qualification script also has two reported lint issues to resolve: executable
file/shebang consistency and an explicit subprocess return-code policy. Reconcile
and rerun the relevant gates before committing the implementation. No production
write, authenticated live Datahub check, multi-host soak, or complete refactor
qualification is claimed by this checkpoint.
