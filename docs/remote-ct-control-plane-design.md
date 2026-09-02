# Remote CT Control Plane Design

- **Status:** Accepted target architecture; foundation implemented
- **Date:** 2026-09-02
- **Scope:** All 25 public CT method authorities, compact remote custody,
  full-detail local callers, workers, caches, and collector handoff
- **Supersedes:** [`remote-session-ledger-design.md`](remote-session-ledger-design.md)

## Decision

CodingTrajectory will build a remote CT control plane, not only a synchronized
session store. The embedded transport retains the existing full-detail Pydantic
semantics against host-local evidence. The initial remote transport serves a
compact, explicitly scoped representation. Supabase provides Auth, RLS,
PostgreSQL, and durable state. Python workers own graph projection and
estimation execution.

The control plane has four explicit authorities:

| Authority | Public methods | Durable state |
|---|---:|---|
| Historical graphs | `project.sessions`, all `session.*`, all `graph.*` — 15 | Accepted observations and immutable `SessionGraph` revisions |
| Project inventory | `project.list` — 1 | Portable projects, aliases, and private agent locations |
| Living state | `living.sessions`, `living.events` — 2 | Agent leases, heartbeats, source checkpoints, ordered observations |
| Estimation | Seven `estimate.*` methods | Forecast events, jobs, attempts, leases, and results |

No local database is a fifth authority. A local outbox holds unacknowledged
delivery work; a local cache holds a labeled remote snapshot. Neither may answer
as shared current truth.

## Runtime topology

```text
Local CT agent ----\
                    >-- CT Python API --+-- historical graph authority
Remote CT agent ---/                    +-- portable project registry
                                        +-- living observation authority
                                        +-- estimation ledger
                                                  |
                   Supabase Auth/RLS/Postgres <---+-- projector/estimate workers
                              ^
                              |
                    authenticated ingestion
                              |
                    host-local CT collector
                    vendor logs + durable outbox
```

The API service is stateless with respect to canonical data. Supabase Edge
Functions may provide small webhook or ingress adapters, but they do not
reimplement graph, evidence, metric, or forecast semantics in TypeScript.

## Application architecture

The public method registry remains the semantic contract. Runtime routing is
transport-neutral:

```text
ContractRegistry
    |
ApplicationDispatcher
    +-- HistoricalAuthority
    +-- ProjectInventoryAuthority
    +-- LivingAuthority
    +-- EstimationAuthority

Transport
    +-- EmbeddedTransport       current local CLI/plugin use
    +-- HttpTransport           authenticated local/remote clients
```

Both transports validate shared request models and compatible result models for
the operations their content scope permits. The embedded transport can serve
full detail; the initial HTTP transport must declare `content_scope: compact`
and reject local-only detail methods. The CLI, Python SDK, plugins, and remote
agents differ by endpoint, credential acquisition, and declared content scope.
Agents never query canonical Supabase tables directly.

## Accepted observation ledger

### Why observations, not completed graphs

A coding session can resume after appearing terminal, and one graph can include
sources observed by different hosts. Therefore collectors do not claim that a
graph is permanently complete and do not race to upload replacement graphs.

Collectors submit normalized source observations. The current deployed wire
format is full-fidelity `canonical_session_snapshot.v1`. Its successor will
first apply the existing measurements retention and then scrub remote-only
private fields before publication:

```text
workspace_id                 resolved from the authenticated principal
agent_id
source_id
source_epoch                 advances on truncation or replacement
source_sequence              monotonic within one epoch
event_id                     stable deduplication identity
schema_version
parser_version
content_sha256
observed_at
payload
```

The server validates and accepts observations. A Python projector deterministically
assembles accepted observations into a complete immutable `SessionGraph`
revision for each currently known source vector. A publisher may report a
terminal observation, but a later valid observation appends a revision rather
than mutating history.

### Idempotency and ordering

- The same event identity and digest is an idempotent success.
- The same identity with a different digest is a conflict and cannot overwrite.
- Sequence gaps may be retained as pending but do not advance the contiguous
  committed source watermark.
- Source truncation, replacement, or incompatible compaction starts a new epoch.
- Every accepted request has a durable receipt keyed by agent and idempotency key.
- Graph projection records the complete source vector used for each revision.

Raw vendor logs and full-detail canonical trajectories remain evidence on their
originating host. The remote compact payload excludes tool bodies, command
bodies, host paths, file/media bodies, and unbounded event payloads. Raw-log
upload is not required by this architecture and must be a separate, explicit
security choice.

## Canonical graph revisions

`ct_artifact_revisions.payload` will contain the canonical compact
`SessionGraph` for the remote service. It round-trips through the compatible
Pydantic model with full-detail body fields absent. Search indexes, metric
facts, cards, and Datahub state are rebuildable projections, never alternate
writable representations. Full-detail local graphs remain host-local evidence,
not a second remote representation.

Each revision records:

```text
(workspace_id, artifact_id, revision)
schema_version
payload
content_sha256
source_vector
published_sequence
superseded_sequence
observed_at
committed_at
```

The composite workspace key applies at every boundary. Heavy projections are
not built in the ingestion transaction. That transaction commits accepted
records, one workspace sequence, a change-log record, and a transactional
projection-outbox item. Workers may rebuild projections later; historical API
handlers can compute from canonical graphs until a compatible projection exists.

## Portable project identity

`project.list` cannot be reconstructed from shared host paths. The control plane
owns stable `project_id` values and revisioned project metadata:

```text
project_id
display_name
repository_identity optional
aliases
published_sequence / superseded_sequence
```

An agent may register a private project location such as a local path or URI.
Locations are scoped to that agent and excluded from shared results by default.
Path basename, current working directory, and vendor-specific encoded paths are
never authorization boundaries or canonical project identity.

## Request snapshots and envelopes

Every call binds to workspace sequence `S` at request start. Every resource read
by that request is the version satisfying:

```text
published_sequence <= S
and (superseded_sequence is null or superseded_sequence > S)
```

Every item in `batch` shares one `S`. This prevents a response from mixing
graphs from before and after concurrent ingestion.

Remote-compatible method result schemas remain unchanged during the first
compact remote migration; local-only detail methods are not exposed through
HTTP. A versioned outer envelope adds transport metadata:

```json
{
  "id": "request-id",
  "method": "session.summary",
  "version": 1,
  "ok": true,
  "meta": {
    "workspace_id": "uuid",
    "snapshot_sequence": 481,
    "source": "remote",
    "freshness": "authoritative",
    "content_scope": "compact",
    "artifact_revision": 7,
    "projection_version": "optional"
  },
  "result": {}
}
```

The authenticated API exposes `call`, `batch`, and `schema`. Potentially
unbounded results use signed, scope-bound cursors. Ingestion and estimation
commands require idempotency keys. A client may request `min_sequence` with a
bounded wait for read-after-write behavior.

Source selection belongs to client transport configuration, not method
parameters. Normal compact operation uses the remote API. A full-detail local
operation selects the embedded transport explicitly. A request for local-only
detail never silently falls back from remote to vendor logs. Explicit offline
mode reads a snapshot-bound cache and reports its last known sequence.

## Living state

Living state is a stream of signed host observations, not database inference.
Each connected collector maintains an agent-instance lease:

```text
agent_instance_id
heartbeat_at
lease_expires_at
observed_at
received_at
source_watermarks
runtime_state
```

Freshness transitions are `living -> delayed -> unknown` unless an explicit
terminal observation exists. Lease expiry never means `not_living`. The
connected app server remains the only authority for direct runtime control.

The existing snapshot/delta and cursor model remains the durable protocol. SSE
may stream the same ordered changes but is only a transport convenience; clients
resume from their last durable cursor and receive `reset` when retention has
invalidated it.

## Estimation authority

Forecast evidence uses a remote append-only event ledger. A central Python
worker is the initial executor and owns provider credential access, budget
limits, retries, and concurrency. Queue payloads contain IDs, not canonical
payloads or secrets.

- `estimate.predict` creates an idempotent job and immutable forecast receipt.
- `estimate.bind` appends a binding event.
- `estimate.get`, `estimate.list`, and `estimate.calibration` fold events at a
  declared workspace snapshot.
- `estimate.backfill.start` creates a durable resumable job.
- `estimate.backfill.status` reads job, lease, progress, attempts, and failures.

Every forecast records the historical snapshot and retrieval corpus sequence it
used. A delegated host executor can be added later, but it is not part of the
initial shared architecture.

## Local outbox and cache

The collector writes proposed observations to a durable local outbox before
network delivery:

```text
vendor log -> normalize -> outbox -> remote receipt -> acknowledge locally
```

Outbox state is `pending`, `in_flight`, `accepted`, or `rejected`. Retries reuse
the same idempotency key. Unaccepted observations are visible only to their
originating host.

A separate optional cache is keyed by server, workspace, snapshot sequence,
artifact revision, and projection version. Revocation triggers eviction, but CT
does not claim it can retract data already delivered to a client. Cache records
are never uploaded as ingestion input.

## Access control

The initial product position is one private workspace with compact remote
membership and full-detail local custody:

- Supabase Auth identifies users and agent principals.
- Every durable key begins with `workspace_id`.
- RLS protects every table exposed to authenticated clients.
- Agent credentials are workspace- and capability-scoped.
- Canonical writes occur only through the API/projector role.
- The service role remains server-side and is never distributed to agents.
- Host locations, source payloads, worker queues, provider credentials, and
  full-detail local evidence are server-only or principal-private.
- Remote payloads contain compact historical facts only.

`session.events` full detail is embedded/local-only in the initial remote
release. Remote `session.items` may return only compact metadata and
measurements. A restricted grant still requires a separately versioned API
rather than an undocumented partial response.

## Foundation and delivery plan

### Foundation — this repository

1. Declare all 25 method-to-authority assignments in code.
2. Route the embedded runtime through a transport-neutral application dispatcher.
3. Add the Supabase identity, sequence, project, observation, graph revision,
   change log, living lease, estimation ledger, and worker-outbox schema.
4. Preserve current local API behavior while remote adapters are introduced.

### Remote service

1. Add versioned control-plane request and response envelopes.
2. Implement authenticated HTTP `call`, `batch`, and `schema` transports.
3. Implement workspace snapshot selection and remote authority repositories.
4. Add projector and estimation workers with leases and bounded retries.
5. Prove compact compatibility for remote-exposed methods, and prove explicit
   scope errors for local-only full-detail methods.

### Local collector

The host-local collector and authenticated Supabase ingress contract are now
implemented. `ct collector` discovers Codex, Claude Code, and Pi sources,
normalizes complete JSONL prefixes with the existing adapters, and currently
persists a full `canonical_session_snapshot.v1` in its local outbox. The next
wire version will apply compact measurements retention plus the remote-boundary
scrubber before publishing with a stable idempotency key and heartbeat.
Deployment and real-host validation remain
operational work because they require a specific Supabase project, registered
agent credentials, and local source access. Its fixed boundary and commands
are documented in [`local-collector-handoff.md`](local-collector-handoff.md).

## Acceptance criteria

The control plane is ready when:

1. Every registered method has exactly one declared authority.
2. Embedded and HTTP transports pass compatible validated results for the
   remote compact scope; local-only detail methods return an explicit scope
   error over HTTP.
3. A batch observes one workspace sequence across all authorities.
4. Repeated observation delivery is idempotent and conflicting reuse is rejected.
5. Compact graph revisions round-trip through `SessionGraph` and retain their
   source vector.
6. `project.list` needs no shared host path.
7. Lease expiry produces `unknown`, never fabricated terminal state.
8. Estimation jobs survive worker restart without duplicate successful forecasts.
9. RLS denies non-members and authenticated clients cannot write canonical tables.
10. Offline cache responses identify their snapshot and never merge vendor logs.

## Explicit non-goals

- Synchronizing SQLite files or allowing cache write-back.
- Treating filesystem paths as shared project identity.
- Recreating Python semantics in SQL, Edge Functions, or TypeScript.
- Building heavy projections inside the ingest transaction.
- Inferring liveness from missing database updates.
- Claiming a session graph can never resume after a terminal observation.
- Collecting or uploading real vendor data as part of the control-plane foundation.
