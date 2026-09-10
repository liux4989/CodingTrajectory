# Canonical storage, synchronization, and live Datahub refactor

Status: initial private deployment verified, 2026-09-10. See the
[implemented workflow and deployment evidence](operations.md) and the
[later qualification checklist](later-qualification-checklist.md). The full
target design still includes capabilities beyond this first release.

## Read in this order

Start with the [operating model and delivery order](operating-model.md), approved
2026-09-10. It makes routine collection, recovery, and software releases explicit
acceptance criteria and prioritizes them ahead of larger-graph support.
The [managed collector service](managed-collection.md) and
[reproducible release workflow](releases.md) implement the operational entrypoints.
The five-step delivery has local integration evidence; activation and deployment
remain explicit operations. See [implementation status](implementation.md).

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
7. [Connections and authentication](connections.md): common profiles, role scope,
   headless agents, credential rotation, and explicit query sources.
8. [Implemented operations](operations.md): connection setup, delivery policy,
   read selection, migration, and release limitations.
9. [Upload candidate checkpoint](candidate-checkpoint.md): existing prototype,
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

## Baseline before implementation versus target

| Area | Baseline | Target |
| --- | --- | --- |
| Discovery | Source checkpoints and living deltas exist | One reusable discovery path per host |
| Construction | Affected source prefixes/graphs are rebuilt | Reusable durable canonical resources with qualified adapter-specific incremental parsing |
| Upload | Committed full-graph path; chunk/outbox candidate under qualification | Bounded initial/incremental transfer, durable offline preparation, stable service lifecycle |
| Local data | DocumentStore plus separate living/collector stores and caches | A canonical repository interface over durable versioned state |
| Shared data | Workspace SQLite Durable Object and private R2 | Indexed metadata, committed manifests, bounded resource access, retention |
| Datahub | Common query protocol; remote adapter serves frozen export | Same views backed by live committed shared data |
| Read side effects | Optional before-read publisher exists | Read-only query execution; publication is a separate command/service policy |

## Completion definition

Configure each host once. After explicitly enabling automatic publication, its
managed collector resumes after restart and temporary network loss without losing
pending work. One status command identifies the affected project and any required
action. One reproducible release workflow validates, migrates, and deploys the
compatible authority and live Datahub; ordinary publication never deploys software.

Two collector hosts can prepare and publish independent sessions, recover from
interruption without loss or duplicate effects, and see their permitted committed
data through the shared Datahub without website deployment. Local queries remain
usable offline. Equivalent sanitized revisions produce equivalent canonical
results in both adapters. Large data is bounded at preparation, transfer, commit,
and read boundaries—not just divided into smaller HTTP requests.

The spec introduces proposed internal contracts and commands explicitly marked
as such. Do not document them as available until implemented. No runtime metrics
or capacity target below is a measured production guarantee.
