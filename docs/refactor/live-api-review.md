# Live API requirements and removal map

Review date: 2026-09-10. Source baseline: `e4112cb`, plus the working-tree
version of `web/worker/live.ts` inspected during this review. Collection and
deployment work was already uncommitted. This is a proposed contract and migration
decision record, not an implementation or a claim about deployed behavior.

The expensive boundaries should change: prepare durable resources incrementally,
publish their validated references atomically, and answer selective queries from
the same indexed published resources in Python and the hosted adapter. Keep
canonical semantics, ownership, privacy, and recovery guarantees. Existing callers
determine migration work; they do not by themselves justify the mechanism they use.

## 1. Requirements before mechanisms

Three independent concepts govern retention:

- **Chronicle event history:** past session/turn/item facts remain available in
  the latest published session. Updating that session does not erase completed
  events merely because an older reconstruction is collected.
- **Reconstruction history:** earlier interpretations or versions of those facts.
  An arbitrary `snapshot_sequence` exposes this today, but that exposure does not
  establish a user requirement to retain every reconstruction indefinitely.
- **Read consistency:** one response, a multi-call operation, or successive pages
  must select compatible data. Bounded version retention or retained page state
  can satisfy this without permanent time travel.

Default reads should select the latest committed publication. A continuation
keeps its original selection until completion or explicit expiry. Independent
navigation selects latest unless the operation needs a parent revision, such as
expanding items from a previously returned turn. Refresh starts a new selection.

| Current user operation | Data actually needed | Required consistency | Contract decision |
| --- | --- | --- | --- |
| Open Datahub; inspect publication status | Capabilities, publication revision/time, retention floor, known coverage, authority observation time | One metadata response | Keep bootstrap; remove catalog listing from it |
| Browse projects and recent sessions | Project identities and bounded session summary rows, filters, ordering, continuation | Stable pagination, including a frozen relative-time filter | Keep lists; unify indexed reads and opaque cursors |
| Open a session graph or tree | Requested topology/summary and its canonical measurements | One response; token for related expansion | Keep semantic queries; replace bundled detail transport |
| Expand turn/item metadata | Requested IDs or one turn page, relationships, availability | Same selection as the parent when expanding it | Keep bounded batch/page reads; avoid fetching every item in an artifact |
| CLI/API queries for overview, summary, statistics and usage | Requested canonical result or bounded inputs to its existing reducer | One response; common selection for an explicitly grouped operation | Preserve behavior while replacing remote graph hydration |
| Show new publications | Changed identities/tombstones and a continuation or reset | Complete interval from acknowledged revision through a fixed upper revision | Keep a bounded invalidation feed |
| Inspect running sessions and collector health | Leases, observations and actual status evidence | Separate living cursor and evaluation instant | Keep distinct freshness semantics; publication success is not collector health |
| Prepare offline; publish; recover after interruption | Changed canonical resources, source fences, frozen batch identity, durable receipt | Local capture transaction and atomic shared publication | Keep durability and fencing; replace full-graph preparation/staging |
| Predict, bind, compare and calibrate estimates | Candidate features, relevant turn outcomes, forecast/comparison records and evidence provenance | Consistent planning input; frozen evidence for reproducibility where promised | Separate estimation evidence retention from arbitrary workspace history |
| Request raw text, events, search or narrative | Host-local bodies matching selected canonical facts | Verify the selected facts against local evidence | Keep local-only boundaries and offline local reads |
| Explicit old-sequence read or whole-graph retrieval | A prior reconstruction or all canonical data | Reproducible access only if separately retained | Existing compatibility surface; no demonstrated ordinary browsing need for indefinite retention or a new export product |

These requirements preserve manual/automatic publication policy, explicit local
versus shared selection, and read-only queries. “Live” means latest **published**
data, not filesystem watching or an upload initiated by a query. An empty valid
local/shared result is still a valid result.

## 2. Confirmed paths and callers

References below identify repository symbols, not deployment evidence. Names such
as “historical” describe today's code organization, not a requirement conclusion.

| Evidence | Current execution and consequence |
| --- | --- |
| [UploadService.prepare / _deliver](../../packages/core/src/coding_trajectory/control_plane/upload_service.py), [CanonicalRepository](../../packages/core/src/coding_trajectory/control_plane/canonical_repository.py) | Inventory can skip unchanged sources, but changed preparation invokes `LocalCollector.collect`, assembles graphs and builds whole Chronicle artifacts. Repository versions and outbox captures contain whole bodies; delivery validates/serializes them again. A one-graph page bounds count, not preparation work. |
| [CloudflareCollectorRemote.stage_artifact_payload](../../packages/core/src/coding_trajectory/control_plane/collector.py), [build_chunks](../../packages/core/src/coding_trajectory/control_plane/upload_chunks.py) | Canonical bytes and the full chunk dictionary are built before missing-node negotiation. Node reuse reduces transfer; it does not eliminate whole-graph traversal, hashing or allocation. |
| [Workspace.rpc / stage](../../cloudflare/control-plane/src/workspace.ts), [reconstruct](../../cloudflare/control-plane/src/upload.ts) | Chunk staging reads packs, reconstructs canonical JSON, hashes it, gzips and base64-encodes it, then calls staging which decodes and decompresses it, parses and validates the whole graph, and stores a gzip copy. Chunk packs and gzip bodies both survive. |
| [ServiceRuntime._call_historical](../../packages/core/src/coding_trajectory/runtime.py), [CloudflareHistoricalRepository](../../packages/core/src/coding_trajectory/control_plane/remote.py) | Only `project.sessions` has a direct projection shortcut. Other supported shared canonical queries select artifacts, download gzip/base64 bodies, validate and convert graphs, construct `DocumentStore`, then dispatch. An incomplete list projection falls back to the same reconstruction. |
| [Workspace.historical](../../cloudflare/control-plane/src/workspace.ts) | Selects `State.all` project/artifact rows and filters them in memory before artifact reads. Resource IDs narrow the selected artifacts, not the fields transferred. A 32 MiB compressed-response selection ceiling does not make a narrow item query resource selective. |
| [RemoteRuntimeFactory.runtime_options](../../packages/core/src/coding_trajectory/control_plane/http_service.py), [CLI remote flags](../../packages/cli/src/coding_trajectory_cli/commands/api.py) | Shared API/CLI runtime construction pins `ct_workspace_snapshot`; `--snapshot-sequence` and the HTTP envelope expose an explicit old-sequence option. This is a concrete compatibility caller, not proof of an ongoing retention requirement. |
| [RemoteEstimationAuthority._snapshot](../../packages/core/src/coding_trajectory/control_plane/remote_estimation.py) | Calls `store_for("project.sessions", {})` directly, bypassing the projection shortcut and requesting workspace-wide artifacts. Predict/bind/get/list/calibration use it; `_refresh_one` can request it again. Some callers initially discard the store just to obtain a sequence. |
| [LocalEvidenceRepository.store_for](../../packages/core/src/coding_trajectory/control_plane/local_evidence.py) | Resolves published canonical membership/facts through the graph reader before attaching and verifying host-local evidence. Removal needs a selective fact/membership verification replacement. |
| [CloudflareProjectInventoryRepository](../../packages/core/src/coding_trajectory/control_plane/remote_inventory.py), [catalogRead](../../cloudflare/control-plane/src/catalog.ts) | `project.list` lists registered projects, with `modified_since`; hosted `projects` derives projects from nondeleted published artifacts. Empty registered projects and filter/result shapes differ. They cannot be redirected without an explicit population contract. |
| [dispatchLive / query / health](../../packages/plugins/datahub/web/worker/live.ts) | Snapshot requests `ct_published_catalog(limit=1)` for its revision. `health()` returns fixed `ready:1` and zero failure counts without source observations. Detail reads fetch `canonical` with graph, all retained trees and items before selecting. Item requests can perform up to eight such graph-bundle reads. |
| [build_read_projections](../../packages/core/src/coding_trajectory/control_plane/read_projections.py), [list variant builder](../../packages/core/src/coding_trajectory/control_plane/collector.py) | Separate graph-to-DocumentStore construction for four list variants and detailed projections. The 128 KiB detail budget can drop graph output, individual trees and later item batches. It bounds stored output, not all computation, and does not carry an explicit per-resource coverage manifest. |
| [Browser API](../../packages/plugins/datahub/web/src/api.ts), [delivery provider](../../packages/plugins/datahub/web/src/hooks/use-datahub-delivery.tsx) | Session requests expose cursor pagination; `fetchProjects()` has no continuation argument. Delivery consumes snapshot/change revisions and resets route queries on gaps. Adding server pagination alone does not migrate all browser callers. |

### Revision domains already diverge

`State.head()` advances for more than publication. `ct_publication_watermark`
queries artifact/publication-watermark records, while the catalog and change feed
use `max(sequence)` from `published_catalog`. Project registration also changes
project metadata independently. Before switching bootstrap to the existing
watermark operation, establish one visible revision definition for catalog,
status, details and changes. A revision change with no affected artifact row, or a
project metadata edit with no publication, needs a specified effect on readers.
Do not numerically substitute one revision domain for another.

## 3. API keep / replace / remove map

“Remove after migration” is a deletion gate. Proposed resource operations below
are logical contracts, not newly available RPC names. Existing public envelopes
can remain, but incompatible paging/availability changes need versioned method
schemas generated from Pydantic definitions.

| Existing API or boundary | Decision | Callers and migration steps | Necessary data/consistency |
| --- | --- | --- | --- |
| `datahub.capabilities` | **Keep**, qualify per method | Hosted `dispatchLive` and browser source/capability discovery; advertise independently from data coverage | Supported operations and versions; a supported operation can have unavailable data |
| `datahub.snapshot` | **Replace backing read** | `fetchDatahubSnapshot` / delivery provider → metadata-only publication status | Published revision/time, authority observation, retained floor and explicit unknown health; no catalog rows or R2 bodies |
| `ct_publication_watermark` | **Keep and consolidate** | Currently exercised by upload qualification; adopt for bootstrap only after revision semantics match catalog/change reads | Latest visible publication status, optional project scope; remove ordinary arbitrary old-sequence input |
| `ct_workspace_snapshot` and metadata-only `ct_historical_snapshot` | **Replace publication uses**, then **remove redundant historical status branch** | Runtime factory and `CloudflareHistoricalRepository.pin_snapshot`; use publication read context for canonical queries. Estimation/living may still require their own consistent state selection | Do not replace an estimation/workspace fence with a publication-only integer |
| `ct_published_catalog(kind=sessions)` | **Keep concept, replace shared contract where necessary** | Hosted sessions, then Python `project.sessions`; common indexed selection, filters, ordering, coverage and cursor behavior | Summary fields requested by the operation; one retained selection across pages |
| `ct_project_sessions_projection` | **Consolidate and remove after parity** | `CloudflareHistoricalRepository.response_for`; migrate all four include variants and `modified_since`, vendor and project filters to common catalog result shaping | Existing Core all-items result needs a versioned bounded page contract or an explicitly capped compatibility collector of pages |
| `ct_project_inventory_snapshot` / `ct_published_catalog(kind=projects)` | **Consolidate access, preserve populations explicitly** | Python `project.list`, hosted projects and browser `fetchProjects`; common project index with explicit registered/published population and supported filters | Identity-based paging; retain empty registrations where required. Name-keyed Core result still needs duplicate-name behavior |
| `ct_published_catalog(kind=detail)` | **Replace** | Hosted `session.graph`, `session.tree`, `session.items` → typed resource/projection reads | Requested graph summary, session tree page or item IDs; no sibling projections; parent read token when needed |
| `ct_historical_artifacts` and non-metadata `ct_historical_snapshot` | **Remove from ordinary reads after migration** | Python runtime, estimation, local-evidence verifier; qualification scripts migrate to supported operations | Selective canonical facts/projections. Keep a temporary bounded compatibility adapter until these callers work; do not turn it into a permanent export requirement |
| `CloudflareHistoricalRepository.store_for` / remote `DocumentStore` reconstruction | **Replace boundary** | Runtime `response_for`/`store_for` fallback, estimation and local evidence | A repository should answer typed queries or return bounded canonical resources. Existing reducers remain canonical; do not port formulas independently into TypeScript |
| `ct_artifact_chunk_manifest` / `ct_artifact_chunks` | **Keep storage authorization primitive; replace product read role** | Repository search found qualification callers, no ordinary browser/Python reader. Resource lookup should resolve authorized manifest membership internally | Digests and membership are internal storage concerns. Keep raw chunk API only if migration/recovery tooling actually uses it; otherwise retire its reader exposure |
| `ct_publication_changes` / `datahub.changes` | **Keep, align revision and retention** | Delivery provider; current adapter requests one page and resets on overflow | Bounded changed identities and tombstones, fixed upper revision, reset on expired interval. Coarse invalidation remains valid; incremental row patches need demonstrated benefit |
| `ct_collector_missing_chunks` / `ct_collector_upload_chunks` | **Keep behavior** | `CloudflareCollectorRemote.stage_artifact_payload` used by `UploadService._deliver` | Bounded content-addressed packs, ownership, digest validation and retry; producer should supply durable changed-node references |
| `ct_collector_stage_chunk_manifest` | **Replace validation/storage path** | Chunked collector → manifest-native candidate validation → ready descriptor | Validate resource schemas, ownership, topology closure, dependencies and projections in bounded resumable work; no whole-graph reserialization requirement |
| `ct_collector_stage_artifact_payload` | **Remove after old writers/readers migrate** | Nonchunked `CloudflareCollectorRemote` plus internal chunk-stage bridge; first separate shared validation from gzip decoding | A decoding entry point for legacy uploads only during migration. Do not require new chunk callers to encode gzip to reach common validation |
| `ct_collector_publish_artifacts` | **Keep semantics, replace artifact-oriented manifest input** | `UploadService._deliver`, `commit_artifact_manifest` | Atomic visible references/indexes/change position/receipt; preserve source fences, ownership, explicit replacement scope and idempotency |
| Project/source registration, checkpoint, recover RPCs | **Keep guarantees; consolidate only where justified** | Collector preparation/delivery/reconcile; `ct_project_register`, `ct_collector_register_source`, `ct_collector_publish_observation`, `ct_collector_recover` | Durable lineage and recovery positions; checkpoint receipt is not publication success. Endpoint combination is secondary to eliminating full-body work |
| `ct_remote_living`, heartbeat and living observations | **Keep separate timing semantics** | `CloudflareLivingAuthority`, collector living publishers | Leases and evaluations; may share identity/index infrastructure with catalog, never substitute lease freshness for publication age |
| `estimate.*` and internal estimation RPCs | **Keep operations, replace input acquisition** | `RemoteEstimationAuthority` and estimation workers | Forecast records directly for get/list; bounded candidate/outcome queries for planning/comparison. Freeze required feature inputs, digests and reducer/model versions for reproducibility |
| `--snapshot-sequence`, HTTP top-level sequence, Datahub method sequence parameters | **Replace ordinary use with opaque read context; deprecate arbitrary history explicitly** | CLI remote flags, runtime factory, hosted query parameters and qualification clients | New requests select latest; continuations/expansions carry server-issued bounded tokens. Separate optional retained-evidence access from browsing |
| Local-only search/events/content/narrative | **Keep** | Core runtime and `LocalEvidenceRepository` | Raw evidence stays on host. Selective published fact proof must preserve the existing mismatch rejection |

### Every Core canonical method needs coverage before fallback removal

The [authority map](../../packages/core/src/coding_trajectory/control_plane/authority.py)
routes these families through the historical handler today. A replacement must
either preserve their shared semantics or explicitly narrow advertised shared
capabilities; a supported method must not silently return an empty approximation.

| Methods | Replacement input/result |
| --- | --- |
| `project.sessions` | Paged summary projection, preserving include/filter semantics |
| `session.overview`, `session.summary`, `session.tree` | Session summary, summary reduction and paged topology as required by each contract |
| `graph.overview`, `graph.stats`, `graph.usage` | Graph topology plus canonical aggregate projections tied to dependency versions |
| `session.stats`, `session.usage`, `session.model_usage`, `session.request_usage`, `session.tool_usage` | Corresponding canonical projection or bounded fact scan with the existing reducer; distinct grouping/filter semantics |
| `session.items` without content | Requested metadata IDs or turn-scoped pages with coverage and membership validation |
| `session.search`, `session.events`, content-bearing `session.items`, narrative-bearing `graph.overview` | Local evidence path only, consistent with current remote exclusions |

## 4. Proposed read contract

1. No token means latest committed data for the requested source/workspace/scope.
   The response exposes its publication revision and observation/publication times.
2. A continuation is opaque to callers and binds authority incarnation, workspace,
   method/version, filters, ordering, projection version, relative-time evaluation,
   selected revision and position. Use authenticated tokens or server-held state;
   current base64 JSON cursors are not a tamper-proof selection or expiry policy.
   Authorization still runs on every request.
3. A bounded read token may also support parent/detail expansion across methods;
   a method-specific pagination cursor cannot be reused as that token. Define
   this distinction before removing explicit sequence parameters.
4. Expiry returns a typed reset/expired-selection result, never silently serves
   later pages at latest. Clients restart the list and invalidate incompatible
   detail caches. Resource-not-found, projection-unavailable, unsupported and
   budget-exceeded remain distinct; current hosted catch-all `unavailable` cannot
   carry the entire migration contract.
5. Coverage distinguishes absent resources from omitted/rebuilding projections.
   Page count/bytes and work are bounded; aggregate computations that need scans
   have bounded continuation or precomputed canonical results. Do not sum
   non-additive graph totals across hosts.
6. Publication time, last authority contact and source observation/lease expiry
   are different fields. Unknown source counts must be nullable or explicitly
   unavailable in a versioned Pydantic response, not invented zeros. `generated_at`
   is response time, not publication time. The hardcoded minimum revision and
   36500-day bootstrap coverage also need actual retention/coverage evidence.

## 5. Data model and stored-copy decisions

| Current representation | Required purpose | Target and deletion gate |
| --- | --- | --- |
| Raw host source files | Evidence, reparse, local content | Keep local; never upload raw bodies to replace shareable canonical data |
| `canonical_versions.body` containing whole captures | Durable local facts and upload replay | Replace with resource versions, immutable bounded blocks, membership and a change journal. Commit source offsets with resource changes. Retain bytes referenced by unread captures/outbox/read selections |
| `sync_batches.body` containing another full artifact | Frozen retry identity and independent delivery durability | Store immutable resource references plus frozen fences/projection descriptors. Remove body duplication only after referenced bytes survive repository compaction, crash and offline retry |
| R2 chunk packs plus `upload_chunk_objects` / `upload_members` | Immutable payloads, efficient physical reads, authorization | Keep logical content addressing and packing; use typed resource/page indexes so readers do not walk a graph to locate one item. Reference membership must remain exact |
| R2 whole-artifact gzip | Current historical transport/rollback | Transitional copy; remove after Python/estimation/local-evidence and nonchunked-writer migration, replay qualification and a bounded rollback window |
| Four list variants in `projections` | Existing `include` result semantics | Prefer one validated summary with selectable optional groups when parity proves equivalent; retain genuinely different reductions. Projection versions must be tied to canonical dependencies |
| Monolithic `canonical` detail projection | Hosted graph/tree/item availability | Replace with individually indexed bounded projections/pages and explicit coverage, invalidated only for affected dependencies |
| `records(kind='artifact')` payload duplicated in `published_catalog` | Current authority plus migrated read index | Converge on one authoritative committed resource/manifest catalog with narrow indexes and a separate bounded change log. Finish/qualify migration before deleting trigger/backfill compatibility |
| All prior artifact/catalog/project versions | Present arbitrary sequence API; cursor consistency | Keep current heads plus versions needed by unexpired read contexts, unfinished writes, recovery and explicit evidence pins. Define time/size policy before enabling GC; no retention duration is established by this review |
| Publisher/checkpoint/receipt state | Ownership and exactly-once effects under retries | Keep recovery/idempotency semantics independently of reconstruction retention; payload GC must not erase replay protection |
| Estimation forecast/evidence/comparison records | Reproducibility and calibration | Preserve required frozen sanitized inputs/outcomes and provenance. Whether exact reconstruction replay is required remains a product decision; candidate queries alone do not establish it |
| Local Datahub `entity_versions` and change log | Local indexed reads and pagination | Keep their bounded-consistency purpose; align query semantics through adapters. A shared API does not require merging every local store or adopting remote storage physically |

The target local execution path is source delta → validated canonical resources
and checkpoint transaction → durable capture references → missing bounded nodes →
manifest validation → atomic publication. Initial import still processes all
selected evidence, but does so with bounded work/resume checkpoints. Incremental
updates should touch changed resources and affected ancestors/projections. Vendor
adapters that cannot yet do this retain an explicit bounded rebuild path until
qualified; transport chunking must not claim incremental parsing.

The shared read path is authenticated query → one committed selection → indexed
summary/resource lookup → bounded projection/fact page → response. Python and
Worker adapters map the same domain results to their public envelopes. Whole
workspace `DocumentStore` reconstruction is absent from this target path.

## 6. Migration order and removal gates

| Step | Concrete work and owners/modules | Required exit evidence |
| --- | --- | --- |
| M1: Freeze operation contracts | Core Pydantic contracts, catalog protocol, Datahub response models and CLI envelope definitions: define project populations, paging, read tokens, availability and revision domains; resolve estimation reproducibility requirement | Caller matrix above reconciled with all include/filter shapes; generated schemas/types agree; no implicit promise of indefinite history |
| M2: Unify status and catalog | Authority `catalog.ts` / publication metadata, hosted `live.ts`, Python inventory/list adapters, browser API/delivery consumers | Empty/populated workspace and empty registered projects behave correctly; status reads no catalog rows/bodies; project rename, publication, staging, heartbeat and estimator activity advance only the intended revision domain |
| M3: Add selective canonical resources | Core projection builders and query repository boundary; authority resource/projection indexes; local and hosted adapters | Graph/tree/items and every advertised Core family have explicit coverage, bounded reads and canonical parity. Parent expansion under concurrent publication stays consistent or expires explicitly |
| M4: Migrate reconstruction callers | Runtime dispatch, `remote.py`, `local_evidence.py`, `remote_estimation.py`, CLI old-sequence option and qualification scripts | Ordinary queries never invoke historical artifact RPCs or build remote `DocumentStore`; estimation get/list avoid workspace hydration; comparisons/evidence matching still work. Unknown external callers get an explicit compatibility/deprecation boundary |
| M5: Make preparation and staging resource based | Vendor ingestion, canonical repository/outbox, collector transport, authority upload/commit | Small append does not rebuild/serialize/hash the whole graph; unrelated resources retain identities. Validation resumes with bounded work; crash/retry/source replacement/ownership/idempotency behavior preserved |
| M6: Remove redundant paths and copies | Delete legacy stage/read routes, gzip bridge, duplicate detail bundles, obsolete catalog authority/backfill and unneeded chunk reader exposure | Caller search and end-to-end qualification pass; all retained current resources readable; pending uploads/receipts and required evidence pins survive. Enable reference-safe retention only after rollback and expiry behavior are exercised |

M2–M4 can improve read costs before M5 is complete, but must not be declared the
whole architectural cleanup. A small early extraction that avoids gzip followed
by immediate decompression is valid within M5; it must still preserve validation
and cannot be presented as manifest-native staging or removal of the gzip copy.

Do not remove an old route solely because the hosted adapter no longer uses it.
The Python CLI/API runtime, estimation, optional local evidence, nonchunked
collector and qualification tools have distinct migrations. Repository search
cannot prove there are no out-of-repository API clients.

## 7. Qualification and limits of this review

Use existing qualification workflows and controlled scenario runs; do not add
unit tests for this review. Future changes should record:

- Results from `qualify-published-catalog.mjs`, `qualify-incremental-upload.py`,
  `qualify-cloudflare-control-plane.py` and `qualify-live-datahub.py` after updating
  their contracts and confirming their target/side effects.
- Concurrent publication between pages and between tree/item expansion; expiry,
  invalid cursor scope, authority reset, deletion and project rename behavior.
- Same sanitized canonical input produces equivalent supported local/shared
  results, including unavailable usage, projection-only items and overlapping
  graphs. Metric changes require evidence-based baseline reconstruction.
- Measured source bytes reparsed, resources hashed, peak preparation memory,
  staging work, stored copies, SQL rows visited, object reads and transferred
  bytes. A narrow item request must not scale its body transfer with graph size;
  an append must demonstrate bounded affected-resource work, not merely smaller
  HTTP uploads. No new capacity or latency guarantee is claimed here.
- Interrupted local capture, interrupted staging, commit-before-ACK loss, stale
  ownership/source fences and expired read tokens, including rollback/GC pins.

This review traced the current code and contracts without calling the remote
authority or changing runtime behavior. The full committed metric baseline
workflow (`uv run python scripts/validate-metrics-baselines.py`) passed all four
cases: 107 assertions and 49 invariants. This checks the existing metric baseline,
not the proposed architecture or a deployed migration.

Before implementation, reconcile this proposal with `contracts.md` (arbitrary
historical selection and retention), `read-path.md` (historical-token vocabulary),
and `implementation.md` (compatibility/deletion gates). Their earlier mechanisms
are evidence of prior design, not reasons to retain whole-graph execution. This
document leaves those existing specifications intact until the replacement
contracts are adopted.
