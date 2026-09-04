# Remote CT Control Plane Design

- **Status:** Shareable historical path implemented locally; deployment pending
- **Date:** 2026-09-05
- **Scope:** Public method authorities, historical artifacts, project inventory,
  living state, estimation, and collector handoff
- **Supersedes:** [`remote-session-ledger-design.md`](remote-session-ledger-design.md)

## Decision

CodingTrajectory has one public API contract and one shared historical handler
implementation. Local and remote shareable calls consume the same
`ct.shareable_graph.v1` artifact. Artifact schema changes do not create a new
public API version while response contracts remain compatible.

Detailed evidence APIs stay local. They continue to hydrate the existing raw
log and full canonical trajectory and do not depend on an uploaded artifact.

## Authorities

| Authority | Public methods | Durable state |
|---|---:|---|
| Historical | 15 project/session/graph methods | Source checkpoints, shareable artifact revisions, normalized source vectors |
| Project inventory | `project.list` | Portable projects, revisions, aliases, private agent locations |
| Living | `living.sessions`, `living.events` | Agent leases and ordered living observations |
| Estimation | Seven `estimate.*` methods | Forecast events, jobs, attempts, leases, and results |

Collector SQLite is not an authority. It stores only delivery state. Raw vendor
logs remain authoritative evidence on their originating host.

## API boundary

Shareable local and remote methods use the same artifact and handler:

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

Local-only evidence methods are:

- `session.search`
- `session.events`
- `session.items` with `include_content=true`

Remote routing rejects those methods explicitly before dispatch. It does not
return a partial evidence response or maintain a legacy compatibility handler.

## Runtime topology

```text
host-local logs
  -> fenced adapters
  -> ShareableGraphArtifact
       |-> local shareable API -> shared handlers
       |-> durable collector outbox
             -> authenticated direct publication
             -> bounded artifact revision + normalized source vector
                    -> targeted remote snapshot
                    -> shared handlers

host-local logs -> full canonical hydration -> local-only evidence APIs
```

The remote side authenticates, authorizes, validates, sequences, stores, and
serves artifacts. It does not download all source observations, rebuild a graph,
recompute measurements, compare complete historical JSON documents, or build a
whole-workspace store for a targeted resource request.

## Historical publication

Each physical log segment is fenced at the last complete line. Resumed segments
with one vendor session identity coalesce into one logical source and one
canonical session. The collector publishes a small immutable source checkpoint
after source registration. Only accepted current checkpoints may appear in an
artifact source vector.

One project publication contains:

```text
workspace_id / agent_id / project_id
agent/project-local publication_sequence
complete normalized source_vector for the collected graphs
one or more bounded ct.shareable_graph.v1 artifacts
```

The transaction verifies collector capability, project ownership, source
membership, accepted checkpoints, current watermarks, schema shape, content
bounds, canonical sizes, request digest, artifact digests, and idempotency. It
then publishes all revisions at one workspace sequence, supersedes their prior
revisions, updates inventory, records resource lookup rows, and commits one
receipt. Sources outside the scan are not deletion evidence. Unrelated existing
artifacts remain visible when a time or vendor filter excludes them.

An overlapping current graph may only be replaced when the request includes all
of its previously published sources. An omitted graph is retired only when all
of its sources are represented in replacement graphs, such as a graph merge.
An incomplete overlap consumes the publication sequence with a rejected receipt
and leaves history unchanged. The collector reports `artifact_scope_incomplete`;
a subsequent expanded, authorized scan can proceed without deleting its outbox.

Each `(workspace, project, agent)` owns its publication sequence. Different hosts
can publish disjoint graphs into the same portable project. Overlapping sessions
from another agent are rejected rather than overwritten. Use separate collector
state for each agent; automatic ownership transfer is not supported.

The same request and idempotency key return the same receipt. Conflicting reuse
is rejected. A publication sequence gap is rejected. After pending requests are
retried,
`ct_collector_recover` returns the authenticated agent's committed publication
watermark. A fresh local database also recovers source epochs/checkpoints and
the existing living-instance sequence. The RPC does not reserve sequence numbers;
run one active collector per agent/project stream. An already-consumed sequence
conflict is retained locally as superseded and reconciled before new work. A
valid but stale source vector is consumed as superseded and never becomes visible.

Legacy source observations and artifact revisions remain immutable. They are
not deleted or mixed with shareable history. Unfinished legacy projector jobs
are retired, and the projector RPCs and worker are removed.

## Historical reads

A request pins workspace sequence `S`. Visible revisions satisfy:

```text
published_sequence <= S
and (superseded_sequence is null or superseded_sequence > S)
and schema_version = ct.shareable_graph.v1
```

Session, turn, and item requests use normalized resource rows to load only the
owning artifact. Project and vendor filters are applied in the snapshot query.
The returned artifact identity, digest, and schema are validated by the same
Pydantic model before it is converted to an ephemeral `DocumentStore` for the
existing handler.

`project.sessions` is the intentional collection read; a targeted session call
does not build a complete workspace store. A batch retains one pinned snapshot
sequence across its historical calls.

## Artifact and JSONB rationale

The artifact is a bounded, nested API document rather than a queryable event
lake. JSONB preserves its ordered hierarchy and optional typed capsules without
creating a second relational canonical model. The fields needed for integrity
or lookup are normalized separately.

The 8 MiB per-artifact and 16 MiB per-publication limits are hard gates. Exact
replay compares deterministic SHA-256 digests. Storage or delta infrastructure
is not introduced while representative artifacts remain safely within those
bounds.

The complete artifact contract and retention decision are documented in
[`remote-ct-storage-optimization-proposal.md`](remote-ct-storage-optimization-proposal.md).

## Content policy

Shared artifacts retain structural and numeric facts, portable paths, and bounded
identifiers. They omit session titles, user/assistant prose previews, plan text,
and free-form tool descriptions. A user-request record carries the fixed marker
`[content omitted]` with its original numeric measurements. Tool descriptions
are limited to `tests`, `checks`, or `command`. Python and SQL enforce these
restrictions; artifact coverage declares `semantic_previews=false`.

Overview and summary responses consequently have reduced descriptive coverage.
Detailed content remains available through the local evidence APIs. Bounded
identifiers and portable paths remain intentional product data, not anonymized
values.

## Project inventory

Portable `project_id`, display name, optional repository identity, and aliases
are revisioned independently from historical artifacts. Agent project locations
may contain private local paths, but are principal-private and never used as
portable identity or returned by shared historical APIs.

## Living state

Living state remains a stream of explicit host observations. Heartbeats and
canonical living changes share one monotonic agent-instance sequence. Freshness
transitions to `unknown` after lease expiry; missing or expired data never
fabricates `not_living` or terminal state.

Snapshot and delta pagination keep their existing raw, scope-bound cursors.
SSE is an optional transport over the same durable sequence.

## Estimation

Estimation remains a separate append-only job and forecast authority. A
server-side worker owns provider credentials, claims, retry policy, and result
publication. Historical inputs are pinned to one workspace snapshot. The
shareable-artifact change does not place estimator credentials on collectors or
clients.

## Access control

- Supabase Auth identifies users and agent principals.
- Every durable key begins with `workspace_id`.
- RLS protects exposed tables.
- Collector writes occur only through capability-checked RPCs.
- Estimator service-role credentials remain server-only.
- Host paths and source files remain local or principal-private.
- Remote artifacts contain only the bounded shareable contract.

## Rollout gates

1. Fetch and record the exact source revision without overwriting local work.
2. Validate representative Codex, Claude Code, and Pi fenced sources.
3. Require zero identity/topology and numeric metric deltas.
4. Require zero prohibited body/path/media fields.
5. Require byte- and digest-identical replay and lost-response retry.
6. Confirm every artifact is below 8 MiB and the atomic publication below
   16 MiB.
7. Confirm the configured Supabase target is authorized and non-production.
8. Apply the migration, publish a canary, and verify snapshot/idempotency/stale
   behavior before enabling supervised collection.

A failed gate stops rollout. Privacy, topology, checkpoint, snapshot, and
idempotency rules are never weakened to continue deployment.

## Non-goals

- Uploading raw logs, full sessions, or general events.
- Synchronizing SQLite or allowing cache write-back.
- Maintaining compact-v2 or historical remote compatibility handlers.
- Treating filesystem paths as shared project identity.
- Reimplementing Python API semantics in SQL or TypeScript.
- Reconstructing graphs in a remote projector.
- Adding blob storage, compression transport, or graph deltas without measured
  necessity.
