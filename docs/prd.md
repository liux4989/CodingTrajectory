# Product requirements

CodingTrajectory reconstructs vendor logs into an agent-agnostic hierarchy for
session analysis, token accounting, and observed runtime activity. Consumers
progress from bounded summaries to exact local evidence through stable IDs.

The canonical hierarchy is `SessionGraph → Session → Turn → Item`, with events
providing underlying evidence. Ordinary conversation forks and spawned agent
runs retain their distinct scopes.

# Core Layer Boundary
- `Event`, `Item`, `Turn`, and `Session` are canonical normalized resources.
- Canonical means agent-agnostic facts and stable references reconstructed from logs, not raw vendor JSONL and not UI-specific interpretation.
- `SessionGraph` is the orchestration aggregate over canonical sessions. Its identity is the root session id, and it exposes observed membership, orchestration capabilities, edges, and summary metadata.
- Replay/UI-oriented interpretations such as sections, operations, roles, and workflow-specific labels should live in a projection or enrichment layer, not the core hierarchy.
- Metric and context-window projections must be session-scoped first and graph-scoped second. A graph aggregate may expose totals, but it must not present child-agent turns, starting context, or provider context windows as if they belonged to the root session. Multi-session responses must expose explicit per-session sections alongside any graph totals.

# Ingestion Transcript Layer
- Each vendor adapter keeps vendor-specific parsing local, then emits a small transcript record stream: user message, assistant message, tool call, tool result, usage/runtime, and task completion.
- Adapters deserialize only fields that contribute to hierarchy, transcript, tool reconstruction, usage, status, or session linkage.
- Transcript records carry CT-owned normalized `data`; lossy or synthetic records are explicitly marked with transcript fidelity.
- A shared transcript projector owns the `Session -> Turn -> Item` reconstruction rules, including turn starts, tool-call/result pairing, and final-answer fallback behavior.
- Provider-specific payloads remain in transcript `data` and canonical `vendor_data` only when they are useful to CT; unused raw log properties are skipped instead of modeled.
- Core ingestion may apply a consumer-neutral retention policy after canonical identifiers are stabilized. `trajectory` retains replay evidence; `measurements` retains hierarchy, usage, runtime, pricing, tool outcomes, and reconciliation inputs while releasing transcript bodies. Both policies preserve the same canonical facts for their shared metric contracts.
- Retention policy must not introduce consumer concepts such as waste scores, rankings, dashboard cards, default horizons, or UI labels. Those remain projection-layer decisions, and immutable vendor logs remain the evidence authority for lazy detail reconstruction.
- Consumer-owned derived stores are replaceable artifacts, not canonical compatibility boundaries. An incompatible SQLite format must be rebuilt from immutable logs; core vendor compatibility and versioned public API contracts remain separate responsibilities.

# Activity Projection Layer
- Activity projections have three separate layers: immutable vendor evidence, canonical item lifecycle reconstruction, and compact presentation. A presentation summary never replaces its underlying item evidence.
- The projector owns an active activity cell and flushes it at every hard boundary. Consecutive successful read/list/search operations become one `Explore` cell; consecutive successful command executions become one `RunCommand` cell; web activity, mutations, external tools, failures, assistant messages, and unresolved item ownership remain distinct. Every cell keeps its item IDs for drill-down.
- A compact command count requires canonical evidence that every command is agent-produced and succeeded. Codex native terminal items—including historical `Extension(kind="web.search")` web actions and `CommandExecution` lifecycles—provide that evidence directly. Older Codex JSONL can add a derived-static command only for an unconditional literal `await tools.exec_command({...})`; its outcome stays `unknown` unless native or explicit single-action wrapper-result evidence proves success, so it never joins a `Ran N commands` cell merely because the outer `exec` wrapper completed.
- The same cell state machine applies after every vendor adapter emits canonical lifecycle facts. Vendor-specific parsing, static-fallback provenance, and raw wrapper preservation remain adapter-local.
- The Codex-reference mapping, historical fallback boundary, and cross-agent contract are recorded in [`docs/codex-activity-reconstruction.md`](codex-activity-reconstruction.md).

# Shareable history

- The originating host constructs one strict `ct.shareable_graph.v1` artifact.
- Local shareable calls and remote calls reuse that artifact and the same handlers.
- Source observations contain checkpoint metadata only. Raw logs, transcript
  bodies, and general event arrays are never historical upload payloads.
- Detailed search, events, and contentful items remain local evidence APIs.
- Remote history stores validated artifacts directly; there is no remote
  canonical reconstruction worker or compact-session compatibility path.
- The [shareable history contract](shareable-history.md) defines bounds and
  reduced semantic coverage. The [control plane](remote-ct-control-plane-design.md)
  defines inventory, living, and estimation authority separately.

Earlier evaluation proposals remain in the [design archive](archive/README.md);
they do not establish a currently available evaluation API.
