# Architecture

CodingTrajectory reconstructs coding-agent logs into canonical session graphs
and exposes versioned Pydantic contracts through a shared service runtime.

## Data flow

```text
Host-local vendor logs (immutable evidence)
  → vendor adapters → normalized transcript → Session → Turn → Item
  → SessionGraph + DocumentStore
      → local evidence handlers (content, events, search)
      → locally assembled ct.shareable_graph.v1
          → shared historical handlers
          → authenticated collector → Supabase artifact revisions
              → snapshot-pinned DocumentStore → shared historical handlers
```

Local ingestion owns graph reconstruction and numeric measurements. Remote
historical reads deserialize a validated artifact for the existing handlers;
they do not ingest vendor logs or run the retired historical projector.

The [shareable history contract](shareable-history.md) defines exact coverage,
privacy, digest, and size bounds. `session.search`, `session.events`, and
`session.items` with content remain local. Metadata-only items are shareable.
Reduced semantic coverage must not be presented as full evidence.

## Code ownership

| Location | Responsibility |
| --- | --- |
| `packages/core/src/coding_trajectory/ingestion/` | Vendor adapters, transcript assembly, retention, canonical resources |
| `packages/core/src/coding_trajectory/discovery.py` | Source discovery, fenced loading, graph assembly |
| `packages/core/src/coding_trajectory/contracts/` | Public request/response contracts |
| `packages/core/src/coding_trajectory/service/` | Runtime, handlers, store resolution, index cache |
| `packages/core/src/coding_trajectory/analysis/` | Evidence and activity projections |
| `packages/core/src/coding_trajectory/metrics/` | Usage, attribution, context, and runtime measurements |
| `packages/core/src/coding_trajectory/control_plane/` | Authority routing, strict sharing contracts, collector, remote repositories, HTTP service |
| `packages/core/src/coding_trajectory/estimation/` | Forecast ledger, prediction, calibration, and backfill |
| `packages/cli/src/coding_trajectory_cli/` | CLI commands, schema inspection, API calls, plugin dispatch |
| `packages/plugins/datahub/datahub_plugin/` | Datahub backend and enrichment |
| `packages/plugins/datahub/web/` | Datahub React frontend |
| `supabase/migrations/` | Ordered database migration history |
| `validation/metrics/` | Committed evidence, audits, pinned pricing, and expected results |
| `scripts/`, `benchmarks/` | Validation and benchmark tools |

## Authorities and state

Historical artifacts, portable project inventory, living observations, and
estimation records have separate authority handlers behind the same public
contract registry. The [control-plane design](remote-ct-control-plane-design.md)
is the detailed authority map. Discover the current methods with `ct api schema`.

Raw logs remain evidence authority on their originating host. Collector SQLite
stores delivery sequences, outboxes, and receipts. Its recovery rules preserve
exact retries and reconcile source/publication watermarks. The local index and
Datahub read models accelerate reads; they do not replace source evidence.

Supabase holds immutable metadata-only source observations, bounded artifact
revisions, normalized source vectors, and resource indexes. Historical SQL
migrations remain ordered and intact even when later migrations retire their
objects. A deployed database is not changed by repository cleanup.

## API and plugin boundaries

`ServiceRuntime` validates requests and responses for dedicated CLI commands,
`ct api call`, and `ct api batch`. Dedicated commands may present compact JSON;
API calls return the canonical versioned contract. Ordinary forks and spawned
agent runs have distinct scopes; graph totals never imply one shared context
window across agents.

Plugins are source-dispatched executables discovered through `plugin.toml`.
They consume CLI JSON contracts rather than importing core implementation.
Datahub owns pricing enrichment and presentation; canonical fields remain
agent-agnostic facts. See [plugin design](plugin.md).

## Development and validation

Use `uv sync` for the Python workspace. The core dependencies are declared in
`packages/core/pyproject.toml`; do not infer them from historical design notes.

```sh
uv run ruff check .
scripts/check-datahub-static.sh
scripts/check-metrics-quality-gate.sh
uv run python scripts/validate-metrics-baselines.py
```

Do not derive new expected metric values from a run alone. Intentional changes
require reconstruction from committed source evidence and an updated audit.
See the [metrics quality gate](metrics-validation-quality-gate.md).

The [September 5 rollout report](remote-ct-rollout-2026-09-05.md) records verified
non-production historical publication and reads. Concurrent collectors and
ongoing supervision remain outside that verified scope.
