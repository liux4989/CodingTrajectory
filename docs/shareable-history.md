# Shareable Historical Artifact Decision

- **Status:** Deployed to the authorized non-production target; seven-day project reads verified
- **Date:** 2026-09-05
- **Scope:** Historical collection, storage, replay, and API coverage
- **Related:** [`remote-ct-control-plane-design.md`](remote-ct-control-plane-design.md),
  [`local-collector-handoff.md`](local-collector-handoff.md)

## Decision

CodingTrajectory has one body-free historical representation for shareable
reads: `ct.shareable_graph.v1`. The originating host constructs it once from a
fenced, complete source prefix. After publication, both local and remote API
callers read that same Supabase artifact through the same handlers. APIs never
rebuild a separate local canonical inventory from logs.

The remote service does not receive raw logs, full canonical sessions, compact
compatibility sessions, or general event arrays. It does not reconstruct
graphs or recompute measurements. Evidence bodies are disabled by default.
Explicit local evidence requests first resolve a published session, then lazily
hydrate matching host evidence. HTTP requests are denied; no local API fallback
exists.

Source observations now carry checkpoint, ordering, parser, and digest metadata
only. An authenticated project collector publishes the locally assembled graph
artifacts atomically with a complete normalized source vector for those graphs.
The agent/project stream is independent from other publishers of the project;
omission from a filtered scan does not remove unrelated historical artifacts.

## Artifact boundary

The strict Pydantic artifact contains:

- graph, session, turn, item, and edge identities;
- parent/child, fork, sidechain, spawn, and edge-origin topology;
- ordering, timestamps, lifecycle status, vendor, model, and reasoning effort;
- normalized request usage, model usage inputs, runtime observations, and
  content-derived numeric measurements;
- tool name, category, outcome, exit code, and fixed verification labels;
- user-request identity and original numeric text measurements;
- portable file-change path and operation;
- compact team membership/task state; and
- bounded semantic identifiers for verification and resolution projections.

It structurally excludes:

- prompts, responses, reasoning, commands, and event payloads;
- session titles, prose previews, free-form tool descriptions, and plan text;
- tool inputs, tool outputs, tool-call transport IDs, and vendor payloads;
- raw context-source text, traces, reasons, triggers, and runtime IDs;
- source files, host locations, working directories, and absolute file paths;
- data URIs, media/blob bodies, and unbounded strings; and
- a general events collection.

User-request content is the fixed marker `[content omitted]`; its original
character/token measurements remain available. Tool descriptions accept only
`tests`, `checks`, or `command`. Titles and assistant/session previews are null,
plan actions are empty, and coverage declares `semantic_previews=false`. Both
Pydantic and SQL reject prose in these fields, even when it is short.

Portable paths and bounded identifiers remain deliberate product data. This is
a content-minimizing contract, not a claim of anonymization. Overview/summary
coverage is reduced; local evidence methods retain detailed content.

## Size and persistence bounds

One serialized graph artifact is limited to 8 MiB. One atomic project
publication is limited to 16 MiB. Both bounds are enforced by Pydantic before
queueing and by PostgreSQL before committing. A bound failure stops publication;
it is not bypassed with a weaker schema.

The persistence model stays intentionally small:

```text
ct_source_observations
  immutable source epoch/sequence/checkpoint/digest metadata

ct_artifact_revisions
  one bounded ct.shareable_graph.v1 JSONB artifact per revision

ct_artifact_revision_sources
  normalized complete source vector for each artifact revision

ct_artifact_revision_resources
  session/turn/item lookup index for targeted reads

ct_artifacts
  current revision plus small indexed inventory fields
```

JSONB remains appropriate because the artifact is one bounded, strictly
validated API document with nested order and optional fields. Relational rows
are used where the server needs joins, integrity, or lookup: source vectors,
resource ownership, project inventory, sequences, and receipts. The database
does not query arbitrary transcript JSON.

Supabase Storage, gzip object transport, generic blobs, staging tables, graph
deltas, and a replacement projector architecture are not part of this design.
They are reconsidered only if representative artifacts cannot satisfy the
explicit bound.

## Digest and replay

Canonical JSON serialization is deterministic. The content SHA-256
covers the complete artifact. Python validates the artifact, byte count, and
digest before durable queueing. PostgreSQL independently validates the exact
object shape, bounded content, canonical byte count, request digest, artifact
digest, and normalized source vector.

The artifact serializes the exact finite `cost_usd` float spelling as a bounded
decimal string, then restores it to a number before invoking shared handlers.
This avoids Python exponent formatting and PostgreSQL JSONB numeric
normalization producing different digests without changing public results.

An exact retry reuses the original serialized request and idempotency key.
Conflicting reuse is rejected. A new publication advances one agent/project-local
monotonic sequence. A source vector older than the accepted source watermarks is
recorded as superseded and never becomes current.

## API coverage

The following methods use the same shareable artifact and existing handler in
both local and remote execution:

- `project.sessions`
- `session.overview`
- `session.summary`
- `session.tree`
- `graph.overview`
- `session.stats`
- `graph.stats`
- `session.usage`
- `graph.usage`
- `session.model_usage`
- `session.request_usage`
- `session.tool_usage`
- `session.items` with `include_content=false`

Numeric usage and stats results are retained exactly. Overview, tree, summary,
tool descriptions, and metadata-only item views have reduced semantic
coverage and do not imply complete evidence. Titles and narrative previews are
unavailable in shared responses; numeric measurements are preserved.

These evidence-body requests lazily load local content only after resolving a
published scope and matching retained canonical facts. They are rejected before
HTTP historical dispatch:

- `session.search`
- `session.events`
- `session.items` with `include_content=true`
- `graph.overview` with `include:["narrative"]`

There is no legacy remote handler or parallel remote result contract.

## Transition

Existing v1 and compact-v2 observations and revisions remain immutable. They
are not deleted, rewritten, projected, or mixed into shareable history. The
migration retires unfinished legacy projector jobs, constrains all new source
observations to metadata-only checkpoints, and constrains all new artifact
revisions to `ct.shareable_graph.v1`.

The projector worker and its RPCs are removed. Remote publication stores the
locally produced artifact directly after authorization and validation.

## Deployment gate

The local privacy, topology, numeric, size, retry, and replay gates must pass
before database access. Migration deployment additionally requires explicit
confirmation that the configured Supabase target is authorized and
non-production. A failed gate stops rollout; old evidence is preserved.

## Review validation — 2026-09-05

The pre-deployment migration was exercised in isolated embedded PostgreSQL with
synthetic Auth and SHA-256 support, using only committed metric fixtures. This
also exposed and fixed a checkpoint JSON operator-precedence error and ambiguous
validator variable references that SQL parsing alone did not detect.

Observed passing scenarios: filtered publication preserves older graphs; a fresh
SQLite database resumes source/publication/living watermarks; two hosts publish
disjoint graphs into one project; incomplete and cross-host overlapping graphs
cannot overwrite history; expanding a rejected scan recovers; a lost response
retries the identical request and key; Python and SQL reject prose previews.

The four committed metric baselines pass without expected-value changes. All
four fixture graphs replay byte-identically. Stats and usage responses match;
model-usage responses differ only by the intentionally omitted titles.

These isolated checks were followed by an authorized non-production reset and
publication through hosted Auth and PostgREST. Authenticated historical reads
passed. The [rollout report](remote-ct-rollout-2026-09-05.md) records the deployed
evidence and execution-budget adjustment. Concurrent collectors and ongoing
supervision remain outside that verified scope.
