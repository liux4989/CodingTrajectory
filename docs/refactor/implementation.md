# Implementation plan and migration

Implement in dependency order. A phase is complete only when its evidence in
[qualification](qualification.md) passes. Do not mark the entire refactor complete
because chunk transfer works or because a Worker deploy succeeds.

## Work packages

| ID | Deliverable / modules | Depends on | Exit evidence |
| --- | --- | --- | --- |
| R0 | Freeze invariants, protocol vocabulary, budgets and ownership; reconcile paused upload candidate | Documentation baseline | Candidate-to-spec gap checklist |
| R1 | Canonical repository interface and durable change/capture boundary: discovery, living stores, ingestion, runtime | R0 | Offline local reads; source/canonical/capture crash checks; reconstruction parity |
| R2 | Durable prepare/batch/transfer/recovery with legacy compatibility: collector, upload modules, CLI, Cloudflare ingress | R0 and R1 capture interface | Initial/changed bounded transfers, lost ACK/restart recovery, unchanged readers |
| R3 | Indexed published catalog, watermark/change feed, ownership and committed manifests: workspace authority, remote repositories | R2 | Multi-host isolation, scope-safe updates, paging beyond current record ceiling |
| R4 | Live Datahub adapter and explicit source/capability/freshness handling: Datahub serving contracts, Worker, frontend | R3 | Authenticated local/shared parity, no website rebuild on publication, no read-side upload |
| R5 | Qualified incremental canonical parsing and manifest-native large-graph reads | R1-R4 | Reduced affected-source work and larger-than-legacy graphs with bounded processing |
| R6 | Lifecycle commands, opt-in supervisor integration, retention, recovery runbooks and soak | R2-R5 | Crash/outage/restart/GC/load gates and documented operational limits |

R1 and R2 can proceed in parallel behind a frozen capture/repository interface.
R5 is necessary for the final scale objective; shipping R2 must explicitly state
that full-prefix reconstruction and legacy whole-graph limits may remain.

## Connection workflow

Implement the [connection lifecycle](connections.md) with R2/R4: one compatible
profile resolver, role-aware collection, read-only capability checks, credential
rotation, and explicit query source selection. Preserve pending delivery state.

## File ownership

- Upload worker: `control_plane/collector.py`, `collector_protocol.py`, upload
  modules, collector CLI, Cloudflare upload/manifest wiring, upload qualification.
- Parent/local repository work: discovery and living repository integration,
  canonical parser contracts, runtime read side effects. Coordinate any collector
  interface changes before editing shared files.
- Parent/Datahub work: `datahub_plugin/serving/`, response models/projection adapters,
  hosted Worker and frontend source/capability integration, Datahub qualification.
- Shared protocol/schema files: one assigned editor per change. Generate types from
  canonical models; do not hand-maintain duplicate schemas.
- Docs baseline: `docs/refactor/`. Update current operational docs only when the
  corresponding behavior is implemented.

The user-authorized upload agent has resumed implementation. The initial
candidate included chunk negotiation/upload, manifest reads, offline preparation,
and an outbox. The [operations record](operations.md) describes the integrated
workflow and remaining scale gates. In
particular, inspect source ownership, partial-scope omission, publication-only
watermark, bound reconstruction, parser behavior, and query-side publication.
Candidate reports are evidence to reproduce, not independent proof of completion.
Use the [candidate checkpoint](candidate-checkpoint.md) as the R0 reconciliation
checklist; update each gap with its implementation and qualification receipt.

## Additive migration

1. Record current deployed versions, scoped credential identities, current
   publication head, retained artifact hashes, and local delivery-state schema.
   Keep receipts private and aggregate; never export secrets or raw evidence.
2. Add new local schema/contract versions with backups and explicit migration.
   Preserve pending outboxes; a schema upgrade must not acknowledge or drop them.
3. Deploy additive shared endpoints/tables. Keep old committed artifact reads.
   Negotiate protocol support; incompatible clients fail explicitly before sending
   data, rather than receive an accepted receipt with changed semantics.
4. Import existing committed descriptors into new indexes in bounded resumable
   transactions. Check inventory and read parity at the same revision. Do not reset
   the shared database or republish every source as a substitute for migration.
5. Shadow-read the new query path, compare with the committed legacy representation,
   then enable a single collector and isolated canary project through the new path.
6. Qualify a second collector, interrupted publication, duplicate copied sources,
   and mixed legacy/chunk readers. Migrate remaining collectors with dedicated
   credentials and stable state paths. Existing migration from a different authority
   may need a fresh state file; routine upgrades within this authority must preserve
   delivery state.
7. Deploy the live Datahub adapter behind the existing private authentication
   boundary. Verify authenticated browser behavior and signed-out denial separately.
8. Retire read-triggered publication and old staging endpoints only after clients
   and rollback requirements are accounted for. Enable automatic publication only
   for explicitly configured hosts; the paused Mac schedule remains paused.
9. Enable qualified retention and large-graph functionality last. Document the
   oldest readable revision and supported schema versions.

## Rollback

Before new-only writes, reverting the reader/service binary can select the existing
legacy representation. Once a new-only graph exceeds the legacy ceiling, old code
cannot safely read it: rollback must keep a compatible reader or disable that
feature. Never silently truncate or call a binary rollback sufficient.

A local rollback must understand the upgraded outbox schema or leave it intact
while draining with the compatible service. A remote database restore changes its
incarnation; collectors detect it and reconcile known manifests/receipts before
resuming. Data and deployment rollback are separate operations.

Do not delete payloads referenced by any rollback/read revision. Garbage collection
requires dependency tracing and a race-safe grace/recheck protocol. Authentication
rollback must not widen membership or distribute administrative credentials.

## Implementation discipline

Use uv and Pydantic. No new unit tests; use integration qualification and fault
injection. Build/test fan-out is at most two jobs. For metric-sensitive changes,
run `scripts/check-metrics-quality-gate.sh` and the full baseline command directly:
`uv run python scripts/validate-metrics-baselines.py`. Do not derive new expected
metrics from current output; reconstruct intentional changes from committed source
evidence first. Commit only owned files and preserve concurrent work.

Keep an execution checklist per phase with code commit, contract version,
qualification receipt, deployment version (if any), observed runtime proof,
remaining limitations, and rollback readiness. Change the spec when a verified
constraint changes a decision; do not quietly relax a gate to finish a phase.
