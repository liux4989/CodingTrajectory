# Direct Published Facts

- **Status:** Implemented
- **Depends on:** [Authority boundaries RFC](../authority-boundaries.md)
- **Decision type:** Clean-break internal architecture; no compatibility path

## Problem

The current fact implementation no longer stores a remote Chronicle artifact,
but it still constructs `ChronicleGraphArtifact` between the canonical graph and
`PublishedFactSet`:

```text
SessionGraph
  → ChronicleGraphArtifact
  → PublishedFactSet
  → ChronicleGraphArtifact
  → SessionGraph
```

The aggregate is not required for remote persistence compatibility. It remains
an internal intermediate that combines four responsibilities:

1. projecting canonical resources into bounded fields;
2. validating hierarchy and reference integrity;
3. serving as the collector's local normalization cache value; and
4. reconstructing a handler-ready `SessionGraph` from facts.

This duplicates the hierarchy already represented by fact rows, requires
parallel artifact and fact validators, and makes the source-to-publication seam
look like another authority. It also leaves `ct.chronicle_graph.v3` in the Core
protocol snapshot even though `ct.published_facts.v1` is the actual local and
remote historical contract.

## Decision

Remove `ChronicleGraphArtifact`, its schema, constructors, serializers, inverse
conversion, cache use, and qualification surface. Build and reconstruct
`PublishedFactSet` directly:

```text
immutable source records
  → canonical SessionGraph
  → build_published_fact_set(SessionGraph)
  → PublishedFactSet
  → session_graph_from_fact_set(PublishedFactSet)
  → shared historical handlers
```

There is one publication contract and one semantic validator. No artifact alias,
adapter, dual read, dual write, migration, or deprecated entry point remains.

Implementation validation retained `ct.published_facts.v1`: direct projection
produces the same canonical rows, row hashes, and fact-set digests as the former
two-step projection. The collector's disposable normalization cache now stores
canonical `Session` JSON and source checkpoints use its canonical digest; neither
is a publication contract or remote historical payload.

## Target modules and ownership

### Canonical ingestion

Ingestion/discovery exposes one canonical segment-normalization function that
returns `Session`, not a publication model. It owns adapter invocation,
stabilization, segment merging, inherited-history inputs, and canonical IDs.

The collector may keep a bounded owner-only normalization cache, but its cached
value is a canonical `Session`. Cache persistence and eviction remain disposable
local acceleration. Publication types must not be used to perform or cache
canonical reconstruction.

### Fact projection

`control_plane/fact_projection.py` owns the one-way, privacy-sensitive mapping:

```python
def build_published_fact_set(graph: SessionGraph) -> PublishedFactSet: ...
```

It constructs graph/session/turn/item/event/edge/request/model/runtime/
measurement/output-evidence rows directly from canonical resources. It owns:

- bounded request and narrative previews;
- portable path and structural command projection;
- normalized event envelopes and tool-output evidence;
- exact measurement conversion and coverage; and
- deterministic fact IDs, row hashes, ordering, and set digest.

Unknown source semantics are never inferred here. The projector consumes only
canonical fields produced by ingestion.

### Published fact contract

`control_plane/published_facts.py` owns the typed row payloads,
`PublishedFactSet`, semantic relationship validation, byte/cardinality bounds,
and direct inverse reconstruction:

```python
def session_graph_from_fact_set(facts: PublishedFactSet) -> SessionGraph: ...
```

The existing bounded payload helper class titles remain internal implementation
names in this cutover because they are not aggregates and changing JSON Schema
definition titles would add unrelated protocol churn. The JSON field spelling
remains the deliberate fact contract; no helper is serialized outside a fact row.

Inverse reconstruction is a transitional bridge for existing shared handlers.
It indexes rows by kind and ownership and builds canonical
`Session`, `Turn`, `Item`, `Event`, and `SessionEdge` values directly. It never
constructs a second graph-shaped publication object. It is intentionally
isolated so a later non-semantic typed `FactIndex` can let shared Python handlers
consume indexed rows without parallel handler semantics or TypeScript summaries.

### Repository and collector

`published_fact_set_for_store` calls `build_published_fact_set` directly.
`document_store_from_fact_sets` calls `session_graph_from_fact_set` directly.
The collector computes publication digests, queues rows, and stages them without
constructing an artifact.

`FactRepository` remains the local/remote historical boundary. Shared handlers
continue to consume the same reconstructed canonical store, so removing the
intermediate does not create separate local and remote semantics.

## Contract decision

`ct.published_facts.v1` remains the wire schema only if the serialized fact rows
are byte-for-byte unchanged. Internal Python class-title changes do not require a
wire version change. Any JSON field, meaning, requiredness, identity, or digest
change requires an explicit new fact schema version.

`ct.chronicle_graph.v3` is removed from `validation/core-protocol.json` rather
than retained as a deprecated schema. The protocol freeze records the published
fact schema and public service methods. No `ChronicleGraphArtifact` parser or
version alias remains.

Chronicle remains the product term for bounded historical facts; it is not a
second graph model or storage format.

## Invariants moved to the fact boundary

Before the aggregate is deleted, every invariant it enforces must have one
fact-native owner:

| Existing responsibility | Target owner |
| --- | --- |
| Retained root and graph counts | `PublishedFactSet` semantic validator |
| Session/turn/item/event identity and ordering | Fact row and relationship validator |
| Projection parent/nested ownership | Item fact validator |
| Item/event and output-evidence references | Relationship validator |
| Spawn and edge origin ownership | Relationship validator |
| Duplicate edge identities | Relationship validator |
| Embedded-body rejection | Fact payload policy validator |
| 16 MiB graph bound | Canonical serialized `PublishedFactSet` bound |
| Sparse zero/default removal | Canonical fact-row serializer |
| Source-to-bounded conversion | `fact_projection.py` |
| Facts-to-canonical conversion | `session_graph_from_fact_set` |

The implementation migration is explicit:

| Former aggregate check | Fact-native enforcement |
| --- | --- |
| Non-empty graph, retained root, duplicate session/turn/item/event IDs | Required graph row, retained root session, unique `(kind, fact_id)` rows |
| Graph session/turn/item counts | Graph payload counts compared with typed row counts |
| Sorted and unique event/turn/item sequences | Unique sibling payload sequences plus `order_index == sequence`; rows remain canonically sorted by kind/ID |
| Event → turn/item ownership and item → event references | Bidirectional event/item checks against retained parent rows |
| Projection parent and nested-index rules | Item-row sibling ownership, canonical-parent, and projection-only checks |
| Output evidence event subset | Evidence source IDs must be a subset of the owning item's event IDs and retained event rows |
| Spawn origin turn/item ownership | Session topology origins checked against retained owned turn/item rows |
| Edge endpoints, origin ownership, event evidence, duplicate identity | Edge relationship validator over retained session/turn/item/event rows |
| Embedded body/path/base64 rejection | Complete sparse fact-set policy validation after row semantics |
| 16 MiB graph, 512 KiB row, 96 MiB publication | Fact-set validator, row validator, and collector/Worker publication validators respectively |
| Sparse defaults and deterministic digest | Fact-row `exclude_none` canonical JSON, row hashes, canonical row order, and set digest |

Client and Cloudflare validators continue to enforce matching relationship,
identity, ordering, and bound rules before publication.

## Collector redesign

Remove:

- `_CollectedSource.artifact`;
- `_normalized_cached(...) -> ChronicleGraphArtifact`;
- `_normalized_segments(...) -> ChronicleGraphArtifact`;
- `build_chronicle_segments`;
- artifact JSON in `normalization_cache`; and
- artifact round trips used to recover one canonical session.

Replace them with ingestion-owned canonical session normalization and a local
`Session` cache. `_CollectedSource` carries `session: Session`. Project graph
assembly consumes those sessions directly, and publication calls
`build_published_fact_set(graph)` exactly once per selected graph.

The cache is not a new authority. It is keyed by the complete source fence and
parser version, owner-only, size-bounded, disposable, and invalidated on parse
failure or version change. If retaining canonical bodies in this cache is not
acceptable, remove persistent normalization caching and reparse; do not insert a
publication DTO as a privacy workaround.

## Deletions

- `ChronicleGraphArtifact`
- `CHRONICLE_GRAPH_SCHEMA_VERSION`
- `build_chronicle_graph_artifact`
- `build_chronicle_segments`
- `chronicle_session_graph`
- `PublishedFactSet.to_artifact`
- `derive_published_fact_set`
- artifact `wire_payload`, `canonical_bytes`, and `digest`
- `ct.chronicle_graph.v3` protocol snapshot
- artifact-oriented fixtures and qualification assertions

The bounded field converters currently in `chronicle.py` move to
`fact_projection.py`; inverse converters move to `published_facts.py`. Delete
`chronicle.py` when no imports remain. Do not leave re-export aliases.

## Implementation sequence

This is one clean cutover, implemented in reviewable commits without shipping a
dual path:

1. **Fact-native reconstruction.** Add `session_graph_from_fact_set` and prove
   it reproduces current fact behavior without calling `to_artifact`.
2. **Direct projection.** Add `build_published_fact_set(SessionGraph)` and prove
   canonical fact bytes/digests match the current accepted contract.
3. **Collector ownership fix.** Return/cache canonical `Session` values and
   remove publication types from source normalization.
4. **Cut over callers.** Update repository, collector, scripts, and qualifiers
   to use the direct functions.
5. **Delete the aggregate.** Remove artifact types/functions, protocol snapshot,
   imports, fixtures, and documentation in the same branch.

Intermediate commits may preserve behavior for review, but the branch is not
complete while both paths exist.

## Qualification

Required focused evidence:

1. For every committed source-backed fixture, direct projection produces the
   expected fact rows, public historical responses, and metrics.
2. An asymmetric synthetic graph exercises every fact kind, cumulative/current
   usage, projection children, event envelopes, output evidence, runtime,
   topology, and edge origins.
3. `SessionGraph → PublishedFactSet → SessionGraph → PublishedFactSet` preserves
   canonical fact bytes and digest.
4. Local and remote repositories return identical responses from the same fact
   rows.
5. Adversarial relationship, ordering, body, secret, and size mutations fail at
   the fact boundary before publication.
6. Collector source fences, deterministic reparsing, graph assembly, and
   publication digests remain deterministic.
7. Repository-wide search finds no aggregate symbols, schema version, artifact
   cache value, or compatibility alias.

Existing Cloudflare scale/deployment qualification need not be repeated when
fact wire bytes are proven unchanged and no Worker code changes. Any fact wire
change reopens client/server schema, digest, bound, and remote qualification.

## Follow-up boundary

The follow-up is implemented in [Indexed Historical Facts](fact-index-read-view.md).
Repositories now retain only a non-semantic typed `FactIndex`; the specialized
local availability/batch subclass and whole-store reconstruction are gone.
Selected graphs are materialized transiently only for the existing shared
semantic handlers that still consume canonical models.

## Risks and safeguards

- **Lost validation during deletion:** use an explicit invariant matrix and
  adversarial mutations before removing the aggregate validator.
- **Digest drift:** compare canonical row bytes and fact-set digest before and
  after direct projection; do not update expected digests from new output alone.
- **Sensitive local cache expansion:** persistent normalized-session caching was
  removed after measured owner-local runs showed no reuse.
- **Accidental semantic derivation in publication:** projection helpers accept
  canonical models only and never provider records.
- **Another replacement aggregate:** reject any new graph-shaped DTO between
  `SessionGraph` and `PublishedFactSet`.

## Non-goals

- Changing public historical method semantics
- Reintroducing raw local diagnostics
- Migrating artifact-era remote state
- Moving summary or metric semantics into TypeScript
- Deploying Cloudflare or uploading real/private data
- Replacing canonical ingestion models
