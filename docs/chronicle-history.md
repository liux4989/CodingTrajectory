# Chronicle Operational History Contract

- **Status:** Locally qualified; admitted to the designated disposable non-production project; production not deployed
- **Date:** 2026-09-09
- **Scope:** Historical collection, storage, replay, and API coverage
- **Related:** [`remote-ct-control-plane-design.md`](remote-ct-control-plane-design.md),
  [`local-collector-handoff.md`](local-collector-handoff.md)

## Decision

CodingTrajectory uses one private, bounded operational history document:
`ct.chronicle_graph.v2`. The originating host builds it from a fenced complete
source prefix. Local Chronicle reads and remote Chronicle reads execute the same
handlers over that exact schema; only source selection and provenance differ.
V2 is intentionally sparse on the wire: zero measurements, false flags, empty
semantic objects, and tool names recoverable from their normalized summary are
omitted and restored by the Pydantic model on read. The disposable deployment
does not accept or translate Chronicle v1 artifacts.

Chronicle is not a public-sharing format. A future public export must be a
separate, more restrictive projection. The old `ct.shareable_graph.v1` name and
Python compatibility aliases are intentionally unsupported in this prototype.

Raw vendor logs remain host-local authority. Explicit local evidence methods use
the full canonical graph without publishing evidence bodies. Chronicle receives
only the bounded operational document and never reconstructs it from uploaded
events.

## Retained operational facts

The strict Pydantic and PostgreSQL contracts retain:

- graph, session, turn, item, request, and edge identities;
- topology, ordering, timestamps, lifecycle status, vendor, model, and effort;
- request usage, runtime observations, and numeric content measurements;
- normalized tool summary, outcome, optimization profile, exit code, and
  verification kind;
- portable file-change paths and operations;
- bounded team membership and task state;
- sanitized operational tool details;
- bounded user-request and assistant-response previews; and
- explicit parent-item and nested-index provenance for projection-only children.

An operational tool detail is a typed capsule with `kind`, `target`, optional
`scope`, and `safety="sanitized"`. Its target is limited to 280 characters.
Repository paths become relative to the observed working directory; unrelated
absolute paths are reduced to portable suffixes. URL credentials, queries, and
fragments are removed. Recognizable secret assignments and values are redacted.
The capsule preserves useful distinctions such as `ReadFile: docs/prd.md`, a
search pattern and scope, or a bounded command signature.

## Excluded evidence

Chronicle structurally excludes:

- complete user, assistant, and reasoning bodies beyond their bounded previews;
- complete shell commands, tool inputs, and tool outputs;
- general event arrays and vendor payloads;
- session titles, plan text, and traces;
- source files, working directories, and host-local absolute paths;
- credentials, URL query strings, data URIs, media, and blob bodies; and
- unbounded strings.

User-request content and assistant-response text are retained as previews of at
most 280 characters. Their original character and token measurements may also
remain. Titles are null, plan actions are empty, and coverage declares
`operational_details=true`, `content=false`, and `events=false`; `content=false`
means that complete evidence bodies are unavailable, not that Chronicle lacks
narrative previews. This is bounded private product data, not an anonymization
claim.

## Expanded execution accounting

Codex `exec` wrappers remain the sole owners of provider-visible input/output
measurements and allocated usage. Their reconstructed typed children are marked
`projection_only` and identify the wrapper by canonical parent item UUID.

Chronicle stats do not count those children as additional content. Instead, the
wrapper's integer token, character, and allocated-usage totals are partitioned
deterministically across the child concepts, weighted by their observed child
sizes. Largest-remainder allocation preserves every parent total exactly. This
restores categories such as files read, searches, edits, and command output
without double-counting the wrapper.

## Bounds and persistence

One canonical graph artifact is limited to 8 MiB and one atomic project
publication to 16 MiB. A bound failure stops publication; it does not fall back
to a weaker schema. Canonical JSON and SHA-256 make retries and replay
deterministic.

```text
ct_source_observations
  immutable source checkpoint and digest metadata

ct_artifact_revisions
  revision visibility plus temporary rollback JSONB

ct_artifact_payloads
  content-addressed Zstandard canonical documents

ct_artifact_read_projections
  small versioned publication-time read projections

ct_artifact_revision_sources
  normalized complete source vectors

ct_artifact_revision_resources
  session, turn, and item lookup rows

ct_artifacts
  current revision and small inventory fields
```

JSONB remains appropriate for small variable envelopes and metadata checkpoints,
not for hot-path aggregation of complete Chronicle graphs. Relations remain the
authority for integrity, lookup, ownership, sequences, and receipts. Compressed
payloads preserve exact replay while versioned read projections avoid loading
the canonical document for supported collection queries. Graph deltas and a
remote projector remain outside this design.

## API coverage

The following methods execute through the same Chronicle contract locally and
remotely:

- `project.sessions`
- `session.overview`, `session.summary`, and `session.tree`
- `graph.overview` without narrative
- `session.stats` and `graph.stats`
- `session.usage`, `graph.usage`, `session.model_usage`,
  `session.request_usage`, and `session.tool_usage`
- `session.items` with `include_content=false`

Evidence-body requests remain local-only:

- `session.search`
- `session.events`
- `session.items` with `include_content=true`
- `graph.overview` with `include:["narrative"]`

Remote routing rejects these requests instead of returning partial evidence.
Response metadata reports the selected source.

## Deployment boundary

The contract is qualified locally and was admitted to the designated disposable
non-production project on 2026-09-09; production remains undeployed. Before any
further remote change, re-validate the Python and SQL shapes, exact digest and
size behavior, real-session operational output, metric reconciliation, and
collector replay locally. Remote target classification and explicit deployment
authorization remain a separate gate. Git history retains the dated
qualification, readiness, and rollout evidence.
