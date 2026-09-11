# Implementation plan and migration

Implement in dependency order. A phase is complete only when its evidence in
[qualification](qualification.md) passes. Do not mark the entire refactor complete
because chunk transfer works or because a Worker deploy succeeds.

## Live API remediation in progress

The [accepted remediation design](live-api-design.md) governs the remaining
M1–M6 work. The additive `ct_catalog_read_v2` slice implements Pydantic-generated
ingress/response schemas, 30-minute server-held selections and cursors, independent
publication/project-metadata selection, registered/published project paging,
all four session include variants and explicit projection coverage. It neither
changes deployed consumers nor removes legacy RPCs or payloads.

The local D2 consumer cutover now routes ordinary Core project inventory and
session lists, hosted status/lists/change feeds, and browser project pagination
through v2. Grouped runtime calls capture the separate workspace/estimation fence
in the same transaction as catalog selection. Explicit sequence callers remain
on the labeled compatibility path. Change feeds freeze their upper bound and
reset rather than acknowledge overflow; metadata-only edits invalidate open lists.
Core all-items compatibility reads fail above 10,000 rows or 8 MiB rather than
silently returning a prefix. Projection absence no longer triggers list hydration.

Local evidence: `npm run check` in `cloudflare/control-plane` passes;
`node scripts/qualify-refactor-runtime.mjs --all` passes, including 86 control-plane
checks and all four v2 include variants through the runtime with local evidence
enabled, against the canonical Python producer. Hosted binding checks include
metadata-only invalidation and typed cursor failures. Chromium verified a project
rename appears in the open list via polling without a publication or reload.
The SQLite catalog scenario traverses 10,001 legacy rows and checks v2 concurrent
publication, metadata rename, empty registrations, scope rejection, expiry and
incarnation reset. Full metric baselines pass 107 assertions and 49 invariants.
These checks use disposable local data; no deployment or shared migration ran.

The first D3 storage slice adds `ct_catalog_read_v2(kind=resources)` for independent
graph, tree and metadata-item projections. Python uses the existing canonical
handlers. Ingress validates membership, item/turn identity, coverage and payload
budgets, then stages immutable rows in bounded pages. Publication selects their
descriptor atomically with the artifact; an unfinished stage is not readable.
The read resolves exact manifest membership and fetches only the requested row.
Item batches accept at most 100 IDs and continue within the existing 512 KiB
response budget, preserving requested order and the original selection.

Initial limits are 128 KiB per independent projection page and 4 MiB/10,000 rows
per staged projection set. Version 2 trees page at semantic branch boundaries;
resource cursors retain both resource and page position. Oversized graph/item
outputs report `budget_exceeded` independently. A branch that cannot fit a page
or a larger staging set fails explicitly before publication. Preparation still
builds the local graph, and old bundles/gzip remain for compatibility readers.

The expanded local upload qualification passes 52 checks, including canonical
graph/tree/item parity, invalid item identity/content rejection and expansion
across publication. SQLite qualification adds 10,000 sibling rows without changing
the selected payload transfer (209 serialized SQL-result bytes in this fixture),
and checks indexed membership query plans. This is not a measurement of all SQLite
pages visited, historical-version scaling, or production latency.

No hosted detail consumer is switched yet: old committed publications lack the
new descriptor. D3 still requires bounded backfill, complete coverage qualification
and consumer cutover with parent-selection propagation. Collectors now negotiate
resource projection v2 through an authenticated capability endpoint and retain
legacy staging when the endpoint returns 404 `not_found`; authorization/transport
failures do not downgrade. V1 retry identities and stored resource rows remain
readable. Staging may upgrade a v1 descriptor to v2 without changing old published
selections. Do not republish unchanged sources as a substitute for backfill.

New local canonical revisions store immutable blocks of at most 64 KiB rather
than repeated inline captures. Referenced outboxes acquire a durable retention
offer before their transaction and confirm it afterwards. Acknowledged rows keep
their pins because comparison and replay still need them. Legacy inline revisions
and outboxes remain readable, including read-only unmigrated databases and stable
inline retry identities. This is storage deduplication, not incremental parsing:
preparation still reconstructs, serializes and hashes complete captures. No GC is
enabled, and offered/retained pins are not yet automatically retired.

Completed forecast get/list calls now acquire only a workspace metadata fence and
forecast records. Actual-outcome refresh scopes compatibility hydration to the
forecast's session where available; predict/bind/calibration acquisition is not
fully migrated. Request tracing qualifies completed get/list without historical
RPCs. The complete runtime suite passes 90 control-plane, 52 upload, 27 canonical
repository and 26 native-binding checks, plus the existing lifecycle, connection,
release and catalog scenarios. Canonical qualification includes two process-crash
boundaries, corrupt-block rejection and old inline retry identity. The metrics
gate skips these non-metric-sensitive paths; the separately executed full baseline
workflow passes 107 assertions and 49 invariants without changing expectations.

The connected Mac runner also qualifies the exact source archive in a disposable
directory on macOS ARM64 / CPython 3.12.14: canonical repository 27 checks with two
crash boundaries, preparation reuse 6 checks, and metrics 107 assertions / 49
invariants pass. The archive checksum and all 445 source files were verified;
qualification used an isolated home and offline locked dependencies after setup.
No provider data, existing collection state or host schedule was used or modified.
This does not qualify deployed credentials or sustained multi-host workloads.

Remaining gates, in order:

1. D1/D2 deployment and scale qualification remain separate release gates;
   local consumer cutover is implemented. No shared retention collection is enabled.
2. D3: complete bounded backfill, coverage and reader cutover on
   the new independent graph/tree/item storage.
3. D4: every Core parameter shape, estimation acquisition, local evidence and CLI
   selection migration before removing historical reconstruction.
4. D5: incremental resource capture/outbox and manifest-native resumable staging.
5. D6: qualified authoritative catalog convergence, legacy deletion and payload GC
   after the seven-day rollback window and reference-safe retention checks.

Catalog v2 currently reuses the legacy projection record's requested JSON summary
variant; SQL still parses that stored record. Physical projection/resource splitting
and query-plan work bounds remain D3 work. Selection and cursor tables each cap at
10,000 entries and remove at most 200 expired entries per allocation; capacity
exhaustion is explicit, never eviction of a live selection. Authority restoration
must rotate the existing upload-authority incarnation before accepting reads.
Shared publications still retain legacy payloads for old readers. Local referenced
revisions/outboxes require a compatible reader: a binary downgrade alone cannot
decode newly written references. Preserve the upgraded local state and drain with
the compatible service rather than deleting or replacing it.

## Work packages

| ID | Deliverable / modules | Depends on | Exit evidence |
| --- | --- | --- | --- |
| R0 | Freeze invariants, protocol vocabulary, budgets and ownership; reconcile paused upload candidate | Documentation baseline | Candidate-to-spec gap checklist |
| R1 | Canonical repository interface and durable change/capture boundary: discovery, living stores, ingestion, runtime | R0 | Offline local reads; source/canonical/capture crash checks; reconstruction parity |
| R2 | Durable prepare/batch/transfer/recovery with legacy compatibility: collector, upload modules, CLI, Cloudflare ingress | R0 and R1 capture interface | Initial/changed bounded transfers, lost ACK/restart recovery, unchanged readers |
| R3 | Indexed published catalog, watermark/change feed, ownership and committed manifests: workspace authority, remote repositories | R2 | Multi-host isolation, scope-safe updates, paging beyond current record ceiling |
| R4 | Live Datahub adapter and explicit source/capability/freshness handling: Datahub serving contracts, Worker, frontend | R3 | Authenticated local/shared parity, no website rebuild on publication, no read-side upload |
| R5 | Qualified incremental canonical parsing and manifest-native large-graph reads | R1-R4 | Reduced affected-source work and larger-than-legacy graphs with bounded processing |
| R6a | Managed host lifecycle, explicit automatic mode, restart/outage recovery and unified status | R2 | Installed-but-disabled supervision; retained outbox and mode after restart; isolated project failures |
| R6b | Reference-safe retention and sustained multi-host/large-graph qualification | R3, R5, R6a | GC races, restore, outage catch-up and documented operational limits |

R1 and R2 can proceed in parallel behind a frozen capture/repository interface.
R5 is necessary for the final scale objective; shipping R2 must explicitly state
that full-prefix reconstruction and legacy whole-graph limits may remain.

## Approved next delivery order (2026-09-10)

Follow [the operating model](operating-model.md):

1. R6a: one managed host service, project registration, explicit enablement,
   restart recovery and local status. This does not depend on R5.
2. Bind the live Datahub Worker to Core through a service binding, preserving
   reader authorization and the external collector HTTPS interface.
3. Add one reproducible release entrypoint with isolated staging, bounded resumable
   migration, authenticated verification and recorded versions.
4. Reduce preparation and validation work: reuse unchanged canonical inputs,
   keep expensive payload work outside the workspace coordinator, and preserve
   the explicit compatibility ceiling until manifest-native scale is qualified.
5. Complete truthful publication/collector health and operational acceptance.
   R6b remains a separate capability gate for destructive retention and scale.

Each step must leave a usable, qualified result. Keep new commands and behaviors
marked proposed until their implementation and integration checks are recorded.

### Operating delivery implementation (2026-09-10)

All five steps have local implementations; no host schedule or cloud deployment
was activated by this delivery.

| Step | Implemented behavior | Local evidence |
| --- | --- | --- |
| Managed lifecycle | One host service; explicit publication policy; launchd/systemd templates; retained per-project state; separate preparation/delivery lanes; secret rotation | `qualify-managed-collection.py`: 22 checks, real SQLite/CLI, HTTP outage and hard process restart |
| Native connection | Datahub `CORE` service binding with scoped reader credential; explicit staging binding | Native workerd service binding; denied writer/wrong workspace; zero public Core calls |
| Release | One resumable script and manual CI workflow; isolated DO/R2; bounded migration; clean-source/staging proof; worker version and durable canary receipts | `qualify-release-workflow.py`: 18 checks on loopback; Actions/live release still unverified |
| Reduced processing | Content/parser/dependency-keyed normalized source cache; shared Python projection store/cache; Worker body validation and reconstruction; 128-entry DO index pages and final coverage check | `qualify-preparation-reuse.py`: exact reuse, same-length edit and corrupt-cache recovery; incremental delivery and legacy reads remain qualified |
| Truthful status | Local heartbeat age, pending age and remedies; metadata-only catalog status; separate publication/authority times; unknown hosted collector health | Native runtime and Pydantic serialization; generated API types and hosted build |

Run `node scripts/qualify-refactor-runtime.mjs --all` for the complete local
integration flow. The metrics gate and full committed baseline workflow also
pass without changing expected values. Qualification has no cloud-account access
and uses no user provider logs.

The source cache avoids repeated normalization of byte-identical source groups,
but still reads and fences complete prefixes. It is disposable and capped at
64 MiB; durable repositories and outboxes remain authoritative. Full-graph
assembly, serialization and the 8 MiB graph ceiling remain. R5 and R6b are not
complete. Hosted health has no host heartbeat feed: source counts and catch-up
are null, and the UI says collector health is unknown. Existing publication rows
without a server commit timestamp retain an unknown publication time.

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
