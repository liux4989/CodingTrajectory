# Remote CT Storage Optimization Proposal

- **Status:** Proposed; no storage or schema change approved yet
- **Date:** 2026-09-02
- **Scope:** Remote collector observations, future graph revisions, retention,
  and the distinction between the active ingest plane and deferred worker
  extensions
- **Related:** [`remote-ct-control-plane-design.md`](remote-ct-control-plane-design.md)

## Decision requested

Adopt a content-addressed canonical-payload layer and a staged retention model
before activating graph projection workers. Keep the existing authority model,
ordering, idempotency, and Pydantic contracts unchanged.

This proposal does **not** authorize a migration, a deletion, an archive, or a
change to the currently accepted remote-control-plane design. It supplies the
decision record and validation gates for that work.

## Why now

A read-only storage measurement on 2026-09-02 found that the active control
plane is not broadly expensive. Sixteen accepted source observations account
for approximately 25.9 MiB of canonical `payload` data; the largest one is
approximately 10.3 MiB. The complete observation table occupies approximately
27.0 MiB including indexes and TOAST allocation. All other currently created
control-plane tables together occupy well under 1 MiB.

The cost is therefore not the count of relational tables. It is the fact that
each changed source is stored as a complete normalized session snapshot. A
growing session repeats most of its earlier content in every accepted snapshot.
The future projector would make this worse if it also stored full, inline
`SessionGraph` JSONB revisions for the same material.

The current implementation has no graph revisions yet. This is the right
point to choose the storage representation before a worker creates a second
large history.

## Current custody and its constraint

The accepted design intentionally separates four authorities:

| Authority | Active durable records | Storage consequence |
|---|---|---|
| Historical graphs | Source observations; future graph revisions | Canonical content is potentially large |
| Project inventory | Projects, revisions, aliases, private locations | Small metadata |
| Living state | Current leases and heartbeat observations | Small, append-only operational history |
| Estimation | Jobs, attempts, forecast events | Deferred; currently empty |

The remote collector sends an already normalized canonical session snapshot.
Raw vendor logs stay on the originating host. The server relies on the
snapshot's canonical JSON digest for deduplication, ordering, and evidence.

Any optimization must preserve all of these invariants:

1. One declared canonical representation must round-trip through the existing
   Pydantic model exactly.
2. A source observation remains immutable and ordered by source epoch and
   sequence.
3. The same event and digest remains an idempotent success; conflicting content
   remains a conflict.
4. A workspace snapshot at sequence `S` must not combine pre- and post-`S`
   resources.
5. No optimization uploads a raw vendor log, a host path, or private collector
   state.
6. No retention task may delete canonical bytes until a durable replay proof
   establishes what can be reconstructed without them.

## Options considered

### A. Keep full JSONB forever

This is the current representation. It is simple: one row is enough to
rehydrate one source snapshot, and PostgreSQL manages compression internally.
It is also linear in the complete size of every changed snapshot. It remains a
reasonable small-pilot baseline, but it does not bound growth for active,
long-running sessions.

### B. Normalize every session field into relational tables

This would move turns, items, events, text, metrics, and links into many
tables. It may help a few query paths, but it creates a second writable session
model, significantly expands migration work, and does not remove the bulk of
transcript text. It is rejected for the first optimization.

### C. Compressed, content-addressed canonical payloads

Store the canonical JSON bytes once per digest in an immutable payload store.
Observation and graph-revision records keep their typed metadata plus a digest
reference. The worker reads, decompresses, verifies the digest, and validates
the Pydantic model before projecting.

This preserves exact replay without asking Postgres to store repeated,
query-inaccessible JSONB documents. It is the recommended first step.

### D. Deltas plus periodic checkpoints

Store a complete checkpoint followed by validated append/delta frames, then
write another checkpoint at a defined boundary. This is the largest potential
space reduction for growing sessions, but it adds a canonical patch protocol,
base-version rules, and reconstruction failure modes. It should follow a
measured compressed-blob baseline, not precede it.

### E. Retain only the latest snapshot

This is cheap but loses exact historical reconstruction. It is incompatible
with the current sequence-snapshot and evidence model, so it is rejected.

## Recommended target

Introduce a content-addressed payload layer whose public semantic value is the
canonical JSON document, not its physical storage format.

```text
collector
  -> canonical JSON + SHA-256
  -> validate and compress
  -> immutable canonical payload blob
                  |
                  +--> source observation header, sequence, receipt, change log
                  |
                  +--> future graph revision header and source vector
```

The logical schema is:

```text
ct_canonical_payloads
  workspace_id
  content_sha256                 -- SHA-256 of uncompressed canonical JSON
  schema_version
  representation                 -- e.g. ct.source-snapshot.v1
  encoding                       -- e.g. zstd
  uncompressed_bytes
  stored_bytes
  storage_locator                -- implementation-private
  state                          -- staging | ready | failed
  created_at
  primary key (workspace_id, content_sha256)

ct_source_observations
  existing identity, ordering, timing, and receipt-related fields
  existing content_sha256        -- references ct_canonical_payloads
  payload is retired after verified backfill

ct_artifact_revisions
  existing artifact/version/source-vector fields
  existing content_sha256        -- references ct_canonical_payloads
```

The payload digest remains the digest of the uncompressed canonical JSON. This
allows an implementation to change compression or move bytes between Postgres
and object storage without changing event identity, replay evidence, or public
method behavior.

### First implementation backend

Use a private, Postgres-backed `bytea` payload blob with explicit compression
for the first migration. It preserves the current RPC transaction boundary:
the ingress can validate bytes, record the immutable blob, accept the
observation, write the receipt, and advance the change log atomically.

Supabase Storage is a later backend option, not the first move. Direct object
upload and a database commit are not one transaction; adopting it first would
require a staging/finalization protocol, orphan collection, server-side digest
verification, and recovery semantics. The proposed `storage_locator` permits
that upgrade once measured database payload growth justifies it.

### Graph projection rule

Do not enable a worker that writes a complete graph JSONB revision for every
source observation. The worker must instead use the same canonical-payload
layer, and must establish a durable source vector for every graph revision.

Before choosing graph checkpoints or deltas, measure these two quantities on a
representative private corpus:

```text
compressed full graph bytes per accepted source update
delta bytes / compressed checkpoint bytes
```

Only introduce deltas when their measured benefit exceeds the added replay and
operational complexity.

## Retention model

Retention is a custody decision, not a vacuum job. The following tiers keep
the evidence chain explicit.

| Record | Hot retention | Long-term rule | Preconditions for compaction |
|---|---|---|---|
| Source payload bytes | All recent accepted snapshots | Terminal and periodic checkpoints; compacted history only with replay proof | Exact rehydration from retained checkpoint/frame chain |
| Observation headers, hashes, ordering | Forever | Forever | None |
| Receipts | Operational window to be decided | Digest/outcome evidence retained or summarized | Retry and conflict window is closed |
| Completed projection outbox | Short operational window | Delete after idempotent projection proof | Projection output and source vector are durable |
| Lease current state | One active row per instance | Replace in place | None |
| Historical liveness observations | Short diagnostics window | Expire or summarize | No public historical-liveness contract depends on each row |
| Estimation attempts/errors | Per job policy | Summarize after completion | Forecast receipt and audit requirements are met |

Initially, no source payload is eligible for deletion: no graph projector has
yet produced durable revision/source-vector evidence. The first storage change
is representation compression, not history removal.

## Active core versus deferred extensions

The live database contains future tables for project inventory, graph
projection, outbox delivery, and estimation. Their physical cost is negligible
today, but they make the foundation harder to read as a live product.

Treat the deployment as staged modules:

```text
Active now
  workspace identity and membership
  collector agents/capabilities
  source registration, observations, receipts, change sequence
  leases and heartbeats

Deferred activation
  project inventory and host-location registration
  graph artifacts/revisions and projection outbox
  estimation jobs, attempts, and forecast events
```

Do not drop the deferred tables merely to save space. They are nearly free
physically, while a destructive rollback would create migration churn. Instead,
document their inactive state, avoid writing them, and activate each module
only with its owner and validation plan.

## Migration and validation plan

### Phase 0 — baseline and acceptance criteria

1. Record aggregate payload sizes, row counts, compressed-size ratios, and
   largest-payload sizes without exporting session content.
2. Build a private local benchmark corpus of configured session identifiers;
   reports contain aggregate byte/count data only.
3. Define a storage reduction target from measured compression, rather than
   assuming a ratio.
4. Confirm that every affected public method has an unchanged result schema and
   that remote reads can rehydrate the same canonical model.

### Phase 1 — additive payload layer

1. Add `ct_canonical_payloads`; reuse the existing `content_sha256` fields as
   the payload references.
2. Dual-write new accepted snapshots to the existing JSONB column and the
   compressed blob layer.
3. For every dual write, decompress, recompute the SHA-256, validate Pydantic,
   and compare it with the accepted observation digest.
4. Do not change query routing or retention in this phase.

### Phase 2 — verified cutover

1. Backfill existing rows through a restricted server-side process that emits
   only aggregate progress and failure counts.
2. Treat a row as blob-backed only after its referenced payload passes exact
   digest and model validation.
3. Make all remote readers use the digest reference.
4. Run the full public-method compatibility workflow against the same revision
   snapshots.

### Phase 3 — representation cleanup

1. Make inline JSONB optional for blob-backed observations.
2. Retain it during a defined rollback window.
3. Remove inline payload bytes only after the backfill is complete, remote
   readers are proven, and a restore rehearsal succeeds.

### Phase 4 — optional checkpoint/delta and object-store backends

Advance only if the measured compressed-blob baseline remains too large. A
delta protocol or object-store backend must retain the same digest,
rehydration, authorization, and cleanup guarantees.

## Acceptance gates

The proposal becomes an implementation decision only when all of the following
are explicit:

- chosen hot-retention period, checkpoint policy, and long-term evidence rule;
- measured compression ratio on a private representative corpus;
- replay proof for every compacted source sequence;
- public-method compatibility evidence for all 25 methods;
- RLS and service-boundary design for payload reads/writes;
- failure handling for an interrupted upload, duplicate digest, invalid
  compression, and failed decompression;
- a rollback plan that never destroys the only canonical copy.

## Open decisions

1. Is exact reconstruction required for every source observation sequence, or
   only every externally visible graph revision and retained checkpoint?
2. What hot window is required for source-level replay and retry diagnosis?
3. Do we accept a Postgres `bytea` blob backend first for atomicity, or is the
   additional object-store protocol justified immediately?
4. Which future projection is allowed to create a graph revision, and what
   source-vector/checkpoint rule bounds revision growth?

Until those decisions are made, the safe action is to continue collecting with
the existing immutable snapshots and avoid enabling the graph projector.
