# RFC: Evidence, Canonical, Publication, and Query Authorities

- **Status:** Accepted
- **Date:** 2026-09-16
- **Scope:** Historical ingestion, Chronicle publication, local queries, and remote queries

## Decision

CodingTrajectory separates historical processing into four authorities. Each
authority owns one kind of truth and validates its output before the next
authority consumes it.

```text
immutable provider records
  → source evidence authority
  → canonical reconstruction authority
  → publication authority
  → local or remote fact repository
  → shared historical query semantics
```

The boundaries are intentionally asymmetric. Information may be normalized or
removed while moving right, but a downstream layer must not reinterpret or
repair an upstream layer's meaning.

## Authorities

### 1. Source evidence authority

Provider logs and their occurrence index are the record of what was observed.
This layer preserves source order, identity, location, digest, parse failures,
and rejection provenance before filtering or deduplication. Raw records remain
local and are not a remote schema or backup payload.

### 2. Canonical reconstruction authority

Ingestion converts occurrences into `DocumentStore`/`SessionGraph` resources.
It alone owns inherited-history classification, revision reconciliation,
deduplication, parent/child linkage, item/event linkage, orchestration edges,
and exact pre-retention accounting. Observed source occurrences remain distinct
from inferred canonical facts.

Publication must not compensate for incorrect reconstruction. Metric
expectations are reconstructed from committed source evidence rather than copied
from current program output.

### 3. Publication authority

`published_fact_set_for_store(DocumentStore)` projects canonical graphs directly
into a bounded `PublishedFactSet`. This boundary owns:

- privacy and allowlist projection;
- typed identities, references, ordering, and coverage;
- row, graph, publication, and page bounds;
- deterministic hashes and fact-set digests; and
- normalized, body-free tool and event evidence.

It does not parse provider records, redo ownership or deduplication, or invent
missing measurements. Unavailable evidence remains unavailable rather than
becoming zero. Raw transcripts, prompts, reasoning, command arguments, tool
input/output, event payloads, arbitrary vendor data, secrets, and host paths do
not cross this boundary.

### 4. Query authority

Local and remote `FactRepository` implementations provide the same bounded fact
model to the same Python handlers. Query storage owns selection, authorization,
snapshot pinning, version validity, filtering, and paging. It does not own
summary, overview, search, or metric semantics.

The remote Durable Object is authoritative for facts published at a workspace
sequence. It is not authoritative for raw evidence or canonical interpretation,
and it is rebuildable from retained local sources.

The current Python query bridge reconstructs a bounded `SessionGraph` from
validated facts so existing shared handlers retain one meaning. This is a
transitional implementation boundary, not a claim that reconstructed canonical
stores are the final or smallest read model. A future typed `FactIndex` may
replace the bridge only if the same shared handlers consume it without parallel
fact-specific or TypeScript summary semantics.

## Why the former remote was “only a copy”

“Copy” described its architectural role, not necessarily byte-for-byte storage.
The former path serialized the locally constructed graph into a compressed
artifact, chunked or projected the same snapshot into several storage shapes,
then downloaded and reconstructed a `DocumentStore` for Python handlers. SQL
could query catalog and projection metadata, but the historical facts remained
inside a whole JSON artifact.

Consequently, the former remote layer:

- did not select sessions, turns, items, or events as typed facts;
- could not validate all fact relationships independently before commit;
- duplicated one graph as artifact, chunks, manifests, and projections;
- paid whole-artifact reconstruction cost for ordinary historical reads; and
- differed from local behavior whenever an API needed bodies omitted from the
  uploaded artifact.

It was therefore a remote persistence copy of a local read model, not a
queryable historical fact authority. The new remote is still **derived**—it does
not replace local evidence—but it has a distinct responsibility: maintain and
serve validated, versioned, bounded publication facts.

## Boundary invariants

1. **Preserve before interpreting.** Source occurrences are identified before
   filtering, slicing, reconciliation, or retention.
2. **Derive once.** Ownership, deduplication, linkage, and accounting are
   canonical-ingestion responsibilities. Publication only projects them.
3. **Fail closed.** Unknown tool evidence retains bounded facts or nothing; it
   never falls back to arbitrary previews or arguments.
4. **Validate semantics, not only hashes.** Payload identity, ownership,
   ordering, references, and edge evidence must agree before publication.
5. **Bound before materializing.** SQL selection limits apply before payloads
   enter Worker memory; encoded row, graph, publication, and response limits
   apply before commit or response construction.
6. **Bind continuations to authority.** Cursors authenticate workspace,
   snapshot, selector, kinds, and continuation position.
7. **Use one public meaning.** Local and remote standard methods have the same
   response shape and coverage semantics. Raw diagnostics, if ever added, are a
   separate local-only surface.
8. **Keep authorities separate.** Historical facts, living observations, and
   local/Core estimation do not share storage authority merely because they
   share a protocol registry.

## Ownership and change rules

| Boundary | Primary owner | Changes that require cross-boundary review |
| --- | --- | --- |
| Source records → canonical graph | ingestion and discovery | IDs, ownership, deduplication, linkage, measurements |
| Canonical graph → published facts | fact projection and published facts | retained fields, coverage, privacy, bounds, references |
| Fact publication → remote SQL | collector and Cloudflare authority | atomicity, validity ranges, cursors, authorization, limits |
| Facts → public responses | contracts and shared handlers | method versions, paging, filtering, response semantics |

Source-backed validation owns expected structural and metric truth. Publication
and query validation may adapt invocation to a new contract, but must not update
expected values from current output.

## Operational consequences

- The clean-break schema has no artifact-era migration or compatibility path.
  A deployment must use fresh remote state or an explicitly approved reset.
- Remote facts are not a raw-log backup. Deleting the only retained provider
  source can make later republication impossible.
- Code acceptance and deployment qualification are separate. Synthetic local
  qualification can establish contract correctness without authorizing remote
  writes or private-data upload.
- Cursor signing requires a server-only `CT_CURSOR_KEY`. Key rotation invalidates
  outstanding cursors, whose callers restart from a pinned snapshot.
- Disabled workflows, deployment, shared-state writes, and evidence upload are
  operator decisions, not consequences of merging code.

## Related documents

- [Architecture](architecture.md)
- [Chronicle historical facts contract](chronicle-history.md)
- [Direct Published Facts design](refactor/direct-published-facts.md)
- [Published Fact Sets refactor](refactor/remote-historical-facts.md)
- [Cloudflare control plane](remote-ct-control-plane-design.md)
- [Metrics validation quality gate](metrics-validation-quality-gate.md)
