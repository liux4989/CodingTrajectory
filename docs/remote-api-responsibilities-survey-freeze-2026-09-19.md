# Remote API responsibilities and evidence freeze — 2026-09-19

Status: design implemented locally after the owner's subsequent implementation
request, with the original read-only survey and measurements retained below as
historical evidence. The new contract supersedes the survey's open choices.
Local implementation and checks do not qualify Stage 3 or authorize deployment,
remote workload, publication, cleanup, push, or a pull request.

The owner approved broad remote support, the delivery order below, and separating
overview from detail. The latest clarification makes **preserving session context
for internal use the product goal**, rather than minimizing disclosed context as
if these were public exports. Performance remains a constraint to measure, not a
reason to silently discard useful information. The path decision is settled:
**keep full paths**, with no shortening proposal or
keep-versus-trim experiment. **Support only the new version**, including its
clients, behavior and links. Local/offline use remains supported, but preserving
old contracts, clients, links, cursors or prepared artifacts is not a requirement
for the next design. Early breaking changes and loss of obsolete derived state
are acceptable; this is not permission to delete canonical source data.

The shared local/publication projection now retains bounded semantic command
arguments and tool target paths instead of executable-only allowlisting and
blanket host-path rejection. [P1] This is a context-fidelity change, **not a proven
performance optimization**: richer facts can increase preparation, storage,
transfer, parsing and response costs. Removing some sanitization work does not
establish a net speedup. The direct-overview design must account for that cost
without treating the old lossy projection as the desired product contract.

## Responsibilities agreed for the next design

| Boundary | Responsibility | Must not become |
| --- | --- | --- |
| Local/Core Python | Own canonical computation and local/offline APIs for the new version. Update owned consumers together. | Compatibility adapters or dual-version support. |
| Publication/preparation | Compute reusable summaries, cards, topology, metrics, indexes, packs, and canonical serialization once per immutable view. Preserve bounded semantic context under the internal-use policy and exact source/view identity. | Request-time repeated graph reconstruction, a lossy public-export filter, or blanket permission to upload raw data. |
| Worker direct reads | Authenticate/authorize each request, validate scope/snapshot/cursor, resolve the authoritative prepared view, verify object bounds and integrity, select and return bounded results. | Cached authorization, skipped SHA verification, mixed stale/pending snapshots, or silent truncation. |
| Durable Object authority | Own workspace authorization-related resolution, publication/snapshot state, and bounded mutable SQL/cursor semantics. | A duplicate per-request metrics or summary engine. |
| Overview response | Preserve session context through useful self-contained navigation, bounded narrative and tool activity, exact identities/references, ordering, pagination, totals, and coverage. | Generic labels that erase useful activity context, or a container for every available diagnostic, metric, item, or event. |
| Detail APIs and clients | Load item/event details and metrics as needed; make new-version evidence links and snapshot identity work end to end. | Legacy-link preservation, or broken drill-down in the supported version. |

Existing `POST /v1/core` accepts control-plane `ct_*` primitives and artifact
locators; Python reconstructs facts and runs canonical handlers. Planned direct
APIs serve prepared high-level results without requiring Python on that request
path. These are distinct boundaries, not evidence that all high-level APIs are
already remotely served. The registry has 18 high-level methods; the strict
graph overview v4 candidate is deliberately unregistered/gate-off. [S1, S2]

## API survey and agreed delivery order

Early support means a delivery priority, **not current availability or proof of
Free-plan qualification**. Exact results that exceed a supported unpaged bound
must return `remote_result_too_large` rather than partial success. The new
contract and bounds are specified below. Method versions in this survey table
identify the surveyed source, not a promise to keep those versions supported.

| Method/version | Observed consumer or demand | Category and qualification prerequisite |
| --- | --- | --- |
| `project.list` v4 | CLI discovery; Loop project selector. [S3, S6] | Foundation: prepared inventory; pagination/versioning before larger-than-supported inventories. |
| `project.sessions` v4 | CLI list; Loop Explore; Monitor watch scope. [S3, S6, S7] | Foundation: prepared cards, stable root/member IDs, zero-item visibility; no N graph reads to make a list. |
| `session.overview` v3 | CLI chronology/tree; Loop first paint and older pages. [S4, S6] | Foundation: prepared visible turns; check order, previews, visibility, IDs and paging in the new version; measure selected reads. |
| `session.summary` v2 | CLI brief; Loop objective, decisions, changes, verification and evidence links. [S4, S6] | Foundation: exact whole-session/turn variants; compact orientation, not a substitute for detail. |
| `session.tree` v3 | CLI conversation lineage/forks. [S4] | Foundation: prepared topology. The new request rejects `turn_id`; no compatibility shim. |
| `session.stats` v3 | CLI context/runtime/message/token report. [S4] | Foundation: exact prepared result. |
| `graph.stats` v3 | CLI aggregate/per-session report. [S5] | Foundation: exact prepared result. |
| `session.usage` v3 | CLI accounting; Monitor per-turn evaluation. [S4, S7] | Foundation: exact whole-session/turn variants; operationally important. |
| `graph.usage` v3 | CLI token/cost aggregate. [S5] | Foundation: exact bounded result, including composition. |
| `session.model_usage` v3 | Documented advanced provider/model grouping; survey found no dedicated first-party production caller. [S8] | Foundation: straightforward preparation; retain despite unknown external demand. |
| `session.request_usage` v3 | CLI ledger; Monitor provider-request counts by turn. [S4, S7] | Foundation: exact fitting whole-session/turn ledger; explicit size error otherwise. A paged version is a later design. |
| `session.tool_usage` v3 | Documented advanced per-tool diagnostics; survey found no dedicated production caller. [S8] | Foundation: exact fitting prepared result; preserve attribution/pricing versions. |
| `session.items` v4 | CLI metadata; Loop paged items and exact evidence resolution. [S4, S6] | First extension: on-demand detail. Add source-order chunks and indexes needed by supported filters; check paging, lookup and read bounds. |
| `session.events` v4 | CLI metadata; Loop exact event and owning-item navigation. [S4, S6] | First extension: on-demand detail. Index accepted turn/event/item/type/status/tool filters; preserve ownership and unresolved-ID diagnostics. |
| `graph.overview` v3 / v4 candidate | CLI v3 hierarchy and edges; no production v4 consumer found. [S5, S9] | Next: implement one new contract and update callers; check global paging and representative CPU cost. No v3 compatibility reader. |
| `living.sessions` v2 | Monitor change polling, watermarks, scope selection. [S7] | Next: highest mutable priority; use bounded SQL and explicit restart on expired cursors, without a long-term retention SLA. |
| `session.search` v2 | CLI ranked evidence discovery. [S4] | Deferred, not removed: start with the simplest useful bounded search semantics and document them; old ranking parity is not required. |
| `living.events` v1 | PRD resource-change protocol; Loop design anticipates it; no production caller found. [S8] | Deferred, not removed: define bounded polling/lease behavior when an internal consumer needs it. |

The foundation contains **12 methods**, followed by **2 detail methods**, then
**2 follow-on methods**, with **2 deferred methods**. Keep this broad support
direction, but let actual internal workflows drive implementation. Speculative
external compatibility, telemetry infrastructure and support-window policies
are not prerequisites.

## Overview field responsibilities

These responsibilities are made concrete in the new contract below. Implement
its schema and owned callers together; no old-version behavior or link
compatibility is required. Existing code schemas are not changed by this document.

| Field group | Frozen direction and evidence |
| --- | --- |
| Graph/root/lineage/entrypoint/session/turn IDs | Keep exact identities for selection, authorization scope and links. [S5, S6, S9] |
| Rank, parent, relationships, complete edges, fork identity | Keep self-contained topology and canonical edge order; distinguish run from lineage/fork. CLI v3 uses these. [S5] |
| Global/session/source ordinals, source sequence, page/cursor | Keep deterministic traversal and snapshot pinning; timestamp order is not a substitute. [S9] |
| Status, timestamps, timestamp state | Keep compact chronology and completion context. [S4, S6] |
| Request/assistant previews | Keep bounded useful previews; full evidence belongs on demand. Smaller preview bounds are a product/contract change, not a transparent optimization. [S6, S9] |
| Semantic tool activity | Preserve bounded action/target descriptions, command arguments, file targets and outcomes through the shared mechanism and the `activities` records below; do not reduce these to executable-only or generic labels solely for public-export sanitization. [P1] |
| Item IDs and request-event reference | Keep precise evidence navigation in the new version. Update link producers and consumers together; old links need no adapter or redirect. [S6, S9] |
| Vendor/title/model | Keep compact labels and interpretation metadata. Richer reasoning/agent diagnostics belong on demand where the revised contract defines them. [S4, S6, S9] |
| `cwd`, `agent_path` | **Keep full path values.** No shortening, omission, separate fetch or keep-versus-trim study. If the complete response exceeds its supported bound, fail explicitly rather than silently shorten a path. This is the next contract's decision, not a claim that current field-length bounds have already changed. |
| Orchestration, non-page totals, visibility and coverage | Keep compact unique information; a single page cannot reconstruct these totals. [S5, S9] |
| Legacy summary and per-session page counts | Reuse or derive redundant values in the new contract; old field compatibility is unnecessary. Keep unique fork and non-page totals. [S9] |
| Usage/cost | Do not add to overview; load the dedicated foundation APIs. |
| Topology repeated across pages | Keep self-contained pages. Do not design a separate topology resource unless observed internal usage demonstrates a need. |

Representative synthetic response accounting was 5,462 bytes: identity/project
about 297, graph/summary/fork 467, sessions 2,200, edges 855, turns 1,327 and
page/coverage 315. These approximate groups and the 3,737-byte topology object
are survey shape measurements, **not production demand measurements**. [S10]

Overview versus detail is a navigation and bounded-work boundary, not a blanket
privacy filter. Bounded semantic descriptions may contain command arguments and
host paths under the internal-use policy. [P1] That does not mean every diagnostic
belongs in overview or authorize uploading raw provider logs, arbitrary payloads,
full tool output or file bodies. Common explicit-credential redaction remains
best effort, not a guarantee that descriptions are secret-free; treat them as
private workspace data. Authentication, workspace isolation, object integrity,
snapshot/cursor validation and resource bounds remain required. Removing those
checks is not part of this policy change.

## New supported contract — decisions, not historical measurements

### One API and one prepared generation

Add `POST /v1/api` for high-level methods with a strict Pydantic request envelope
`{protocol: "ct.api.v1", id, method, method_version, params}`. Generate the
Worker validators from those models; `id` is null or a string of at most 128
characters. Reuse the success/error/availability envelope structure, with this
protocol literal and the requested method version;
do not send high-level methods through `ct_*` dispatch. Existing `/v1/core`
control-plane primitives remain the collector/authority transport, not an older
high-level API fallback. Local Core dispatch exposes the same method contracts
without requiring a network connection.

Advance each implemented method's registry version once from the survey:
`project.list`/`project.sessions` → 5; `session.overview` → 4;
`session.summary` → 3; `session.tree`, all six stats/usage methods plus
`session.tool_usage`, and `graph.overview` → 4;
`session.items`/`session.events` → 5; `living.sessions` → 3.
Here the six stats/usage methods are `session.stats`, `graph.stats`,
`session.usage`, `graph.usage`, `session.model_usage`, `session.request_usage`.
Only that version of each delivered method is accepted, locally and remotely.
Deferred methods stay unavailable on the direct endpoint; they do not gain a
second reader or remote Python fallback. No version negotiation is needed.

Use preparation version **3**, `ct.published_facts.v2`,
`ct.prepared-summary.v2`, and `ct.artifact-manifest.v2` for the revised shared
facts and manifest. Add one prepared JSON format, `ct.prepared-api.v1`, whose
objects declare method/version, source identity and projection version. These
names distinguish the new generation from P1's preparation v2 and the unmerged
graph candidate. The producer and reader accept only these new prepared schemas
after cutover. This is a rebuild, not an in-place transformation of old data.

Every immutable response carries an `identity` outside its method result:
`workspace_id`, `source_snapshot_sequence`, `source_manifest_sha256`,
`view_snapshot_sequence`, `view_manifest_sha256`. A prepared view manifest names
the accepted source fact manifest and its exact method objects. For a graph,
`source_manifest_sha256` is the existing `compute_fact_set_digest` over schema,
graph ID and sorted `[kind, fact_id, row_hash]` entries; it is not the upload
object's byte hash or the workspace-wide artifact manifest hash. Inventory
source identity hashes its complete prepared project/session cards. The remote
view manifest additionally binds the authority-assigned project ID; Worker
overlays that ID on overview project metadata, not the producer's local ID.
Source and view sequence fields record their publication fences (currently the
same atomic accepted-publication sequence), not independent revision counters. Optional
request `view_manifest_sha256` pins a read; otherwise authority resolves latest
complete once. Local preparation uses a content-addressed manifest too; its
sequence fields are null, and the local workspace ID/hash still bind cursors.
No wall-clock timestamp is used as a snapshot identity.

`unsupported_version` (HTTP 400, availability `unsupported`) covers a wrong
protocol/method version. `unsupported_prepared_version` (409) means rebuild
with the current producer; `prepared_view_unavailable` (409) means no complete
new view exists. Never reinterpret an old object, reconstruct remotely in Python,
or serve some new objects alongside stale old ones. A not-yet-delivered method
returns `method_unavailable` (501). Invalid requests return 400; authorization
continues to use 401/403 before any object or snapshot disclosure.

### Overview schema and field placement

`graph.overview` takes `root_session_id`, `limit` (default 20, 1–200), optional
`cursor` (at most 4,096 characters), and optional pinned view hash. It selects
one orchestration run using current `orchestration_runs` membership (follow
non-`forked_from` edges); a fork is a separate run, with its origin retained.
Reject a non-root selector
instead of guessing. `session.overview` takes `session_id` with the same page
parameters and selects only that session's visible turns. Both use the same
turn projection, topology record and page rules; no `before_turn_id` input.
Graph overview pages carry all run sessions and all run edges, including
zero-turn sessions. Session overview carries its session plus parent/fork
identity, not unrelated sibling turn arrays. External fork endpoints remain
explicit references, not fabricated in-run nodes.

The result has `graph_id`, `root_session_id`, `lineage_root_session_id`,
`entrypoint_id`, `project` (`project_id`, `display_name`), `orchestration`,
`fork_origin`, `totals`, `sessions`, `edges`, `turns`, `page`, `coverage`.
`entrypoint_id` records the selected session/run ID; run identity is distinct
from lineage identity. Keep the existing typed edge/origin records, including
source session/turn/item/event references, in canonical source edge order.
`fork_origin` is the incoming fork edge or null. Include outgoing fork edges
with external target IDs so fork navigation survives a page boundary.
`orchestration` retains kind, vendors, multi-agent modes/versions,
session/spawned-session counts and edge-type counts. Add `totals` containing
run-wide `source_turns`, `narrative_turns`, `filtered_turns` and lineage-wide
`forks` (number of canonical `forked_from` edges). Session overview uses its
session counts for turns and keeps the lineage fork total. Agent paths occur
on session records only. Remove the duplicate legacy `graph`/`summary`
containers and derivable per-session page counts/ranges.

| Record | Required fields and rules |
| --- | --- |
| Session | `session_id`, `session_rank`, `run_root_session_id`, `parent_session_id`, `parent_in_run`, `edge_type`, `relationship`, `status`, `latest_turn_status`, `started_at`, `ended_at`, `vendor`, `title`, `model`, `agent_name`, `cwd`, `agent_path`, `multi_agent_version`, `multi_agent_mode`, `source_turn_total`, `narrative_turn_total`, `filtered_turn_total`. Nullable source facts stay null; no invented status/title. `relationship` is the canonical incoming edge type, or `root`; `edge_type` preserves that nullable edge fact. |
| Turn | `global_ordinal`, `session_id`, `session_narrative_ordinal`, `source_turn_ordinal`, `turn_id`, `source_sequence`, `started_at`, `ended_at`, `timestamp_state`, `status`, `user_request`, `assistant_responses`, `activities`, `refs`, `content_coverage`. Ordinals are zero-based; source sequence retains its canonical value. Timestamp state is `complete`, `missing`, or `partial` according to the two timestamps, not a synthetic sort key. |
| User request | Null or `{content, source, event_id}`; content is the shared 280-character preview, source preserves request provenance, event ID is nullable. |
| Assistant responses | Up to 8 `{item_id, preview}` entries, each preview at most 280 characters. Keep the last 8 in source order so the outcome remains visible. |
| Activities | Up to 8 latest semantic tool/file activity records in source order: `{item_id, kind, tool_name, concept, target_kind, target, path, operation, status, outcome, exit_code}`. Use shared tool-summary/detail and output-evidence facts, not a second shell parser. Target includes bounded command arguments or file target (280 characters); `path` uses the shared bounded file fact (512); outcome/labels at most 512. Nullable unsupported facts remain null. Raw output/bodies are excluded. |
| References | `refs.item_ids`: first 100 item IDs in source order; `refs.user_request_event_id`: exact ID or null. References on retained assistant/activity entries are kept even when outside those first 100. A turn-scoped items page resolves the rest; references never require an overview page to contain all items. |
| Content coverage | For assistant responses, activities and item references, store `{total, returned, truncated}`; shared projection records whether text/target previews were shortened and propagates that flag here. Counts refer to eligible source records before this overview cap, not all raw events. Unknown source completeness is `partial`, not guessed complete. |

Use current narrative visibility (`effective_user_request`/`is_low_value_turn`)
before paging, but emit **one record per visible canonical turn**, with no
teammate-turn merging that loses individual turn identity. Preserve semantic
collaboration activities using the same shared facts. `filtered_turn_total`
counts invisible turns; `source_turn_total = narrative_turn_total +
filtered_turn_total`. `coverage` keeps the existing retention/measurement/
searchable/trimmed meanings; `trimmed` is true for omitted pages or bounded
content, never evidence that full raw output was retained. Keep reasoning
diagnostics and usage/cost on the dedicated detail/stats APIs, not overview.

Full `cwd` and `agent_path` are nullable strings with **no character clipping,
path normalization, basename substitution or field-specific length cap**.
Preserve the exact canonical source values in shared Chronicle session facts,
fact reconstruction, prepared topology and JSON responses. Current main's
`ChronicleSession` does not carry either field: changing the serving schema alone
would not restore them. Remove the generic 512-character validation for these
two fields only, while retaining enclosing fact/object/response byte bounds.
An absent source value is null; a present value that cannot fit is a size error,
never null. Semantic tool targets remain explicitly bounded previews; they do
not redefine these two full session-path fields.

### Page, size and link behavior

Assign session rank with current `ordered_sessions` (root/child UUID order,
then canonical order for remaining nodes); concatenate sessions by rank and
their visible turns by canonical source order to assign global ordinals.
This deliberately is **not globally time-sorted**. Session overview uses the
same algorithm for its one-session selection. Missing/equal/non-monotonic
timestamps cannot change traversal.

The first page is the newest suffix; subsequent pages end immediately before
the previous page's start. Return rows ascending within each page. Clients
prepend older pages without sorting by timestamp. `page` contains `direction:
"older"`, `requested_limit`, `start_ordinal`, `end_ordinal_exclusive`, `returned`,
`total`, `has_more`, `next_cursor`. `total` is the complete visible-turn count
for the selection. Empty selections have `[0,0)`, zero returned and no cursor.
Every nonempty successful page advances by at least one row; there are no
empty success pages with a continuation cursor.

Use these ceilings for the new design (not historical limits or a qualification claim):
64 KiB request, **448 KiB complete UTF-8 response envelope**, 320 KiB selected
turn array, 128 KiB topology object, at most four object reads / 768 KiB fetched
and one authority call / at most 20 returned authority rows per immutable read.
JSON punctuation, escaping, metadata and cursor bytes count. Publication retains
the existing 16 MiB fact-set, 512 KiB fact-row and fact cardinality bounds.
These budgets can yield smaller pages or explicit size failures; they do not
establish equivalent coverage or improved CPU compared with the historical design.

Choose ordinary verified JSON: one index (at most 64 KiB), one topology, and
source-order turn packs (at most 256 KiB each). An index stores ordinal ranges,
object hashes/lengths and exact serialized row byte lengths. The Worker reads
at most two adjacent packs, then selects the largest suffix fitting limit,
turn-array and complete-response budgets. Reserve 8 KiB for bounded envelope,
identity, page and cursor fields before selection; check actual serialized
bytes before returning. Topology and row sizes include JSON escaping overhead.
Do not repeatedly fetch smaller packs to search for a fit. If packing, index,
topology or a single required row cannot fit, record that method as unavailable
with `remote_result_too_large` (413); other fitting methods remain usable.
At serving time, a row that cannot fit together with the complete topology
also returns 413. A smaller page is explicit through `returned`/`has_more`, not
silent data loss. Never shrink full paths or drop topology/edges to make it fit.
No length-framed fragment format or separate topology endpoint is needed.

Cursors are opaque base64url payloads plus HMAC, binding workspace, method and
version, a SHA-256 of normalized scope/filters, limit, view/source manifest identities,
index hash, exclusive end ordinal (or detail position) and expiry. Issue them
for 24 hours, but a discarded view can invalidate them earlier; this is not a
retention promise. Request/cursor scope conflicts and tampering return
`invalid_cursor` (400); expiry or unavailable pinned view returns
`view_expired` (409). Resolve the pinned view, never replace it with latest.
Local cursors use a persisted local signing key and the local manifest hash;
a rebuild/key loss can require restarting pagination. Cursor authorization is
not permission: authorize every page even with a valid signature.

New evidence references are structured `{workspace_id, view_manifest_sha256,
session_id, turn_id?, item_id?, event_id?}`. Owned clients encode these as
URL/hash parameters with a `reference_version=1` marker, percent-encode IDs,
and reject unsupported reference versions. Preserve this reference when
saving an investigation; do not save payloads or generate legacy redirects.
The detail request passes the same view hash and exact IDs. Event → owning
item uses the event's `item_id` in that view; an unowned event displays its
event facts without inventing an item. Unknown IDs are reported in
`unresolved_ids`; known IDs outside a supplied turn/filter are nonmatches, not
misattributed records. An expired view displays “View expired; reopen latest”;
only an explicit user action opens latest, with no claim of identical evidence.
Local links work against retained local prepared views without remote access.

`session.items`/`session.events` retain their current typed filters, ID batch
cap 100 and type-filter cap 20; use default limit 200, maximum 1,000. Filter
first, order by canonical source order (ID as deterministic tie-breaker), then
page forward. Add `total`, `returned`, `unresolved_ids` and `next_cursor` to
their current result shapes. Prepare filter/ID locators and source-order JSON
pages within the same four-read/byte/response budget. Reduce returned count
when necessary, resume at the first unreturned match, and explicitly reject a
single oversized record or unrepresentable index. Never scan the whole graph
on a request or silently ignore a requested filter. Detail is retained typed
evidence, not a promise to fetch raw logs or file bodies.

Use per-field posting lists in the bounded detail index (turn, ID, type, status,
tool, owning item as applicable), intersect/union them in memory, and fetch
only packs containing the next matches. Lists within one filter are OR;
different filters are AND. Two selected packs may be nonadjacent; stop the page
before a third is needed. Compute totals and unresolved IDs from the index,
including requested IDs not on the current page. Index overflow is an explicit
method size failure, not a new scanning fallback. Inventories use the same
bounded index/pack selection; exact unpaged foundation results need only a
verified locator and complete result object, not a turn-pack layout.

### Foundation, authority and rebuilding responsibilities

Prepare `project.list` and `project.sessions` as snapshot-bound, filtered,
forward-paged inventories (default 100, maximum 200), ordered by project ID
and root-session ID respectively. Return `items` as an array plus `total`,
`returned`, `next_cursor`. Preserve zero-item sessions and root/member IDs.
Keep the current project selectors, absolute `modified_since` and vendor filter.
An omitted selector means the authorized workspace inventory, not a hidden
default project. Producers publish project cards and root/member cards; the
authority materializes the workspace inventory directory on publication from
those cards, with no graph/metric computation. Its immutable manifest pins the
exact project source/view manifests; this directory is the source/view identity
for cross-project inventory reads. Never assemble an inventory by fetching N
graphs on a request. A detail link takes the selected card's project view hash,
not the inventory directory hash. Retain the same bounded index/page format.
Prepare summary, tree, stats and usage from the same canonical Python handlers
once per source view, including the accepted per-turn summary/usage variants.
These remain exact unpaged results: 413 if their complete result exceeds the
response budget. `session.tree` rejects `turn_id`; it returns the selected
session's lineage tree rather than silently accepting an ignored filter.
No new usage calculation lives in the Worker or client. Request/tool ledgers
get no pagination in this iteration.

The producer owns visibility, ranks, topology, totals, shared bounded semantic
facts, method/filter indexes, canonical JSON serialization and feasibility
checks. Upload immutable workspace-scoped, content-addressed objects, then
publish the complete manifest with the existing accepted-source-vector,
publisher ownership and monotonic publication fences. Only mark a new view
ready after all references are present; do not expose pending objects as latest.
Record method size failures in the manifest, not partial success data.

The authority owns current workspace permissions, source/view selection and
publication state; use indexed point resolution, not a scan of every manifest
or graph. Worker authentication and authority resolution happen on every
request, before using any cached bytes. Cache keys include workspace and
content hash, never cached permission. Before parsing, stream-bound each
object to its manifest length and method budget, verify exact length and
SHA-256, then validate schema, scope, ordinal ranges and internal references.
Missing/corrupt ready objects return 503 (`prepared_object_missing` /
`prepared_object_corrupt`), not a fallback view. Do not expose arbitrary object
keys to clients. Use `Cache-Control: no-store` on API responses as today.

`living.sessions` stays authority-owned mutable SQL: bounded change pages,
fixed `through` watermark/evaluation time per batch, stable scope binding,
and explicit restart on expired watermarks. Cap page output at 200 changes
and 448 KiB; query limit+1 candidates, with indexed scope/sequence selection.
Do not claim that an output row limit bounds all SQL work: inspect the actual
plan for changed queries. Reuse current lease/removal semantics; do not turn
mutable updates into immutable overview pages. Search and `living.events`
remain deferred; neither blocks the foundation work.

On cutover, rebuild disposable local preparation caches and publish new
prepared views from canonical source through the existing producer. If old
facts lack full paths or richer semantic context, reread canonical source;
an old lossy projection cannot reconstruct missing values. If that source is
unavailable, report rebuild unavailable rather than filling invented data.
Never delete canonical logs, source exports or user investigations to repair
a cache. No new cleanup service, dual reader or automatic migration is needed;
remote publication/cleanup still requires its own authorization.

Current-source anchors inspected for these decisions (main at P1):
`contracts/session.py`, `contracts/envelope.py`, `contracts/registry.py`;
`analysis/graph_views.py`, `analysis/session_graph_views.py`,
`analysis/orchestration_runs.py`, `ingestion/indexes.py`;
`control_plane/fact_projection.py`,
`control_plane/published_facts.py`, `control_plane/fact_repository.py`;
`cloudflare/control-plane/src/{index,artifacts,living}.ts`; and Loop's
`web/src/main.tsx`. Paths without a package prefix are relative to
`packages/core/src/coding_trajectory/`. Main's registry still has graph v3;
the unmerged candidate and its measurements below remain separate evidence.

## Measured evidence and its limits

The optimized remote run returned 664/664 measured HTTP 200 responses with
canonical parity. Fetch CPU p95/p99 in milliseconds: [E1]

| Shape | p95 | p99 | Existing gate |
| --- | ---: | ---: | --- |
| Representative sequential | 3 | 4 | PASS |
| Large-index sequential | 6 | 7 | PASS |
| Near-budget sequential | 7 | 8 | FAIL |
| Near-budget concurrency 8 | 12 | 15 | FAIL |

Targets remain p95 <=6 ms, p99 <=7.5 ms, and fewer than two samples above 9 ms
per class. No cap, eligibility rule or gate is relaxed by this freeze.
Overall Stage 3 is **FAIL**; exact peak memory is **UNQUALIFIED**. R2 reads and
bytes are manifest estimates, not exact platform-attributed measurements.
Two authority events each have two markers; their per-request CPU attribution
is ambiguous. Every fetch event has one marker, so the failing fetch evidence
does not depend on that ambiguity. Successful outcomes are not CPU proof. [E1]

The near-budget input is exactly 654,629 bytes across four reads: 1,512 index,
131,072 topology, and 261,022/261,023 turn packs. Six unselected turns account
for 195,600 bytes fetched, verified and parsed. Ten selected turn objects total
326,000 bytes. The returned session array is 130,249 bytes, dominated by one
**synthetic 129,599-character `cwd`**. The actual response is 458,523 bytes;
selected turn array plus session array accounts for 456,260. [E2]

This historically motivated a keep/trim comparison; that decision is now closed
in favor of full paths. Avoiding overfetch or repeated parsing remains useful.
The synthetic path tests adversarial capacity, not typical user demand. Use
focused size-bound checks for changed shapes, not a new path-shortening study.

Local actual-workerd lower-bound experiments suggested about 10–16% less sampled
payload work from finer selection and about 15–25% from pre-serialized output.
They embed response bytes and omit general dynamic assembly, authorization,
R2/DO latency, cursor HMAC and logging. They are neither a complete format
prototype nor Cloudflare CPU qualification; concurrency cause remains unknown.
Finer packs can violate the four-read bound or inflate publication/retention
cost. Fully verified length-framed fragments remain a proposal, not an approved
format migration. [E2]

## Evidence index and reproducibility

Policy amendment (separate from the original survey/CPU evidence):

- **P1:** [Retain bounded semantic tool details in local and published facts](https://github.com/liux4989/CodingTrajectory/commit/5a249915820f9f6917d6b32962611eebd45d6af1),
  especially `packages/core/src/coding_trajectory/control_plane/fact_projection.py`
  and `docs/remote-ct-control-plane-design.md`. Preparation v2 refreshes old lossy
  caches; updated readers retain v1 artifact compatibility. This implementation
  changes shared fact descriptions, not the gate-off direct v4 field contract.
  It does not establish new latency/CPU measurements or remote qualification.
  Its existing v1 compatibility is historical implementation evidence, not a
  requirement to carry compatibility into the next design.

Source citations below refer to the exact **unpushed optimized checkout**, not
this document's main-branch checkout. Retrieve with `git show <source>:<path>`
after verifying/importing the bundle. Candidate commits are not claimed to be
merged or publicly fetchable. Line ranges are pinned to that source.

- Optimized source: `e26612f87cd1a67bac3a57ea277abe975175b0e9`, tree
  `74b98b20e456ae92b822a013a37ddbcba6c3f7ec`; serving commit
  `658b921951b8674d14251b0e0b817af5a8f95810`.
- Local baseline ancestor: `d3333fe2cda1f60000b8590d8fe384daba9654b4`.
- Analyzer prerequisite: `0772e700f25c04c620ea7e69db5a3ec9d9e66be7`;
  bundle SHA-256 `959f7cfd8b80337afc56d5b6bcf74cb62856c8fd44cbf2ce623f0ec91c962f45`.
- Optimized bundle: `coding-trajectory-graph-v4-stage3-cpu-opt-e26612f.bundle`;
  SHA-256 `0428aae5f186fc1a6d6a1f578bbf2722b89dc3ea09deb733f78b7656be4c990d`.
- Fixture producer remains `a3d69407b412dd0371a728b2b182d3a195d2c999`,
  tree `fb87fa97787d17444d5ffc624f64715be209d44d`; manifest SHA-256
  `7d87ed126d0a6a24adffe096cdc432d5ba017e304d9bc70db3bcbf18c27ca535`.
  Fixture provenance is not deployed-runtime provenance.

Source locations (all at optimized source unless stated otherwise):

- **S1:** `packages/core/src/coding_trajectory/contracts/registry.py:106-200`.
- **S2:** `cloudflare/control-plane/src/index.ts:86-140`;
  `docs/remote-ct-control-plane-design.md:27-38`.
- **S3:** `packages/cli/src/coding_trajectory_cli/commands/project.py:49-80,99-144`.
- **S4:** `packages/cli/src/coding_trajectory_cli/commands/session.py:186-410,596-847,1266-1271`.
- **S5:** `packages/cli/src/coding_trajectory_cli/commands/graph.py:37-67,96-131`.
- **S6:** `packages/plugins/loop/web/src/main.tsx:87-90,308-405,438-698,711-960`.
- **S7:** `packages/plugins/loop/loop_plugin/monitor/runtime.py:103-165,258-275,323-407`.
- **S8:** `docs/cli.md:102-104,294-302`; `docs/prd.md:121-122,142-146`;
  `docs/loop-design.md:208`.
- **S9:** `packages/core/src/coding_trajectory/contracts/graph_overview_v4.py:1-109`;
  `packages/core/src/coding_trajectory/control_plane/graph_overview_v4.py:279-403,483-549,651-713`;
  `cloudflare/control-plane/src/graph-overview-v4.ts:108-123,157-178`;
  `validation/openapi/graph-overview-v4/stage2-qualification.md:3-7`.
- **S10:** [Read-only survey thread](https://ampcode.com/threads/T-01a0b820-f05b-76fe-832b-176badc01916).
  Consumer absences and representative field-byte measurements are survey
  findings, not exhaustive claims about external clients.

Execution/investigation evidence:

- **E1:** `graph-overview-v4-stage3-cpu-opt-full-2026-09-19.tar.gz`, SHA-256
  `ef15ca2521bffa8421401117f9bba8b21557c1510525b4bc1e191fc41837324a`;
  summary `graph-overview-v4-stage3-cpu-opt-cloudflare-2026-09-19.json`, SHA-256
  `6425d873a0ad677971cfe9da5c50afc1df50d317e0a40d46d3a88595bc1befa9`.
  Sanitized artifacts are retained under `.amp/in/artifacts/`, not committed
  as part of this document. [Execution thread](https://ampcode.com/threads/T-01a0aeaf-9c10-760a-81aa-6575743230e1).
- **E2:** `docs/graph-overview-v4-near-budget-investigation-2026-09-19.md` and
  `scripts/investigate-graph-overview-v4-near-budget.mjs` at investigation commit
  `9b448a5cd39b79521db76600c8e8da19bb8030b5`, tree
  `67cb61fbfec2623fa7a2c344fffbf22b2aa2655f`; bundle
  `coding-trajectory-graph-v4-near-budget-investigation-9b448a5.bundle`, SHA-256
  `a56803eb310220083e45f9aa998325c950e538e9af8ebe74fda3e6bc3a6f0de4`,
  prerequisite optimized source above.
  [Investigation thread](https://ampcode.com/threads/T-01a0b5b2-12bd-7197-9502-17586afacfc4).

For this freeze, the coordinator rechecked the optimized/investigation bundle,
execution archive and summary hashes; inspected the pinned contract, consumer,
integrity code and investigation evidence. The survey remains attributed rather
than represented as a newly rerun exhaustive consumer search. No benchmark or
remote workload was rerun to create this document.

## Implementation sequence and focused checks

1. **Shared contracts and preparation.** Implement the new Pydantic envelopes,
   method versions, shared full-path facts and bounded activity/coverage fields.
   Reuse canonical Python computations, route owned readers only through the new API, add immutable JSON
   indexes/packs and the current-generation rebuild path. Update schema
   snapshots intentionally from these decisions, not from unexplained output.
2. **Twelve foundation methods.** Add prepared publication/authority resolution
   and direct Worker selection, keeping local dispatch usable offline. Update
   CLI, Loop and Monitor consumers with each changed method. Use the same page
   primitives for inventories and session overview; reject oversized unpaged
   results explicitly. No production switch or remote publication is part of
   this document's authorization.
3. **Items/events and evidence links.** Add typed filter/ID indexes, pinned
   detail reads, reference-only client links and expired-view restart. Verify
   overview → older page → item → event → owning item before calling the
   end-to-end remote workflow complete. Until these routes exist, owned clients
   use supported local detail or show remote detail unavailable; never silently
   switch source or expose a link that claims unavailable remote detail works.
4. **Graph overview, then living sessions.** Reuse the prepared turn/topology
   and cursor implementation for the global run page. Add the bounded mutable
   SQL path and Monitor watermark updates afterward. Leave search/living.events,
   paged ledgers, fragment formats and telemetry outside this sequence.

Use existing integration/qualification scripts, not new unit tests or a new
report suite. Start from the normal locked workspace setup
(`uv sync --all-packages --frozen`, as in CI). Apply only the checks affected
by each implementation slice:

| Check | Command or existing workflow and decisive cases |
| --- | --- |
| Contract/build | `uv run python scripts/check-core-protocol.py`; `npm --prefix cloudflare/control-plane run check`. Explicitly update the protocol snapshot/generated schemas with the implementation, then require clean checks. |
| Local context and round-trip | `uv run python scripts/qualify-chronicle-usage-roundtrip.py` and `uv run python scripts/validate-local-first-source-selection.py` when shared preparation changes. Extend existing synthetic scenarios for command arguments, file targets, failed/successful outcomes, preview truncation, and exact long Unicode/backslash `cwd`/`agent_path` round-trips. Do not use executable-only expected labels. |
| Owned-client navigation | `uv run python scripts/check-loop.py`, updated for the new contracts, plus one CLI graph/session page. Use at least two sessions with unequal turn counts, forks, a zero-turn member, hidden turns and equal/out-of-order timestamps. Walk all pages: no gaps/duplicates, correct ordinals/totals, same topology on each graph page, exact pinned item/event ownership and unresolved IDs. Check offline use with unusable remote configuration. |
| Direct-read boundaries | `uv run python scripts/qualify-prepared-api.py` uses a disposable Miniflare Worker and the real producer, without Wrangler credentials or remote bindings. It replaces the old reconstruction-reader qualification for this endpoint. Check actual UTF-8 bounds, two-pack continuation, sparse detail IDs, full paths, missing/corrupt objects, absent views, workspace/role denial, cursor tampering/filter conflict/expiry and unsupported versions. The older artifact benchmark/qualification remains historical evidence, not a supported client fallback. |

Do not run the entire historical suite for every edit. Repository metric gates
apply if implementation changes metric-sensitive paths:
`scripts/check-metrics-quality-gate.sh` and the full baseline workflow directly
with `uv run python scripts/validate-metrics-baselines.py`; reconstruct any
intentional metric change from committed source before changing expectations.
For UI rendering changes inspect the affected client states, including pinned
detail and expired-view restart rather than only the default overview.

Measure performance when changing the serving path or making a performance
claim: a representative internal session and the known near-budget shape are
the initial comparison. Record actual selected/fetched/response bytes and local
CPU/time for local claims; remote CPU claims require separately authorized
remote measurements. No standalone format prototype, exact peak-memory
certification or full Stage 3 rerun is required for each iteration. Historical
Stage 3 **FAIL** and memory **UNQUALIFIED** remain unchanged; a new smoke result
cannot relabel them. There is **no unresolved owner choice** blocking this
sequence; capacity and timings remain implementation verification, not a
request to reopen full paths or single-version support.

Local reversible implementation and checks need no extra approval checkpoint.
This document does not itself authorize a push, deployment, remote workload or
destructive cleanup. Those shared-state actions still need specific approval;
prior remote-run allocations are consumed. Accepting early breaking changes
does not authorize deleting source data or shared resources.

### Local implementation and verification

The producer/collector now prepares and publishes the new methods; Worker
serves `/v1/api` from verified indexes/base/packs, and the owned Python remote
runtime/proxy no longer reconstructs remote fact graphs. Local APIs use the
same prepared method generation without networking. Loop and Monitor carry
view hashes through detail/evaluation links. CLI overview, inventory and detail
JSON preserve the new response fields; inventory continuation uses `--cursor`,
`--limit` and an unchanged absolute `--modified-since` (no moving 30-day default).
The shared command preview uses 280 characters, not the earlier 60-character
compaction that could erase arguments before preparation.

Executed locally: `scripts/qualify-prepared-api.py` passed the real producer →
disposable Miniflare publication → overview → older pages → pinned item/event
workflow, plus the owned Python remote runtime and authenticated HTTP proxy.
It checked source-order completeness, full Unicode cwd, argument/outcome
retention, size rejection, missing/corrupt objects, workspace/role isolation,
unsupported versions, cursor tampering/filter conflict/expiry, and signed
living-session continuation with fixed evaluation time. The 776-item exact
tool ledger correctly returns 413; its one-turn result succeeds. No remote
bindings or credentials were used. This is targeted qualification, not an
exhaustive capacity or compatibility proof.

One valid 97-turn/776-command fixture measured **18.138 s wall / 18.134 s CPU**
for cold preparation on this orb. Its first local prepared page measured
**11.9 ms wall / 11.8 ms CPU**, **4 reads**, **482,835 fetched bytes**, and
**329,078 result bytes for 66 turns**. This is Python local timing, not Worker
CPU or peak-memory qualification; no before/after speedup is claimed.

The contract freeze, Worker TypeScript/schema check, Loop generated types/build,
Loop/Monitor integration, collector replay/preparation, Chronicle usage
round-trip and local-first selection checks passed. Both the full committed
metric baseline workflow and the required quality gate passed all four cases;
numeric usage/cost/runtime expectations are unchanged. A CLI two-session graph
walk returned ordinal 1 then 0 with identical full topology on both pages.
Chromium inspection covered desktop pinned detail, the narrow layout without
horizontal overflow, and an expired-view error with a working restart link.

Historical Stage 3 **CPU FAIL** and peak memory **UNQUALIFIED** above remain
unchanged. Production performance, deployment and actual publication have not
been tested or authorized. There is no unresolved owner product choice.

### Fresh-session pre-deployment validation

Coordinator validation created new disposable source journals rather than
reading existing user sessions: a three-turn Amp journal exercised file reads,
command arguments and failed/successful outcomes; the prepared-API qualification
now starts from a new UUID-bearing Pi journal before generating its capacity
shape. Real Amp ingestion exposed command-context loss for unfamiliar transport
tool names. Classification now respects canonical command-execution items.
Known detail IDs excluded by a turn/filter now remain nonmatches, not unresolved
IDs, in both offline and Worker readers.

Profiling the fresh 97-turn/776-command preparation identified repeated shell
parsing. Reusing the existing token cache for the lifetime of one preparation
measured **3.796 s wall / 3.883 s CPU** on the updated fixture; its first prepared
page measured **10.4 ms**, **4 reads**, **480,307 fetched bytes**, and **327,356
result bytes for 66 turns**. These are local observations, not a controlled
speedup comparison or remote CPU qualification. Cold preparation still takes
seconds at this size. The new three-turn Amp session produced its CLI overview
in **0.926 s cold / 0.481 s repeat** before the cache-scope improvement.

Browser validation followed pinned item → failed event → owning item, retaining
the same view identity and exit code 7. Desktop and narrow screenshots were
inspected; the narrow DOM had no horizontal overflow and retained command
arguments. An expired pin displayed `view_expired`; restarting removed the old
pin and loaded the current chronology. No existing user logs, Cloudflare calls,
deployment, or shared-resource changes were involved.

### Current Worker local CPU and memory benchmark

The benchmark runs the actual producer and Worker in disposable Miniflare,
without Wrangler configuration or remote bindings. Reproduce each shape with:

```sh
uv run python scripts/qualify-prepared-api.py --shape representative --benchmark-output /tmp/representative.json
uv run python scripts/qualify-prepared-api.py --shape near-budget --benchmark-output /tmp/near-budget.json
```

Each invocation creates a new synthetic Pi journal and a fresh workerd process.
The representative shape has three turns/24 commands and a normal full path;
the near-budget shape has 97 turns/776 commands and a 120,006-character full
path. These are not the old Stage 3 fixtures. Two complete final-harness runs
per shape each measured one first read after publication, 100 sequential reads,
25 concurrency-8 batches, another 100 sequential reads, and a separate
100-request CPU profile. All 2,004 responses matched offline-reader turns,
session topology and page boundaries. Cursor presence was checked, not cursor
byte equality across local/remote identities. Earlier harness exploration
included an inspector URL-scheme failure and an initial complete run per shape;
the table below uses both final-harness runs without removing any samples.

| Measurement | Representative | Near-budget |
|---|---:|---:|
| Response envelope bytes | 16,611 | 446,151 |
| Expected reads / fetched bytes (offline reader) | 3 / 16,987 | 4 / 598,559 |
| Turns returned | 3 | 66 |
| First read after publication wall ms | 8.34–8.59 | 22.62–23.21 |
| Sequential process CPU ms/request, both sequential phases | 4.4–5.0 | 14.3–15.0 |
| Sequential wall p95 ms, both sequential phases | 7.06–8.77 | 36.47–41.18 |
| Concurrency-8 process CPU ms/request | 3.15–3.35 | 12.35–12.65 |
| Concurrency-8 wall p95 ms | 36.39–37.88 | 145.79–148.81 |
| Highest unprofiled sampled process RSS MiB | 173.5–174.8 | 390.3–393.1 |

Worker bundle SHA-256 was
`37985d93d31247a900d78b7854ac7ec69a5c23e87f176c6f6702c52e89bb2eab`.
Reports retain fixture/harness hashes, runtime versions, all latency samples,
process CPU totals, RSS samples and inspector heap snapshots; sibling
`.cpuprofile` files retain the raw profiles. The application-isolate near-budget
used-heap snapshots were 27–53 MiB before/after profiling, not peak values.
`HeapProfiler.collectGarbage` timed out in this runtime, so post-GC retained
heap and exact peak memory remain unqualified. Rising RSS alone is not proof
of a leak: it includes local storage/authority services and allocator retention.

The near-budget profiles consistently highlight `load`, `bounded` input-stream
assembly/copying, `stable` canonical serialization, and `digest`. Source inspection
confirms full-object hashing/parsing and repeated result serialization/encoding.
These are follow-up optimization candidates, not changes made by the benchmark.
Profiler sample weights include scheduling/wait effects and are not billed CPU.

Linux process CPU uses 10 ms ticks aggregated over batches and includes all
services in workerd, not per-request isolate CPU. Wall time includes loopback,
harness parsing and scheduling; concurrent response validation also loads the
driver. RSS is sampled every 10 ms and can miss peaks; lifetime high-water marks
include publication. First read is after publication/preflight, not guaranteed
cold storage or a cold isolate. No deployed CPU/memory gate can be inferred from
these measurements. Historical Stage 3 results remain unchanged. No production
code, deployment, remote workload, or shared resource was changed.
