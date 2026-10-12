# Architecture

Core has two responsibilities: construct contextual sessions and report native
session measurements. Consumers use stable references to move from inventory
to detailed evidence.

## Canonical model

The hierarchy is `SessionGraph → Session → Turn → execution unit → Item`.
Turns order direct item references and tool-script executions. Flat items retain
the underlying evidence once; script membership is expressed through references.
Events provide observed evidence for these resources.

- A session represents one coding-agent thread.
- A turn represents one interaction or an observed agent lifecycle.
- An execution unit is a direct item reference or a script execution with source,
  observed external calls, context references, and execution result evidence.
- An item represents a message, tool operation, or another normalized action.
- An event records a normalized observation and its hierarchy references.
- A graph contains one orchestration run, including its spawned agents.

`session.tree` shows conversation lineage. An ordinary conversation fork starts
a separate orchestration run; it does not enter the parent's graph totals.
Graph identity is the root session ID.

## Data flow

```text
Local vendor logs
  → adapter-owned streamed relationship metadata
  → fresh project/session/topology inventory per request
  → selected runs through existing canonical ingestion
  → complete normalized canonical content and measurements in memory
  → direct contextual handlers and on-request metrics
```

Inventory reads discover identities and relationships without constructing every
run. Detail reads reconstruct only requested runs and their canonical dependencies.
Vendor adapters preserve source identity and order before normalization.
Ingestion owns revision reconciliation, inherited-history classification,
deduplication, relationships, and accounting before retention removes content.

Historical and inventory queries are stateless: topology is discovered fresh for
each request, and only selected runs and their canonical dependencies are ingested,
normalized in memory with complete available content. Reuse is limited to one request or explicit batch;
there is no cross-request topology or graph persistence. Core no longer uses
`~/.coding-trajectory/local.sqlite`. Repeated detail reads of large runs repeat
ingestion rather than amortizing it through a persistent cache.

No upfront preparation, publication packs, manifests, saved snapshots, signatures,
or byte-budget pages are required. Display projections and metrics are computed
on request, not stored as canonical history. Pricing uses the existing live
catalog path and remains distinct from provider-reported cost.

## Local query boundary

Chronicle is the local canonical query layer. Its 18 typed methods execute
locally, without source negotiation or remote fallback. The parked Cloudflare
runtime is unsupported and is not built by Core CI.

Collection queries return every matching resource in deterministic order by
default. An optional positive `limit` selects a prefix; overview selects the
latest turns. There is no maximum page size, continuation cursor, or saved view.
Canonical ordering and ID tie-breakers remain explicit. Repeated reads of large
runs may be slower; correctness and complete local evidence take priority over
response-size optimization.

Living queries are stateless too. `living.sessions` projects source topology and
file metadata without ingesting transcripts; `living.events` projects a required
run or subordinate scope through the same canonical-run path as detail queries.
Each returned resource has a stable digest. Consumers compare complete live
passes and own their last-seen state; Core keeps no change history, removal
markers, checkpoints or second payload store. Loop's `investigations.sqlite3`
and `monitor.sqlite3` remain product/user data, not Core derived caches.

Local `ct.api.v2` envelopes contain `result`, structured availability and errors.
Constant transport metadata is removed. Method versions and exact schemas are
in the [frozen protocol](core-protocol.md).

## Evidence and privacy

Original logs remain the evidence authority on their host. Local detail and
search queries preserve complete available normalized message, command, argument,
and output content, along with paths, identities, measurements and provenance.
Arbitrary vendor payloads do not become public canonical fields. Source omissions
and provider truncation remain explicit; complete local access cannot recover
content the original source never recorded.

Previews, sanitized tool descriptions, summary selection and short search
snippets belong to display projections. Their limits do not remove canonical
bodies or evidence references. Search matches full normalized content before
creating snippets. Operational output facts remain distinct from body availability.

Script units retain source references and observed-call evidence. Observed calls
and source-only candidates are distinct; source syntax does not prove execution
order. Stats retains native accounting and does not allocate wrapper content to
source-inferred children. Source digests preserve provenance, while living-resource
digests let consumers detect changed normalized content. Persisted source
checkpoints and incremental publication/repair APIs are removed.

**Warning:** canonical content and local logs can contain private information.
Bounded previews and description redaction are not a complete secret scanner.
The legacy Amp capture plugin can still auto-publish externally; that path is
unsupported. Set `CT_AMP_AUTO_PUBLISH=0` for local capture.

## Ownership

Paths below are relative to `packages/core/src/coding_trajectory/`, unless stated otherwise.

| Boundary | Owner |
| --- | --- |
| Source inventory and relationships | `discovery.py`, `discovery_metadata.py`, vendor adapters in `ingestion/` |
| Canonical reconstruction and retention | `ingestion/` |
| Request-scoped topology and in-memory graph ownership | `service/store.py` |
| Local execution and typed query boundary | `runtime.py` |
| Direct contextual queries and optional selection | `service/` |
| Native metrics and live pricing | `metrics/` |
| Stateless living inventory and resource projections | `living_sessions.py`, `living_events.py` |
| Public contracts | `contracts/` and `validation/core-protocol.json` at the repository root |
| Product state and judgments | `packages/plugins/loop/` at the repository root |

Plugins consume Core contracts. They do not reconstruct sessions independently
or redefine native metric formulas. Pricing estimates remain distinct from
provider-reported cost. Evaluations, scores, forecasts, and recommendations do
not become canonical session facts.

See [operations](operations.md) for capture and local validation procedures.
