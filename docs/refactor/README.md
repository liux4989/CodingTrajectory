# Canonical storage, synchronization, and live Datahub refactor

Status: implementation baseline, 2026-09-10. This is the target design, not a
claim of deployed functionality. The upload agent's uncommitted candidate is
paused for reconciliation with this specification. Current services remain in
place until the migration gates pass.

## Read in this order

1. [Architecture and decisions](architecture.md): responsibilities, authorities,
   deployment topology, and scope.
2. [Data and protocol contracts](contracts.md): identity, storage, versioning,
   manifests, revisions, and public/internal API boundaries.
3. [Synchronization and recovery](synchronization.md): preparation, batching,
   delivery modes, crash recovery, and operational control.
4. [Local and shared reads](read-path.md): query execution, canonical projections,
   lazy loading, source selection, and honest capability/freshness presentation.
5. [Implementation and migration](implementation.md): dependency-ordered work,
   module ownership, compatibility, rollout, and rollback.
6. [Acceptance and qualification](qualification.md): invariant-driven checks and
   evidence required before each stage is complete.
7. [Upload candidate checkpoint](candidate-checkpoint.md): existing prototype,
   reported checks, and unresolved integration work.

The existing [live-read outline](../datahub-live-read-flow.md) is a summary.
This package governs the refactor; existing architecture and control-plane docs
continue describing the current implementation until each stage lands.

## Agreed decisions

- One canonical domain model and common query semantics for local and shared use.
- Host source files remain evidence authority. Only the approved shareable
  representation leaves a host.
- Discovery, canonical construction, upload, and presentation are separate layers.
- Canonical changes are persisted before waiting for an upload batch to fill.
- Send bounded changes; reuse unchanged chunks and freeze each attempted batch.
- Publish complete revisions atomically; partially uploaded data is invisible.
- Recover with durable checkpoints and idempotent replay after interrupted work.
- Support manual and automatic publication. Default to manual; installation,
  restart, and an ordinary query must not activate uploads automatically.
- Local and remote Datahub use the existing query envelopes; data publication
  does not rebuild or deploy the website.
- No implicit union of local and shared records. A selected source is explicit.

## Current state versus target

| Area | Current state | Target |
| --- | --- | --- |
| Discovery | Source checkpoints and living deltas exist | One reusable discovery path per host |
| Construction | Affected source prefixes/graphs are rebuilt | Reusable durable canonical resources with qualified adapter-specific incremental parsing |
| Upload | Committed full-graph path; chunk/outbox candidate under qualification | Bounded initial/incremental transfer, durable offline preparation, stable service lifecycle |
| Local data | DocumentStore plus separate living/collector stores and caches | A canonical repository interface over durable versioned state |
| Shared data | Workspace SQLite Durable Object and private R2 | Indexed metadata, committed manifests, bounded resource access, retention |
| Datahub | Common query protocol; remote adapter serves frozen export | Same views backed by live committed shared data |
| Read side effects | Optional before-read publisher exists | Read-only query execution; publication is a separate command/service policy |

## Completion definition

Two collector hosts can prepare and publish independent sessions, recover from
interruption without loss or duplicate effects, and see their permitted committed
data through the shared Datahub without website deployment. Local queries remain
usable offline. Equivalent sanitized revisions produce equivalent canonical
results in both adapters. Large data is bounded at preparation, transfer, commit,
and read boundaries—not just divided into smaller HTTP requests.

The spec introduces proposed internal contracts and commands explicitly marked
as such. Do not document them as available until implemented. No runtime metrics
or capacity target below is a measured production guarantee.
