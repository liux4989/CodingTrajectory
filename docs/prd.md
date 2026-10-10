# Product requirements

CodingTrajectory reconstructs vendor logs into an agent-agnostic hierarchy for
two purposes: progressively exposing the context of a coding session and
reporting native session metrics. Consumers progress from project and session
inventory through bounded contextual views to exact local evidence, using stable
IDs at every step.

The canonical hierarchy is `SessionGraph → Session → Turn → Item`, with events
providing underlying evidence. Ordinary conversation forks and spawned agent
runs retain their distinct scopes.

# Core responsibilities

Core has exactly two product responsibilities.

## Contextual session construction

- Reconstruct agent-harness mechanisms into the canonical hierarchy and expose
  that hierarchy progressively: inventory, topology, bounded summary, bounded
  chronological overview, scoped search, items, and events.
- High-level harness concepts are allowed when they are deterministic
  normalizations of source evidence, remain scoped to the hierarchy, and retain
  stable references back to their supporting items or events.
- Different display methods may intentionally project the same session for
  different reading tasks. For example, `session.overview` is chronological and
  navigational, while `session.summary` is a compact objective/outcome brief.
  Neither display becomes part of the canonical persistence contract.

## Native session metrics

- Report measurements derived from the reconstructed session: observed time,
  runtime, status, counts, token and context usage, and provider/model/request/tool
  measurements.
- Every metric has one owning method. Summary statistics, token accounting,
  model grouping, provider-request detail, and tool measurements must not be
  repeated across several responses merely for display convenience.
- Metric queries are session-scoped first and graph-scoped second. A graph
  aggregate may expose totals, but it must also preserve explicit per-session
  sections and must not present child-agent turns, starting context, or provider
  context windows as if they belonged to the root session.
- Provider-reported cost is native evidence. By explicit product decision,
  catalog-priced estimates remain supported through Core's existing live pricing
  path, unchanged by this refactor and distinct from reported cost. Waste scores,
  rankings, recommendations, and other product judgments belong to enrichment.

## Native and constrained

Both responsibilities must remain native and constrained:

- **Native** means reproducible from immutable vendor evidence through declared,
  versioned normalization or metric rules. Core does not invoke an external
  estimator or maintain a separate predictive domain.
- **Constrained** means every detail query has an explicit project, graph,
  session, turn, item, or event scope. Collection responses have stable ordering,
  bounded page sizes, and cursors or an equivalent continuation contract.
- Derived values declare their source, method, confidence, and coverage. Missing
  evidence remains unavailable or partial rather than becoming a guessed zero.

# Canonical hierarchy boundary

- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources.
- Canonical means agent-agnostic facts and stable references reconstructed from logs, not raw vendor JSONL and not UI-specific interpretation.
- `SessionGraph` is the orchestration aggregate over canonical sessions. Its identity is the root session id, and it exposes observed membership, orchestration capabilities, edges, and summary metadata.
- Consumer-neutral operation types may be canonical when they are derived from
  structured harness evidence through declared normalization rules. Sections,
  importance, workflow roles, rankings, and UI labels remain projection or
  enrichment concerns.

# Two-layer API

Core separates portable canonical data from display projections.
Loop consumes this complete Core boundary directly; its independent display
and enrichment ownership is defined in [`loop-design.md`](loop-design.md).

## Chronicle data layer

- Chronicle is the local canonical query layer, not a publication format or
  remote storage authority. Remote delivery is deferred and unavailable.
- Vendor logs feed adapter-owned streamed relationship metadata for inventory,
  with fresh topology discovery per request. Detail queries reconstruct only
  selected runs and their canonical dependencies through existing canonical
  ingestion, then apply publication-independent retention and redaction in memory.
- Consumers may read bounded canonical resources directly instead of going
  through a Core-owned display projection. Stable hierarchy references retain
  their meaning across contextual and metric queries.
- Historical and inventory queries have no cross-request derived persistence.
  Core no longer uses `~/.coding-trajectory/local.sqlite`; topology and retained
  graphs are reused only within one request or explicit batch. Immutable logs
  remain the evidence authority. Repeated detail reads of large runs repeat
  ingestion rather than benefiting from a persistent graph cache.
- No upfront preparation, packs, manifests, saved snapshots, signatures, or
  byte-budget pages are required. Display and metric responses are computed on
  request and never become required canonical dependencies.
- Full transcript bodies, raw vendor payloads, tool inputs, and tool outputs
  remain under host-local evidence authority. Standard canonical queries expose
  retained evidence with explicit provenance and coverage.

## Display query layer

- The Core display layer is a first-party internal consumer of Chronicle. It may
  expose bounded convenience methods, but it has no evidence or semantic
  authority unavailable to another Chronicle consumer.
- Consumers may use these methods when their reading semantics fit, or construct
  different displays directly from Chronicle. A display response is never a
  required canonical dependency.
- The same declared projection and metric rules apply to local queries. Coverage
  and provenance must not silently change the meaning of a supported response.
- Capability is declared per method and source. One declaration marks all 18
  methods local-only; individual projections do not scatter ad hoc remote checks.
  `auto` selects local sources without remote fallback.
- Local envelopes expose `result`, rather than the parked remote envelope's
  `data`. Metadata is `local` / `live` / `retained`, without snapshot identity.
  This local-only revision increments all 18 method contract versions by one.
- Collection pages use opaque unsigned count-keyset cursors bound to query and
  method version. Inventory uses identity ordering; content uses canonical source
  order with ID tie-breakers. Determinism is per call, not a cross-call snapshot.
  Published-view references and stale-view errors are removed.
- Stateless scoped ingestion preserves native metrics and public vNext cursor
  contracts; it does not introduce a new cursor or snapshot model.

### Chronicle query ownership

- `project.list` owns bounded project discovery.
- `project.sessions` owns bounded session and graph discovery; it does not embed
  runtime or usage responses.
- `session.tree` owns conversation lineage and ordinary-fork structure.
- `session.items` and `session.events` own progressively expanded canonical
  evidence. Their public contracts expose stable hierarchy references, source
  ordering, timestamps, normalized type and observed status when supported.
- `living.sessions` owns bounded header-level session inventory with live state
  and digests, without transcript ingestion. Global and project scopes use a
  rolling horizon (72 hours by default); explicit run scopes have no horizon.
- `living.events` owns a scoped current-resource snapshot with digests;
  consumers derive changes by comparing complete passes. Digests cover retained
  details, including in-place tool completion, even when requesting compact views.
- Neither living method stores payloads, change history or source checkpoints.
  Pages are live keysets, not frozen snapshots; absence from a complete pass
  means the resource is no longer in that scope. Consumers own last-seen state.
- Loop's `investigations.sqlite3` and `monitor.sqlite3` are product/user data,
  not Core derived caches, and remain intact.

### Core reference display ownership

- `graph.overview` owns a bounded orchestration overview assembled from
  Chronicle topology and member metadata.
- `session.summary` owns the compact objective, outcome, change, verification,
  unresolved-work, and next-action brief.
- `session.overview` owns chronological turns and activity. Summary does not
  duplicate its recent-activity view, and graph overview does not duplicate its
  narrative.
- `session.search` owns bounded retrieval over retained session evidence.

### Metric query ownership

- `session.stats` owns compact session time, status, count, context, compaction,
  and effort-change statistics; `graph.stats` owns the corresponding explicit
  graph aggregate and per-session sections.
- `session.usage` owns session and turn token accounting; `graph.usage` owns the
  corresponding explicit graph aggregate and per-session sections.
- `session.model_usage` owns provider/model grouping and throughput.
- `session.request_usage` owns the provider-request usage ledger and native
  request-level context observations. Its tool-result consumption links are
  timestamp-window associations between usage observations. Their explicit
  attribution reports request-input membership as unknown; they are not causal
  evidence that the provider request received the linked result.
- `session.tool_usage` owns tool counts, status, duration, and input/output size
  measurements.
- Construction displays do not embed these metric responses. Consumers compose
  contextual and metric methods explicitly at the display or enrichment layer.

### Required Core contract changes

- Strengthen `session.items` with typed canonical item records containing item,
  session, turn and event references; source sequence; timestamps; normalized
  kind and operation; observed lifecycle status; provenance; and coverage.
- Strengthen `session.events` with typed canonical event records containing
  event and session identity, timestamp, normalized type, related hierarchy
  references, provenance, coverage, and a stable source-order key when source
  evidence supports one.
- Keep both collections bounded with stable ordering and continuation. Do not
  introduce a second timeline resource: consumers interleave canonical turns,
  items, events and graph edges for their own display needs.
- Preserve orchestration targets on canonical graph edges through source session,
  turn, item and event references. Consumers must not infer subagent linkage from
  labels or command text.
- Metric responses remain separate and joinable through canonical session, turn,
  item, request and tool identities. Chronicle construction responses do not add
  embedded metric payloads or display-only metric references.
- Preserve method schemas and the protocol freeze gate. Retired published-facts
  schemas are removed from the snapshot, not replaced by a new storage contract.

### Excluded domains

- Duration forecasts, prediction binding, calibration, historical backcasts,
  estimator execution, and forecast-job state are not contextual session
  construction or native session metrics. The `estimate.*` domain is excluded
  from the Core API.
- Evaluation, scoring, ranking, and recommendations are enrichment concerns and
  must consume Core contracts without becoming canonical fields. The explicit
  live-pricing exception above remains supported; it is not forecast execution.

# Ingestion Transcript Layer
- Each vendor adapter keeps vendor-specific parsing local, then emits a small transcript record stream: user message, assistant message, tool call, tool result, usage/runtime, and task completion.
- Adapters deserialize only fields that contribute to hierarchy, transcript, tool reconstruction, usage, status, or session linkage.
- Transcript records carry CT-owned normalized `data`; lossy or synthetic records are explicitly marked with transcript fidelity.
- A shared transcript projector owns the `Session -> Turn -> Item` reconstruction rules, including turn starts, tool-call/result pairing, and final-answer fallback behavior.
- Provider-specific payloads remain in transcript `data` and canonical `vendor_data` only when they are useful to CT; unused raw log properties are skipped instead of modeled.
- Core ingestion may apply a consumer-neutral retention policy after canonical identifiers are stabilized. `trajectory` retains replay evidence; `measurements` retains hierarchy, usage, runtime, reported cost, tool outcomes, and reconciliation inputs while releasing transcript bodies. Both policies preserve the same canonical facts for their shared metric contracts.
- Retention policy must not introduce consumer concepts such as waste scores, rankings, dashboard cards, default horizons, or UI labels. Those remain projection-layer decisions, and immutable vendor logs remain the evidence authority for lazy detail reconstruction.
- Request-scoped retained graphs are in-memory reconstructions, not a persistent SQLite format or canonical compatibility boundary. Core vendor compatibility and versioned public API contracts remain separate responsibilities; living-store migration and Loop product/user data have their own ownership.

# Activity Projection Layer
- Activity projections have three separate layers: immutable vendor evidence, canonical item lifecycle reconstruction, and compact presentation. A presentation summary never replaces its underlying item evidence.
- The projector owns an active activity cell and flushes it at every hard boundary. Consecutive successful read/list/search operations become one `Explore` cell; consecutive successful command executions become one `RunCommand` cell; web activity, mutations, external tools, failures, assistant messages, and unresolved item ownership remain distinct. Every cell keeps its item IDs for drill-down.
- A compact command count requires canonical evidence that every command is agent-produced and succeeded. Codex native terminal items—including historical `Extension(kind="web.search")` web actions and `CommandExecution` lifecycles—provide that evidence directly. Older Codex JSONL can add a derived-static command only for an unconditional literal `await tools.exec_command({...})`; its outcome stays `unknown` unless native or explicit single-action wrapper-result evidence proves success, so it never joins a `Ran N commands` cell merely because the outer `exec` wrapper completed.
- The same cell state machine applies after every vendor adapter emits canonical lifecycle facts. Vendor-specific parsing, static-fallback provenance, and raw wrapper preservation remain adapter-local.

# Chronicle history

- Historical Chronicle queries reconstruct publication-independent retained
  canonical graphs in memory from local vendor evidence for each request or
  explicit batch. Fresh inventory metadata locates selected runs; it does not
  replace canonical ingestion or supply fabricated measurements.
- Direct historical handlers consume those graphs. Metrics are calculated on
  request, while the existing live pricing path remains unchanged.
- Retention preserves identities, topology, source order, timestamps, lifecycle
  facts, measurements, bounded operational details, previews, and provenance.
  Rendered summaries, overview pages, searches, and metric responses are not
  canonical history or persisted snapshot artifacts.
- Standard methods expose retained canonical evidence, not arbitrary transcript,
  tool, or vendor payload bodies. Logs remain private under host-local authority.
- Remote history, storage, publication, and delivery are deferred. No remote
  fallback, collector, connection management, or API server is supported.
- The legacy Amp capture plugin can still auto-publish externally. Set
  `CT_AMP_AUTO_PUBLISH=0`; that path is unsupported until deliberately redesigned.

There is no currently available evaluation API; earlier proposals are retired.
