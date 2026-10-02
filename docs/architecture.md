# Architecture

Core has two responsibilities: construct contextual sessions and report native
session measurements. Consumers use stable references to move from inventory
to detailed evidence.

## Canonical model

The hierarchy is `SessionGraph → Session → Turn → Item`. Events provide the
observed evidence for these resources.

- A session represents one coding-agent thread.
- A turn represents one interaction or an observed agent lifecycle.
- An item represents a message, tool operation, or another normalized action.
- An event records a normalized observation and its hierarchy references.
- A graph contains one orchestration run, including its spawned agents.

`session.tree` shows conversation lineage. An ordinary conversation fork starts
a separate orchestration run; it does not enter the parent's graph totals.
Graph identity is the root session ID.

## Data flow

```diagram
┌────────────────────┐
│ Local vendor logs  │
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Canonical graphs   │
└─────────┬──────────┘
          ▼
┌────────────────────┐        ┌────────────┐
│ Graph preparation  │───────▶│ Local API  │
└─────────┬──────────┘        └────────────┘
          │ authorized publication
          ▼
┌────────────────────┐
│ Collector          │
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ R2 artifacts       │
│ DO manifests       │
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Prepared remote API│
└────────────────────┘
```

Vendor adapters preserve source identity and order before normalization.
Ingestion owns revision reconciliation, inherited-history classification,
deduplication, relationships, and accounting before retention removes content.
Publication must not repair incorrect reconstruction or invent missing values.

Graph preparation produces validated facts, summaries, and bounded API objects.
Local reads and the collector share this computation. A disposable SQLite cache
uses the canonical graph digest and preparation version as its key.
Its default path is `~/.coding-trajectory/prepared-graphs.sqlite`; its payload
budget is 128 MiB. Discovery, parsing, and hashing still detect changed sources.

## Remote authority

The Python Cloudflare Worker stores immutable artifacts in R2. One SQLite
Durable Object (DO) per workspace stores manifests, checkpoints, receipts,
inventory, upload claims, and living state.

The collector computes historical facts and prepared responses. The Worker
authenticates requests, validates publication, selects snapshots, checks object
integrity, and serves bounded prepared results. It does not run ingestion or
recalculate historical metrics on each read.

Remote clients use `/v1/api` with `ct.api.v1`. Collector and authority operations
use `/v1/core` with `ct.core.v1`. Retired SQL fact read/stage/publish methods
return 404. There is no SQL fallback or automatic import of pending legacy batches.

The public Core registry has 18 methods. Remote delivery supports 16;
`session.search` and `living.events` return `method_unavailable` remotely.
Method versions and exact schemas are in the [frozen protocol](core-protocol.md).

## Evidence and privacy

Original logs remain the evidence authority on their host. Standard queries
return retained canonical evidence, not arbitrary raw vendor payloads.
Coverage fields distinguish observed, derived, partial, and unavailable data.

Publication targets an authenticated internal workspace. It retains bounded
message previews, semantic tool descriptions, command arguments, and target paths.
It excludes full transcripts, reasoning bodies, raw tool input/output objects,
stdout/stderr, file or patch bodies, and arbitrary event payloads.

**Warning:** bounded content is not necessarily safe to share publicly.
Description redaction handles common explicit credentials and strips sensitive
URL components. It is not a complete secret scanner. Preview truncation is not
credential redaction. Review the intended content before authorizing publication.

## Ownership

Paths below are relative to `packages/core/src/coding_trajectory/`, unless stated otherwise.

| Boundary | Owner |
| --- | --- |
| Source discovery and reconstruction | `discovery.py`, `ingestion/` |
| Retained facts and evidence policy | `control_plane/fact_projection.py`, `control_plane/published_facts.py` |
| Reusable preparation | `control_plane/graph_preparation.py`, `control_plane/prepared_api.py` |
| Local reads | `control_plane/fact_repository.py`, `service/` |
| Remote client | `control_plane/remote_api.py` |
| Publication and recovery | `control_plane/collector.py`, `control_plane/publication_run.py` |
| Public contracts | `contracts/` and `validation/core-protocol.json` at the repository root |
| Workspace and artifact authority | `cloudflare/control-plane/src/` at the repository root |
| Product state and judgments | `packages/plugins/loop/` at the repository root |

Plugins consume Core contracts. They do not reconstruct sessions independently
or redefine native metric formulas. Pricing estimates remain distinct from
provider-reported cost. Evaluations, scores, forecasts, and recommendations do
not become canonical session facts.

See [operations](operations.md) for publication and release procedures.
