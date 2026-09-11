# Data and protocol contracts

This document specifies semantics. New names and wire structures are proposed
until the corresponding Pydantic models and generated ingress schemas land.
Existing `ct.core.v1` and `ct.datahub.v1` public envelopes remain authoritative.

## Identity and revision vocabulary

| Identity | Meaning |
| --- | --- |
| workspace_id | Authorization and shared commit boundary |
| project_id | Portable project identity; aliases/display name are metadata |
| collector_id / existing agent_id | Stable collector installation identity; not one coding session |
| agent_instance_id | One running collector incarnation; changes on restart where lease fencing requires it |
| source_id, source_epoch | Stable source lineage and explicit replacement/reset fence |
| session_id, turn_id, item_id | Existing canonical domain identities |
| local_revision | Host repository revision; never compared numerically across hosts |
| consumed_cursor | Changes durably captured in the outbox |
| acknowledged_position | Captures whose publication receipt is durably recorded |
| publication_revision | Shared historical commit order; unaffected by heartbeats or staging |
| project_metadata_revision | Project registration/name metadata order; independent of publication |
| batch_id | Stable frozen publication identity for retries |
| content_sha256 | Immutable canonical bytes or chunk identity |

A cursor binds source/workspace, method, filters, revision, schema/projection
version, and pagination position. Source liveness uses its own evaluation time.
Do not reuse a transport cursor as source-file offset or remote publication order.

The accepted [live API design](live-api-design.md) replaces ordinary arbitrary
history selection with a 30-minute fixed read selection. `ct_catalog_read_v2`
selects publication and project-metadata heads atomically and returns a token
bound to the authenticated workspace/principal and authority incarnation. Its
server-held cursors additionally bind kind, population, normalized filters,
include variant and position. Token possession does not bypass authorization.
Refresh selects latest; expiry/reset never silently resumes at latest.

V2 supports metadata-only status, identity-paged registered/published projects and
session summary pages with all four include variants. It rejects explicit
`snapshot_sequence`; legacy RPCs retain their existing semantics during migration.
The two revision values currently use independent maxima in the existing commit
sequence space: they are not interchangeable counters. Estimation/workspace
fences and living evaluation remain separate.

Payload GC is not enabled. Current v2 retained floors are zero because existing
history is retained; coverage of observed sources remains explicitly unknown.
Accepted future retention keeps current data, live pins, durable pending work,
required estimation evidence and seven days of rollback after legacy cutover.
This policy does not authorize deletion before its qualification gates pass.

## Logical host schema

- `sources`: identity, epoch, complete-record offset, size/checksums, parser version.
- `resource_versions`: kind/ID, local revision, payload reference, lineage, tombstone.
- `changes`: ordered local revision/change ID and affected resource identities.
- `canonical_manifests`: graph/session revision, membership, dependency references.
- `captures`: immutable selected canonical revision and publication scope.
- `outbox_batches`: batch ID, source vector, root digest, schema, state, attempts,
  next retry time, and prepared payload references.
- `delivery_receipts`: batch identity, committed revision, response digest.
- `sync_state`: consumed cursor, acknowledged position, mode, and local owner lock.

Source checkpoint advancement and its canonical updates/change journal must commit
atomically. Capture creation and consumed-cursor advancement must commit atomically
in the outbox store. A cursor may advance before remote ACK only when all required
bytes/references are durably retained locally. Database recreation must not reuse
an old cursor secret or pretend it has the former source history.

## Logical shared schema

- Collector principals and owned source/session lineage.
- Immutable chunk descriptors: workspace, digest, encoding, lengths, validation version.
- Candidate manifests and verified membership, invisible to ordinary readers.
- Committed artifact/session versions: identity, owner/epoch, revision, root digest.
- Published changes indexed by `(publication_revision, change_id)`.
- Project/session read indexes by project, vendor, activity time, resource ID.
- Frozen idempotency receipts and collector recovery watermarks.
- Current/versioned leases with separate observed and expiry times.

Indexes must support keyset paging and resource-scoped lookups. Retain validity
intervals or equivalent immutable version selection for live bounded selections
and explicitly retained evidence, not indefinite ordinary reconstruction history.

## Canonical chunk manifest

A manifest binds workspace/project, owning collector, source vector, canonical
schema, root identity, expected base revision, root chunk digest, referenced chunk
membership, bounded size/count metadata, and projection version. Root or manifest
hash covers dependency membership; a caller cannot attach an arbitrary chunk to
an authorized graph. Chunk reads verify membership in the requested committed
revision and never expose raw R2 keys.

Use stable semantic resource IDs and bounded resource/array blocks so appending
or inserting an item does not rechunk an entire session unnecessarily. Keep small
metadata, completed history blocks, active resource blocks, and topology references
separately. Corrections replace affected immutable nodes. New root references
reuse unchanged nodes. Preserve canonical decimal/number encoding and digest
semantics; cross-language serialization must not change identity.

Budget every request, decompressed node, resource count, graph depth, dependency
fan-out, response, and validation task. Reject cycles, missing dependencies,
digest mismatches, forbidden fields, and unknown versions before publication.
The first compatibility release retains the existing 8 MiB whole-graph ceiling;
chunk transport must not claim larger-graph support. Supporting larger graphs
requires bounded manifest-native validation and resource-paged reads in phase R5.

## Publication semantics

The safe unit is an explicitly scoped canonical revision with all its required
dependencies. A batch may carry several independent revisions within a bounded
commit budget. Do not promise all-host/global atomic publication. Large work is
split explicitly; each receipt identifies exactly what became visible.

Publication upserts only explicitly named owned resources. Historical deletions
require explicit fenced tombstones or a separately declared complete replacement
scope. An omitted resource in a partial batch is not a deletion. A resource
leaving the living inventory is not a historical tombstone.

Each session/source lineage has one authoritative writer. Replicas with the same
canonical IDs must deduplicate identical content or report an ownership conflict.
Transfer requires an explicit compare-and-swap ownership epoch. A host publishing
its sessions cannot replace another host's sessions in the same project. Missing
cross-host dependencies remain pending or explicitly partial; never invent graph
closure or accept a falsely complete manifest.

Commit verifies ownership, expected base/source fences, schemas, payload readiness,
and idempotency, then atomically updates visible revisions/indexes/change feed and
stores the receipt. Same batch + same content returns the same effect/receipt;
same batch + changed content conflicts. A checkpoint ACK or staging ACK does not
mean a historical revision is published.

## Internal upload and read operations

Candidate upload operation names are owned by the upload agent; freeze their
Pydantic definitions before integration. Required operations: negotiate missing
nodes, upload bounded nodes, validate/stage manifest, commit revision, recover
receipt/watermarks. Authorization is enforced at every boundary.

The agent has proposed additive read methods:

| Method | Required result |
| --- | --- |
| `ct_publication_watermark` | Workspace/project scope plus latest historical publication revision, optionally pinned |
| `ct_artifact_chunk_manifest` | Workspace/artifact identity, selected snapshot and artifact revisions, owner/source provenance, canonical/root hashes, versions, transport kind |
| `ct_artifact_chunks` | Bounded nodes authorized by membership in that committed manifest |

Existing gzip readers continue to work within their size budget during migration.
New readers negotiate transport/schema support. Unsupported versions fail explicitly;
no silent downgrade that loses semantics. Add a publication change-feed operation
before enabling Datahub delta refresh; a watermark alone cannot identify changes.
