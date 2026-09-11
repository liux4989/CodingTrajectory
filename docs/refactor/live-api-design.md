# Live API remediation design

Status: design and policy choices accepted; implementation in progress. No deployed
contract or payload-retention behavior has changed. This is the design for the open
findings in [live-api-review.md](live-api-review.md), not a claim that M1–M6 are
complete. Existing public contracts remain authoritative until versioned changes
land. `ct_catalog_read_v2` now implements the additive catalog-selection slice
locally; browser/Core consumer cutover and selective detail resources remain open.
Other logical operation and model names below remain implementation targets.

## 1. Scope and decisions

Replace whole-graph execution at two boundaries: shared query acquisition and
durable preparation/publication. Deliver the read migration first; do not make it
depend on an incremental parser rewrite. Keep Python's canonical measurements,
manual/automatic publication policy, host-local raw evidence, ownership fences,
atomic publication and durable replay.

Accepted contract decisions:

- Ordinary navigation selects latest published data. Continuations and related
  expansions retain a bounded selection; arbitrary reconstruction history is a
  deprecated compatibility capability, not the browsing contract.
- A read selection pins publication and project metadata separately. Project
  edits become visible without publishing a session; heartbeats and estimation
  writes do not invalidate catalog views.
- Both adapters use one typed resource/query contract. The Worker shapes stored
  canonical results; it does not reimplement Python reducers.
- Registered and published projects are explicit populations. Do not silently
  remove empty registered projects from Core or add them to Datahub browsing.
- Retain frozen estimation inputs/results/provenance, not every workspace
  reconstruction. This preserves evidence inspection, not a promise that an
  external model will reproduce identical output.
- Introduce authenticated, expiring selections before enabling payload GC.
  Exhausted storage must reject new work explicitly rather than evict live pins.

Non-goals: raw-content hosting, a new graph export product, merged local/remote
storage, independently ported metrics, deployment during design, or a claim of
larger-graph support while compatibility staging retains its 8 MiB ceiling.

## 2. Revision and selection contract

Keep four domains distinct: publication revision, project-metadata revision,
estimation/workspace fence and living observation/evaluation. Publication revision
advances once per visible publication transaction, including tombstones; staging,
registration, leases and forecast records do not advance it. Project registration,
rename and metadata deletion advance the project-metadata revision instead.

Select both catalog heads atomically in the workspace coordinator. A server-held
`ReadSelection` contains authority incarnation, workspace, authorized scope,
publication revision, project-metadata revision, selection/evaluation time,
expiry and supported schema/projection versions. Return an unguessable token;
look it up and reauthorize on every request. It grants no access by possession.
The same bounded token can serve lists and parent/detail expansion in its scope.

Use an authenticated method-specific cursor containing selection identity,
method/schema, normalized filters, ordering, projection version and last key.
Freeze relative-time cutoffs at selection creation. Changing parameters or using
another method's cursor is `invalid_cursor`, not an implicit new query. Cursor
and explicit selection must agree. Keep current 200-row and 512 KiB catalog
ceilings initially; declare separate node, item-batch and query-work limits in
the versioned schemas before enabling their operations.

Accepted initial selection lifetime: 30 minutes, fixed rather than sliding.
Refresh creates a new selection. This is a policy choice, not a measured UX
requirement; qualification must exercise long browsing and grouped CLI calls.
Pin creation and GC eligibility checks serialize through the authority. A token
does not permit selecting an arbitrary older revision. Authority restoration
changes incarnation and invalidates all old selections.

Status returns publication head/time, project-metadata head, authority observation
time, actual retained floors, and known coverage with nullable unknown values.
It reads metadata only. Empty workspaces have revision zero and null publication
time. A retained floor does not imply every historical integer is selectable.
Do not derive source coverage from retention or replace it with a fixed day count.

The existing publication feed remains publication-only: freeze its upper revision
and page the complete interval `(acknowledged, upper]` by revision/change identity.
A separate project-metadata head in bootstrap is sufficient for coarse project
and name-filtered session invalidation; no second detailed feed is required yet.
On a metadata-head change, new navigation sees the edit while existing selections
retain the old metadata. Reset on expired feed intervals or authority incarnation
changes. A client acknowledges the upper bound only after consuming every page.

## 3. Typed query boundary and availability

Define strict Pydantic domain request/response models beside `catalog_protocol.py`
and generate Worker schemas/types using the existing generation workflow. Keep
Core and Datahub envelopes; version incompatible method shapes explicitly.
Replace `store_for` with typed query results or bounded canonical fact pages.
Do not conceal a legacy graph fetch inside the new repository interface.

Every successful page includes selection token/expiry, selected revisions,
coverage, data and optional next cursor. Capability support is independent of
whether a selected projection exists. Define these distinct outcomes:

| Outcome | Meaning and client handling |
| --- | --- |
| Complete, including empty | Requested scope is known and fully represented |
| Partial | Explicit covered/missing scopes; only methods permitting partial data may return it |
| Not found | Membership index proves absence at this selection |
| Projection unavailable | Resource exists; requested version is absent, rebuilding or failed |
| Unsupported | Method/version or local-only field is not supported here |
| Budget exceeded | Cannot satisfy the request under its contract; never truncate silently |
| Selection expired/reset | Discard incompatible pages and details; restart explicitly |
| Invalid cursor | Malformed, unauthenticated or mismatched cursor; do not retry at latest |

Authentication/authorization and transport failures remain separate errors. Item
batches report per-ID outcomes without revealing unauthorized membership.
Remove the hosted catch-all conversion to `unavailable` only when clients handle
these distinctions. Never convert unavailable usage into zero or an empty list.

Projects page by stable project ID with `population=registered|published`.
Published means at least one nondeleted committed session at the selection;
registered includes empty projects. Names and `modified_since` use pinned project
metadata. Preserve the current Core name-keyed compatibility response and its
duplicate-display-name error; v2 returns identity-bearing rows. Browser
`fetchProjects` must consume continuation explicitly.

Sessions page by descending activity time and ID tie-breaker. Preserve vendor,
project and `modified_since` semantics and all four include variants through a
validated summary with selectable groups only after parity proves equivalence.
Legacy all-items callers may collect pages within an explicit total budget; on
overflow fail with a paging migration error, not a shortened success response.

## 4. Selective storage and canonical computation

Use one authoritative committed manifest catalog plus narrow query indexes.
Logical records (physical table names follow implementation review):

- Resource versions: kind, semantic ID, owner/source epoch, immutable payload
  reference, schema and content digest.
- Manifest membership: manifest identity and exact resource-version membership,
  including topology/dependency references and tombstones.
- Projection descriptors: operation/parameter shape, version, dependency digest,
  payload page references, coverage and failure/rebuild state.
- Visibility intervals: scoped manifest/resource/index references selected by
  publication revision. Candidate indexes remain invisible until commit.
- Summary/topology/item indexes: project, session, turn and item lookup keys,
  ordered page keys, and physical pack location/length for direct reads.

Index membership by selected manifest and resource ID. Item lookup resolves only
authorized requested resources; tree lookup returns bounded topology pages;
graph lookup returns the requested summary/aggregate, never sibling trees/items.
Bound physical packs as well as logical pages, so one item does not require
fetching a graph-sized pack. Record resource/projection dependency versions in
coverage; absent detail output is never evidence that an item does not exist.

Initially build these resources from existing Chronicle artifacts in Python.
This removes read amplification without claiming incremental preparation. Replace
the 128 KiB all-or-nothing detail bundle with independent projection pages.
Required summary indexes are ready at publication; optional expensive projections
have explicit coverage. Any later projection attachment must validate immutable
dependencies and advance visible publication state rather than mutate an already
selected response's coverage silently.

Maintain a per-method coverage registry for every family in the review:
`project.sessions`; session overview/summary/tree; graph overview/stats/usage;
session stats/usage/model_usage/request_usage/tool_usage; metadata items. Inventory
all include/filter/grouping combinations before selecting projection keys.
Finite common shapes may be precomputed with existing Python dispatch/reducers.
Other shapes use bounded fact scans through those reducers; non-pageable exact
aggregates require a qualified materialization or explicit budget failure.
Do not advertise hosted support for unimplemented reductions. Existing Python
methods stay on the declared compatibility path until their full coverage lands.
Never sum per-host graph totals: deduplicate canonical identities and preserve
overlap, projection-only items and unavailable measurements.

## 5. Migrate non-browser consumers

`runtime.py` and `remote.py` route covered shared methods directly to the typed
repository. Grouped calls explicitly reuse a read selection; independent calls
select latest. Remove historical fallback only after every advertised method is
covered, including filters, rather than after the first successful graph query.

`remote_estimation.py` get/list/backfill-status read forecast records directly
under their own fence. Any requested actual-outcome refresh is a separate bounded
query. Predict/bind acquire scoped candidate features and relevant turn outcomes;
preserve existing candidate ranking/exclusion/retrieval semantics across all pages,
not merely the first page. Calibration pages comparison records through the
existing reducer. Durable plans freeze sanitized feature inputs, selected examples,
outcomes when compared, digests and reducer/prompt/model versions. Preserve current
evidence until inspection proves those records suffice; do not drop reconstruction
pins solely because get/list no longer hydrate a store.

`local_evidence.py` requests selected membership and canonical fact fingerprints,
then verifies host-local evidence before serving content/search/events/narrative.
An absent or mismatched local source still rejects the attachment. No raw source
body crosses the shared boundary, including through estimation evidence records.

CLI/HTTP explicit sequence inputs remain a labeled, bounded legacy surface while
callers migrate to selections. Do not reinterpret a workspace sequence as a
publication revision. Document deprecation and negotiated support for unknown
external clients; removal requires a release boundary, not repository search alone.

## 6. Durable preparation and manifest-native publication

In `canonical_repository.py` store immutable bounded resource blocks, membership
and a change journal rather than whole `canonical_versions.body` captures.
Commit checkpoint offsets and changed resources atomically. Freeze batch identity,
source fences, replacement scope and referenced payloads when capturing an outbox;
capture and consumed-cursor advancement are atomic. Retain references until receipt
recording and all other local pins release them. Cross-store capture must use a
durable retention handshake, not assume two database commits are atomic.

Vendor adapters incrementally parse complete appended records and update changed
resources plus affected ancestors. Initial imports checkpoint bounded progress.
Truncation, replacement, parser-version changes and adapters without incremental
semantics use an explicit resumable rebuild path. Partial trailing records do not
advance the complete-record checkpoint. Reuse immutable completed blocks; stable
resource IDs alone do not prove bounded hashing or parsing.

`collector.py`/`upload_chunks.py` negotiate durable changed-node references before
loading payloads. Ingress validates candidates in resumable bounded tasks: digests,
schemas/privacy, exact membership, ownership, topology closure/cycles, dependency
versions and projection descriptors. Reuse validated unchanged subtrees only with
matching validation version and dependency identity. Reject total/depth/fan-out
budget violations explicitly. Durable progress binds candidate digest and validator
version; retries cannot attach results from another candidate.

Stage large indexes invisibly and mark an immutable ready descriptor only after
validation completes. The serial publication transaction rechecks current ownership,
source/base fences and readiness, then swaps bounded visible manifest references,
advances publication/change position and stores the receipt atomically. It must
not copy every resource index row into the transaction. Explicitly split batches
that exceed the root/commit budget; never pretend they published atomically.
Same batch/content returns its receipt after ACK loss; changed content conflicts.
Omitted resources are not deletions without an explicit fenced replacement scope.

## 7. Retention, migration and rollback

Retain current heads plus dependency closure referenced by live selections, pending
local captures/uploads, committed recovery needs, explicit estimation evidence pins
and rollback. Keep replay protection independently of payload lifetime. Use
mark/eligibility, grace and serialized recheck before deletion; unreferenced staging
cleanup must not race an active validation task or commit.

Use a seven-day rollback window after the last legacy writer/reader cutover;
actual deletion still requires operational approval. Legacy history retained
during migration is not a new indefinite-retention promise. Do not enable GC until
expiry, restore and rollback qualification passes. Once new-only resources cannot
be decoded by old binaries, rollback retains a compatible reader or disables new
writes; a binary downgrade alone is insufficient.

| Slice | Scope / owners | Exit and deletion gate |
| --- | --- | --- |
| D1 / M1 | Pydantic contracts, generated types, caller/parameter coverage registry | Versioned shapes and policy decisions reviewed; no runtime removal |
| D2 / M2 | `catalog.ts`, workspace metadata, inventory/list adapters, browser delivery | Metadata-only status; two pinned catalog revisions; authenticated selections; project pagination and typed reset handling |
| D3 / M3 | Projection builder, resource indexes, typed repository, hosted details | Same-revision parity for graph/tree/items; narrow physical reads; explicit coverage |
| D4 / M3–M4 | Remaining Core families, estimation, local evidence, CLI/HTTP | Every supported shape qualified; ordinary queries invoke no historical artifact RPC or remote graph hydration |
| D5 / M5 | Canonical repository/outbox, vendor adapters, chunk producer, ingress/commit | Bounded changed-resource work and resumable manifest validation; recovery/fencing preserved |
| D6 / M6 | Compatibility routes/copies, authority catalog convergence, GC | Caller migration, retained-resource reads, rollback window and reference-safe collection qualified |

Backfill existing committed artifacts into resources in bounded resumable jobs,
preserving original publication identity and ownership. Shadow-compare at the same
selection before switching readers. Keep trigger/backfill compatibility until the
new catalog is authoritative; never republish all sources or reset the database as
a migration shortcut. Shared backfills, deployment and deletion require separate
authorization. This design authorizes none of them.

## 8. Qualification and specification reconciliation

Use existing integration qualification/scenario workflows; add no unit tests.
Inspect each script's target and side effects before execution. Extend
`qualify-published-catalog.mjs`, `qualify-incremental-upload.py`,
`qualify-cloudflare-control-plane.py` and `qualify-live-datahub.py` as slices land.

- Exercise empty registrations, duplicate names, rename without publication,
  staging/heartbeat/forecast writes, deletion and two independent collectors.
- Publish between list pages and parent/item expansion. Verify old selections
  remain consistent; expired/forged/wrong-scope tokens and restored incarnations
  never continue at latest. Freeze time filters across a boundary.
- Compare every advertised parameter shape against canonical Python results on
  sanitized fixtures with overlapping graphs, unavailable usage and projection-only
  items. Metric-sensitive changes run both required repository metric workflows;
  expected values come from committed source evidence, never new output alone.
- Measure SQL rows visited, object requests and bytes for fixed item IDs as graph
  size increases. Response limits alone do not pass the selective-read gate.
- Measure bytes reparsed, resources hashed, peak memory and validation work for
  a fixed append as history grows. Explain any ancestor/global reduction work;
  a smaller network transfer alone does not pass the incremental gate.
- Inject interruption around checkpoint/capture, stage readiness, commit-before-ACK,
  receipt persistence and GC recheck. Exercise stale owner/source fences, pending
  outbox migration and retained evidence after reconstruction expiry.

`contracts.md` and `read-path.md` now distinguish bounded selections from explicit
retained evidence and describe the revision tuple. `implementation.md` records
the additive catalog slice, remaining D1–D6 gates and rollback limits. Their prior
implementation evidence remains intact; consumer contracts change only when the
corresponding migration lands.

The 30-minute selection lifetime, separate project-metadata revision,
evidence-based estimation retention without arbitrary reconstruction replay and
seven-day rollback window are accepted. Retain existing data and compatibility
paths until the corresponding implementation and qualification gates pass.
