# Remote API responsibilities and evidence freeze — 2026-09-19

Status: owner-agreed direction and read-only survey, frozen before implementation.
This document does not change a contract, enable a route, qualify Stage 3, or
authorize deployment, workload, cleanup, push, or a pull request.

The owner approved broad remote support, the delivery order below, and separating
overview from detail. The subsequent clarification makes **performance the main
criterion for host paths**, with **keep and trim both open**. The owner defined
**trim as shortening the path in the overview**, not omission or separate loading.
Local/offline Python and existing v3 behavior remain intact.

## Responsibilities agreed for the next design

| Boundary | Responsibility | Must not become |
| --- | --- | --- |
| Local/Core Python | Preserve canonical semantics, local/offline APIs, existing v3 consumers, and computation of prepared results. | A removed fallback or a silently reinterpreted API. |
| Publication/preparation | Compute reusable summaries, cards, topology, metrics, indexes, packs, and canonical serialization once per immutable view. Publish exact source/view identity. | Request-time repeated graph reconstruction, or permission to publish additional private/raw data. |
| Worker direct reads | Authenticate/authorize each request, validate scope/snapshot/cursor, resolve the authoritative prepared view, verify object bounds and integrity, select and return bounded results. | Cached authorization, skipped SHA verification, mixed stale/pending snapshots, or silent truncation. |
| Durable Object authority | Own workspace authorization-related resolution, publication/snapshot state, and bounded mutable SQL/cursor semantics. | A duplicate per-request metrics or summary engine. |
| Overview response | Supply useful self-contained navigation, bounded previews, exact identities/references, ordering, pagination, totals, and coverage. | A container for every available diagnostic, metric, item, or event. |
| Detail APIs and clients | Load item/event details and metrics as needed; preserve precise evidence links and snapshot identity. | Broken drill-down or transferring unbounded work to clients without accounting for it. |

Existing `POST /v1/core` accepts control-plane `ct_*` primitives and artifact
locators; Python reconstructs facts and runs canonical handlers. Planned direct
APIs serve prepared high-level results without requiring Python on that request
path. These are distinct boundaries, not evidence that all high-level APIs are
already remotely served. The registry has 18 high-level methods; the strict
graph overview v4 candidate is deliberately unregistered/gate-off. [S1, S2]

## API survey and agreed delivery order

Early support means a delivery priority, **not current availability or proof of
Free-plan qualification**. Exact results that exceed a supported unpaged bound
must return an explicit size error rather than partial success. The proposed
error name `remote_result_too_large` and numeric bounds require contract review.

| Method/version | Observed consumer or demand | Category and qualification prerequisite |
| --- | --- | --- |
| `project.list` v4 | CLI discovery; Loop project selector. [S3, S6] | Foundation: prepared inventory; pagination/versioning before larger-than-supported inventories. |
| `project.sessions` v4 | CLI list; Loop Explore; Monitor watch scope. [S3, S6, S7] | Foundation: prepared cards, stable root/member IDs, zero-item visibility; no N graph reads to make a list. |
| `session.overview` v3 | CLI chronology/tree; Loop first paint and older pages. [S4, S6] | Foundation: prepared visible turns; preserve order, previews, visibility, IDs and existing cursor semantics; prove bounded pack selection. |
| `session.summary` v2 | CLI brief; Loop objective, decisions, changes, verification and evidence links. [S4, S6] | Foundation: exact whole-session/turn variants; compact orientation, not a substitute for detail. |
| `session.tree` v3 | CLI conversation lineage/forks. [S4] | Foundation: prepared topology; preserve existing behavior. Accepted-but-ignored `turn_id` needs a separate future-version decision. |
| `session.stats` v3 | CLI context/runtime/message/token report. [S4] | Foundation: exact prepared result. |
| `graph.stats` v3 | CLI aggregate/per-session report. [S5] | Foundation: exact prepared result. |
| `session.usage` v3 | CLI accounting; Monitor per-turn evaluation. [S4, S7] | Foundation: exact whole-session/turn variants; operationally important. |
| `graph.usage` v3 | CLI token/cost aggregate. [S5] | Foundation: exact bounded result, including composition. |
| `session.model_usage` v3 | Documented advanced provider/model grouping; survey found no dedicated first-party production caller. [S8] | Foundation: straightforward preparation; retain despite unknown external demand. |
| `session.request_usage` v3 | CLI ledger; Monitor provider-request counts by turn. [S4, S7] | Foundation: exact fitting whole-session/turn ledger; explicit size error otherwise. A paged version is a later design. |
| `session.tool_usage` v3 | Documented advanced per-tool diagnostics; survey found no dedicated production caller. [S8] | Foundation: exact fitting prepared result; preserve attribution/pricing versions. |
| `session.items` v4 | CLI metadata; Loop paged items and exact evidence resolution. [S4, S6] | First extension: on-demand detail. Publish source-order chunks and turn/ID/type indexes; prove exact filter/cursor behavior and bounded reads. |
| `session.events` v4 | CLI metadata; Loop exact event and owning-item navigation. [S4, S6] | First extension: on-demand detail. Index accepted turn/event/item/type/status/tool filters; preserve ownership and unresolved-ID diagnostics. |
| `graph.overview` v3 / v4 candidate | CLI v3 hierarchy and edges; no production v4 consumer found. [S5, S9] | Next after shape/CPU qualification: v3 pages sessions independently; v4 uses a global suffix page. Never reinterpret v3. |
| `living.sessions` v2 | Monitor change polling, watermarks, scope selection. [S7] | Next after mutable qualification: highest mutable priority; bound SQL rows and define cursor retention/revocation. |
| `session.search` v2 | CLI ranked evidence discovery. [S4] | Deferred, not removed: bounded prepared index with ranking parity, or separately versioned constrained exact path/prefix semantics. |
| `living.events` v1 | PRD resource-change protocol; Loop design anticipates it; no production caller found. [S8] | Deferred, not removed: qualify leases, bounded SQL, retention and content-reference policy. |

The foundation contains **12 methods**, followed by **2 detail methods**, then
**2 qualification-dependent methods**, with **2 deferred methods**. Unknown
in-repository demand is not evidence of absent external demand. Telemetry and
support-window policy remain to be designed, not enabled by this decision.

## Overview field responsibilities

This is direction for a revised direct contract, not an edit to existing v4
schemas. All exact schema/version changes and client adaptation remain pending.

| Field group | Frozen direction and evidence |
| --- | --- |
| Graph/root/lineage/entrypoint/session/turn IDs | Keep exact identities for selection, authorization scope and links. [S5, S6, S9] |
| Rank, parent, relationships, complete edges, fork identity | Keep self-contained topology and canonical edge order; distinguish run from lineage/fork. CLI v3 uses these. [S5] |
| Global/session/source ordinals, source sequence, page/cursor | Keep deterministic traversal and snapshot pinning; timestamp order is not a substitute. [S9] |
| Status, timestamps, timestamp state | Keep compact chronology and completion context. [S4, S6] |
| Request/assistant previews | Keep bounded useful previews; full evidence belongs on demand. Smaller preview bounds are a product/contract change, not a transparent optimization. [S6, S9] |
| Item IDs and request-event reference | Keep precise links until a replacement demonstrably preserves direct-link semantics. Item/event support alone does not automatically justify deleting references. [S6, S9] |
| Vendor/title/model | Keep compact labels and interpretation metadata. Richer reasoning/agent diagnostics belong on demand where the revised contract defines them. [S4, S6, S9] |
| `cwd`, `agent_path` | **Open keep-versus-trim choice, driven primarily by performance.** Keep preserves exact existing values. Trim means a shortened path included in the overview, not omission or a separate fetch. Exact length, retained components and shortening indication remain to be defined explicitly in the revised contract. Compare complete processing cost; preserve v3/local behavior. |
| Orchestration, non-page totals, visibility and coverage | Keep compact unique information; a single page cannot reconstruct these totals. [S5, S9] |
| Legacy summary and per-session page counts | Candidate for reuse/derivation in a revised contract, not an approved field deletion. Preserve unique fork and non-page totals. [S9] |
| Usage/cost | Do not add to overview; load the dedicated foundation APIs. |
| Topology repeated across pages | Keep self-contained pages initially. A separate topology resource is deferred pending realistic corpus evidence and snapshot/retention/client-recovery design. |

Representative synthetic response accounting was 5,462 bytes: identity/project
about 297, graph/summary/fork 467, sessions 2,200, edges 855, turns 1,327 and
page/coverage 315. These approximate groups and the 3,737-byte topology object
are survey shape measurements, **not production demand measurements**. [S10]

“Detail” means detail permitted by the existing data policy. This freeze does
not authorize uploading raw provider logs, arbitrary payloads, secrets, or
host paths previously excluded from remote publication. Performance is the
keep/trim decision criterion within applicable security and data constraints.

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

This exposes separate choices: keep/trim output fields and avoid overfetch or
repeated parsing. The synthetic path tests adversarial capacity; it does not
establish typical user demand. Removing it alone cannot qualify the largest
remaining supported outputs. New shapes require fresh boundary qualification.

Local actual-workerd lower-bound experiments suggested about 10–16% less sampled
payload work from finer selection and about 15–25% from pre-serialized output.
They embed response bytes and omit general dynamic assembly, authorization,
R2/DO latency, cursor HMAC and logging. They are neither a complete format
prototype nor Cloudflare CPU qualification; concurrency cause remains unknown.
Finer packs can violate the four-read bound or inflate publication/retention
cost. Fully verified length-framed fragments remain a proposal, not an approved
format migration. [E2]

## Evidence index and reproducibility

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

## Pending decisions and authorization boundary

1. Define the path-shortening rule and compare full versus shortened paths for
   complete request cost, output utility and representative plus adversarial
   shapes. Include preparation, storage and client work; do not select a winner
   in advance. Omission and separate loading are not the trim alternative.
2. Specify the revised direct contract/version and client compatibility. Exact
   preview/reference bounds, derived fields and shortened-path representation are not yet
   frozen schemas. Preserve existing local/v3 behavior and precise links.
3. Design an offline format prototype only as a separate implementation task;
   preserve integrity, cursor/snapshot identity, retained-reader compatibility,
   existing coverage and resource bounds. No format was selected here.
4. Define later paged-ledger/search contracts, mutable retention guarantees and
   unknown-demand telemetry before implementing those deferred capabilities.
5. Requalify any changed contract/format locally, then obtain specific approval
   for a bounded remote validation. Prior remote-run allocations are consumed.
   Existing staging resources remain deployed; cleanup also needs approval.

The freeze records responsibilities, priorities, evidence and open choices.
It does not turn a direction into an implementation or a failed gate into a pass.
