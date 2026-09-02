# Remote CT Compact Storage Proposal

- **Status:** Compact v2 boundary implemented; private-corpus validation pending
- **Date:** 2026-09-02
- **Scope:** Remote collector observations, remote content scope, and future
  storage work
- **Related:** [`remote-ct-control-plane-design.md`](remote-ct-control-plane-design.md),
  [`local-collector-handoff.md`](local-collector-handoff.md)

## Decision

Remote collection will reuse the existing `measurements` retention mode and
apply a small remote-boundary scrubber. It will not introduce a parallel
`remote_compact` canonical model.

The resulting compact observation is the remote historical representation. The
full vendor log and full-detail canonical trajectory remain on the host that
owns them. The remote service must declare `content_scope: compact`; it must
not imply that omitted content is unavailable on the originating host.

This decision changes future collection only. Existing accepted full snapshots
remain immutable while compact collection, replay, and query compatibility are
validated. It does not authorize a destructive backfill or deletion.

The implementation uses `canonical_session_snapshot.v2`, advances existing
collector sources into a new epoch, retires unaccepted local and remote v1
projection work, and validates the compact invariant again in the Python
projector. Existing v1 observations remain evidence but are never projected.

## Why now

A read-only storage measurement on 2026-09-02 found 16 accepted source
observations with 25.9 MiB of JSONB payload. The largest snapshot is
approximately 10.3 MiB; the median is approximately 527 KiB. The observation
table occupies approximately 27.0 MiB including indexes and TOAST allocation.
Every other currently created control-plane table together occupies well under
1 MiB.

The cost is therefore not the count of relational tables. It is duplicated tool
content inside each full normalized session snapshot. All 16 current
observations belong to different sources, so checkpoints, deltas, and
whole-snapshot content addressing would not reduce this baseline. They are
future-growth tools, not the first fix.

Exact, content-free comparison of linked event and item fields found 16.75 MiB
of duplicated successful tool output and 0.88 MiB of duplicated tool input:
17.64 MiB, or 68.1% of the current measured payload bytes. This is a
representation measurement, not a promise of equal disk savings after
compression.

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

## Remote compact boundary

The existing `CanonicalRetention="measurements"` mode is the base. It retains
canonical IDs, event and turn ordering, timing, status, tool identity,
item/event links, compact usage, and content-derived measurements. It drops
item `input`, `output`, `command`, `text`, and `vendor_data`; it also drops
LLM-response and `vendor.raw` events and context-source text.

`session.summary` is a read-time projection, not a storage profile. It cannot
replace compact canonical events, items, timing, or measurement facts.

Measurements retention needs only three remote-boundary additions:

| Existing behavior | Required remote rule |
|---|---|
| User-prompt payload text may remain | Retain measurements and an explicitly bounded summary only; omit full prompt bodies |
| `FileChangeItem.path` may remain | Omit host paths and private location hints |
| Team-state text may remain | Retain only an explicitly approved bounded summary or counts |

The scrubber runs after measurements retention. It is a small composition rule,
not a second retention model that can drift from the local compact path.

```text
host-local vendor log
  -> existing adapter builds full transient session
  -> CanonicalRetention = measurements
  -> remote-boundary scrubber
  -> compact canonical source observation
  -> durable receipt, ordered change log, lease update
```

| Keep remotely | Omit remotely |
|---|---|
| Stable IDs, source epoch/sequence, hashes, timing, status | Full tool inputs and outputs |
| Tool name, tool-call ID, exit code, item/event links | Commands and command output |
| Token/character measurements, compact usage, bounded approved summaries | Full assistant/reasoning text and unbounded event payloads |
| Compact file-change operation/counts, if useful | File paths, file contents, binary/media data, data URIs, and base64 bodies |
| Workspace/project IDs and liveness watermarks | `vendor_data`, context-source text, and host-local provenance |

The collection schema version must advance when this policy is implemented. The
digest continues to cover the complete compact canonical payload, so retry,
ordering, and conflict semantics remain unchanged.

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

This remains a possible second step after compact collection. It does not
remove duplicate bodies within one snapshot, and whole-snapshot deduplication
does not help the current one-snapshot-per-source baseline.

### D. Deltas plus periodic checkpoints

Store a complete checkpoint followed by validated append/delta frames, then
write another checkpoint at a defined boundary. This is the largest potential
space reduction for growing sessions, but it adds a canonical patch protocol,
base-version rules, and reconstruction failure modes. It should follow a
measured compact-payload baseline, not precede it.

### E. Retain only the latest snapshot

This is cheap but loses exact historical reconstruction. It is incompatible
with the current sequence-snapshot and evidence model, so it is rejected.

## Deferred payload backend

Content-addressed payloads are deferred until compact collection is measured.
If later justified, their public semantic value remains the canonical JSON
document, not its physical storage format.

```text
compact collector
  -> compact canonical JSON + SHA-256
  -> optional validate and compress
  -> optional immutable canonical payload blob
                  |
                  +--> source observation header, sequence, receipt, change log
                  +--> future compact graph revision header and source vector
```

If later justified, the logical schema is:

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

### Later implementation backend

Prefer a private, Postgres-backed `bytea` payload blob with explicit
compression before any object-store backend. It preserves the current RPC
transaction boundary: the ingress can validate bytes, record the immutable
blob, accept the observation, write the receipt, and advance the change log
atomically.

Supabase Storage is a later backend option, not the first move. Direct object
upload and a database commit are not one transaction; adopting it first would
require a staging/finalization protocol, orphan collection, server-side digest
verification, and recovery semantics. The proposed `storage_locator` permits
that upgrade once measured database payload growth justifies it.

### Graph projection rule

Do not enable a worker that writes a complete graph JSONB revision for every
source observation. The worker must first use compact source observations and
establish a durable source vector for every compact graph revision.

Before choosing graph checkpoints or deltas, measure these two quantities on a
representative private corpus:

```text
compact graph bytes per accepted source update
delta bytes / compact checkpoint bytes
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
is compact collection, not compression or history removal.

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

## Validation and rollout

### Phase 1 — compact collection (implemented)

1. Select existing measurements retention before remote serialization.
2. Apply the remote-boundary scrubber and publish a new compact schema version.
3. Keep the existing full local trajectory and vendor log untouched.
4. Measure aggregate payload size during private-corpus validation without
   recording content, paths, identifiers, or prompts.

### Phase 2 — compatibility evidence

1. For a private representative corpus, compare local full and remote compact
   results for summaries, graphs, statistics, and usage methods.
2. Confirm `session.events` and `session.items` return retained compact evidence
   through the same handlers and contracts as local reads.
3. Confirm exact retry, source epoch rollover, and change-log ordering with the
   compact payload digest.
4. Confirm no host paths, file contents, data URIs, base64 bodies, or tool
   bodies are present in the outbound compact payload.

### Phase 3 — reassess storage backend

Only after Phase 2, measure compact-payload size distribution and repeat-source
versions. Propose compression, content-addressed blobs, checkpoints, or object
storage only if their measured benefit outweighs their transaction and replay
complexity.

## Acceptance gates

Implementation is ready only when:

1. The compact payload validates against a versioned Pydantic contract.
2. Full tool bodies, command bodies, paths, media-like bodies, and unbounded
   event payloads are absent from outbound remote observations.
3. Summary and metric results have documented compact compatibility evidence.
4. All historical methods remain callable and identify compact coverage without
   silently hydrating omitted bodies.
5. Existing source ordering, idempotency, receipts, and lease semantics pass
   unchanged.
6. No existing remote payload is deleted or rewritten as part of rollout.

## Open decisions after compact validation

1. Which bounded summaries, if any, are useful remotely and what are their
   maximum character/token limits?
2. What compact-size distribution and repeat-version rate justify a compressed
   blob backend?
3. What retention period is required for compact source observations and
   operational receipts?
