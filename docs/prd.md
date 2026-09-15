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
- Provider-reported cost is native evidence. Catalog-priced estimates, waste
  scores, rankings, recommendations, and other product judgments belong to an
  enrichment layer.

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

- The full host-local canonical graph is the richest source. The private,
  bounded `ct.chronicle_graph.v2` artifact is its portable historical projection
  for remote collection and replay.
- Chronicle is a public Core query boundary, not only a persistence format.
  Consumers may read its bounded canonical resources directly instead of going
  through a Core-owned display projection. Local and remote adapters implement
  the same contracts subject to declared evidence capability.
- Chronicle stores only canonical dependencies needed to reproduce supported
  remote queries: identities, topology, ordering, timestamps, lifecycle facts,
  measurements, bounded operational details, bounded previews, and provenance.
- The Chronicle artifact never embeds pre-rendered overview, summary, search, or
  metric responses. A transport may separately materialize a versioned display
  projection keyed by its canonical dependency hash, but that projection is a
  replaceable cache rather than canonical history. Display evolution must not
  require rewriting canonical history unless its actual dependencies change.
- Full transcript bodies, general event arrays, raw commands, tool inputs, and
  tool outputs remain under host-local evidence authority.

## Display query layer

- The Core display layer is a first-party internal consumer of Chronicle. It may
  expose bounded convenience methods, but it has no evidence or semantic
  authority unavailable to another Chronicle consumer.
- Consumers may use these methods when their reading semantics fit, or construct
  different displays directly from Chronicle. A display response is never a
  required canonical dependency.
- The same projection and metric rules apply to both sources. Source selection
  may change availability, coverage, and provenance, but must not silently change
  the meaning of a supported response.
- Capability is declared per method and source. Display logic may distinguish a
  source through that capability declaration; it must not scatter ad hoc
  local/remote checks throughout individual projections.
- A remote source explicitly rejects a query whose required evidence was not
  retained. It never substitutes a partial display that appears complete.

### Chronicle query ownership

- `project.list` owns bounded project discovery.
- `project.sessions` owns bounded session and graph discovery; it does not embed
  runtime or usage responses.
- `session.tree` owns conversation lineage and ordinary-fork structure.
- `session.items` and `session.events` own progressively expanded canonical
  evidence. Their public contracts expose stable hierarchy references, source
  ordering, timestamps, normalized type and observed status when supported.
- `living.sessions` owns the bounded changing-session inventory;
  `living.events` owns scoped canonical resource changes.

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
  request-level context observations.
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

### Excluded domains

- Duration forecasts, prediction binding, calibration, historical backcasts,
  estimator execution, and forecast-job state are not contextual session
  construction or native session metrics. The `estimate.*` domain is excluded
  from the Core API.
- Evaluation, scoring, ranking, pricing estimates, and recommendations are
  enrichment concerns and must consume Core contracts without becoming
  canonical fields.

# Ingestion Transcript Layer
- Each vendor adapter keeps vendor-specific parsing local, then emits a small transcript record stream: user message, assistant message, tool call, tool result, usage/runtime, and task completion.
- Adapters deserialize only fields that contribute to hierarchy, transcript, tool reconstruction, usage, status, or session linkage.
- Transcript records carry CT-owned normalized `data`; lossy or synthetic records are explicitly marked with transcript fidelity.
- A shared transcript projector owns the `Session -> Turn -> Item` reconstruction rules, including turn starts, tool-call/result pairing, and final-answer fallback behavior.
- Provider-specific payloads remain in transcript `data` and canonical `vendor_data` only when they are useful to CT; unused raw log properties are skipped instead of modeled.
- Core ingestion may apply a consumer-neutral retention policy after canonical identifiers are stabilized. `trajectory` retains replay evidence; `measurements` retains hierarchy, usage, runtime, reported cost, tool outcomes, and reconciliation inputs while releasing transcript bodies. Both policies preserve the same canonical facts for their shared metric contracts.
- Retention policy must not introduce consumer concepts such as waste scores, rankings, dashboard cards, default horizons, or UI labels. Those remain projection-layer decisions, and immutable vendor logs remain the evidence authority for lazy detail reconstruction.
- Consumer-owned derived stores are replaceable artifacts, not canonical compatibility boundaries. An incompatible SQLite format must be rebuilt from immutable logs; core vendor compatibility and versioned public API contracts remain separate responsibilities.

# Activity Projection Layer
- Activity projections have three separate layers: immutable vendor evidence, canonical item lifecycle reconstruction, and compact presentation. A presentation summary never replaces its underlying item evidence.
- The projector owns an active activity cell and flushes it at every hard boundary. Consecutive successful read/list/search operations become one `Explore` cell; consecutive successful command executions become one `RunCommand` cell; web activity, mutations, external tools, failures, assistant messages, and unresolved item ownership remain distinct. Every cell keeps its item IDs for drill-down.
- A compact command count requires canonical evidence that every command is agent-produced and succeeded. Codex native terminal items—including historical `Extension(kind="web.search")` web actions and `CommandExecution` lifecycles—provide that evidence directly. Older Codex JSONL can add a derived-static command only for an unconditional literal `await tools.exec_command({...})`; its outcome stays `unknown` unless native or explicit single-action wrapper-result evidence proves success, so it never joins a `Ran N commands` cell merely because the outer `exec` wrapper completed.
- The same cell state machine applies after every vendor adapter emits canonical lifecycle facts. Vendor-specific parsing, static-fallback provenance, and raw wrapper preservation remain adapter-local.
- The Codex-reference mapping, historical fallback boundary, and cross-agent contract are recorded in [`docs/codex-activity-reconstruction.md`](codex-activity-reconstruction.md).

# Chronicle history

- The originating host constructs one strict `ct.chronicle_graph.v2` artifact.
- Host-local service APIs read local sources first and use the published
  Chronicles authority only when local discovery is unavailable or a targeted
  record is missing. Both sources run through the same handlers and chronicle
  artifact contract.
- Source observations contain checkpoint metadata only. Raw logs, transcript
  bodies, and general event arrays are never historical upload payloads.
- Content is disabled in chronicle artifacts. Explicit local evidence calls read
  the full local graph without requiring publication; remote content requests
  are denied.
- Remote history stores validated artifacts directly; there is no remote
  canonical reconstruction worker or compact-session compatibility path.
- The [chronicle history contract](chronicle-history.md) defines bounds and
  bounded operational and narrative coverage. The [control plane](remote-ct-control-plane-design.md)
  defines inventory and living authority separately from historical artifacts.

There is no currently available evaluation API; earlier proposals are retired.
