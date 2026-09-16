# Architecture

CodingTrajectory reconstructs host-local coding-agent records into canonical
`DocumentStore`/`SessionGraph` models and exposes versioned Pydantic contracts
through one shared service runtime.

## Historical data flow

```text
immutable provider JSONL
  → occurrence-aware ingestion and canonical reconstruction
  → DocumentStore / SessionGraph with exact measurements
  → direct privacy projection → bounded PublishedFactSet
      ├─→ local FactRepository ─┐
      └─→ collector → Durable Object SQLite → remote FactRepository
                                └──────────────→ shared historical handlers
```

Ingestion owns source occurrence identity, provenance, reconstruction, and exact
pre-retention accounting. Chronicle publication consumes only the canonical
graph. It projects allowlisted, bounded facts and never reparses raw provider
payloads. The collector stages fact rows and checkpoints without changing their
meaning. Cloudflare validates, versions, selects, and pages facts; shared Python
handlers own historical summary and metric semantics.

Raw records and occurrence inventories remain local. Remote historical state is
queryable fact rows in Durable Object SQLite, not a whole-document store, chunk
graph, reconstruction cache, or backup. Standard local and remote methods use
the same fact representation and response contract.

Living observations are a separate authority. Local/Core estimation under
`packages/core/src/coding_trajectory/estimation/` is also separate; Cloudflare
does not host estimation jobs or semantics.

## Ownership

| Location | Responsibility |
| --- | --- |
| `ingestion/`, `discovery.py` | Source occurrences, canonical reconstruction, exact accounting |
| `control_plane/fact_projection.py` | One-way privacy and evidence projection |
| `control_plane/published_facts.py` | Published fact identities, semantics, bounds, and direct reconstruction |
| `control_plane/fact_repository.py` | Shared local/remote historical store boundary |
| `control_plane/collector.py` | Checkpoint and fact-set staging/publication |
| `cloudflare/control-plane/src/facts.ts` | Fact validation, atomic revisions, selection, cursors |
| `contracts/`, `service/` | Stable public contracts and shared semantics |
| `estimation/` | Local/Core forecast authority |
| `validation/metrics/` | Source-backed metric expectations and audits |

See [Chronicle history](chronicle-history.md), the [collector handoff](local-collector-handoff.md),
and the [Cloudflare design](remote-ct-control-plane-design.md).

## Validation

Use `uv sync --all-packages`. Metric-sensitive changes require both the metrics
quality gate and the direct baseline validator. Never derive expected metric
values from current output alone.
