# Browser Plugins Survey and Datahub Redesign

## Status

Accepted consolidation plan based on the repository state on 2026-08-25.

The first implementation slice is complete: Datahub now defaults to Sessions,
exposes Today and Compare as the other first-level routes, preserves old route
links with redirects, and no longer presents generated code-time data as
telemetry. The standalone metrics and code-time browser applications were
retired after the consolidated routes reached behavioral parity. The canonical
session workspace now also includes a chronological, source-linked evidence
timeline with evidence-type, agent, and outcome filters plus lazy detail
hydration from verified source ranges.

This document surveys the three browser-facing plugins, decides their product
boundaries, and defines an incremental migration. It does not propose a new
general-purpose plugin framework.

## Executive Summary

CodingTrajectory currently has three independent dashboard experiments:

- `datahub`: project/session exploration, model usage, context analysis, and
  cleanup commands;
- `metrics`: cohort comparisons for tokens, cost, and execution time;
- `code-time`: a compact recent-work report with a separate web page.

They are not three independent products. They are three views over the same
project, session-graph, usage, cost, and runtime facts. Keeping them as separate
plugins has produced three servers, three React applications, three copies of
frontend infrastructure, incompatible cache strategies, and overlapping
analytics.

The redesign is:

1. Make `datahub` the only browser application and web server.
2. Move the useful `metrics` and `code-time` views into Datahub feature
   routes backed by Datahub's existing read-model runtime.
3. Keep `ct plugin code-time` as a small text/JSON command if the command remains
   useful, but remove its browser mode after route parity.
4. Retire the `metrics` plugin after its validation and comparison behavior has
   equivalent Datahub owners.
5. Keep cleanup as CLI-only policy. Do not add mutation to the web application.
6. Do not build a shared frontend package merely to support applications that
   should no longer be separate.

The unit of a plugin should be a separately deployable capability, not one page
of the same local analytics product.

The primary product wedge is **cross-agent session observability**: a local
flight recorder for understanding what happened, why it happened, and what
evidence supports the answer. Recent-work orientation and token/cost/runtime
comparison support that investigation; they are not separate products.

## Current Inventory

The measured source inventory excludes lockfiles and generated assets.

| Plugin | Python lines | Frontend lines | Components | Routes | Default port |
| --- | ---: | ---: | ---: | ---: | ---: |
| `datahub` | 14,581 | 10,934 | 44 | 7 | 8765 |
| `metrics` | 921 | 1,225 | 15 | 1 route module / 3 paths | 8767 |
| `code-time` | 649 | 1,171 | 4 | 1 | 8766 |

All three use Python's `ThreadingHTTPServer`, serve a Vite/React build, expose a
local JSON API, and independently configure TanStack Query and Router. The
Datahub and metrics applications also duplicate app shells, metric cards,
chart wrappers, formatting utilities, and shadcn components.

### Capability Matrix

| Capability | `datahub` | `metrics` | `code-time` |
| --- | --- | --- | --- |
| Recent project/session overview | Yes | Cohort inventory | Yes |
| Token and cost summaries | Yes | Yes | Yes |
| Execution and wait time | Yes | Yes | Yes |
| Model comparison | Yes | Yes | No |
| Session drill-down | Yes | Summary rows | Project/session table |
| Context-window analysis | Yes | No | No |
| Graph/tree exploration | Yes | No | No |
| Incremental persistent read model | Yes | No, 30-second memory cache | No, 30-second memory cache |
| Revision/change delivery | Yes | No | No |
| Destructive cleanup | CLI only | No | No |
| Validation command | No | Yes | No |

### Current Data Flows

```diagram
┌───────────────────── datahub ─────────────────────┐
│ React app ─▶ Datahub HTTP API ───▶ SQLite models │
│                                      │            │
│                                      ▼            │
│                              core internals/files │
└───────────────────────────────────────────────────┘

┌───────────────────── metrics ─────────────────────┐
│ React app ─▶ metrics HTTP API ─▶ memory cache    │
│                                      │            │
│                                      ▼            │
│                          in-process service client│
└───────────────────────────────────────────────────┘

┌──────────────────── code-time ────────────────────┐
│ React app ─▶ code-time HTTP API ─▶ memory cache  │
│                                      │            │
│                                      ▼            │
│                          in-process service client│
└───────────────────────────────────────────────────┘
```

## Findings

### 1. The plugin split follows experiments, not product boundaries

`metrics` was intentionally created without refactoring Datahub for its first
release. That was a reasonable experiment boundary, but its token, cost,
execution, model, cohort, and session concepts now overlap Datahub's model
usage route. `code-time` is a compact dashboard home view rather than a
separately deployable capability.

Users must currently choose between three applications without a stable rule
for which one owns a question.

### 2. Data semantics and freshness differ for the same facts

Datahub retains revisioned SQLite read models and polls for changes. Metrics
and code-time call the service runtime and cache complete responses in memory
for 30 seconds. The same session can therefore have different freshness,
coverage, grouping, and loading behavior depending on the selected plugin.

The separate applications also repeat cohort aggregation instead of reusing
Datahub's retained canonical graph facts.

### 3. `code-time` can visualize invented data

The code-time API declares `hourly_density` and `project_trend` as optional but
does not populate them. Its frontend falls back to `generateSampleHourlyDensity`
and `generateSampleProjectTrend`, both of which use `Math.random()`. As a
result, the two primary charts change across renders and can be mistaken for
observed activity.

The production Datahub must never substitute demo data for missing evidence.
An unavailable projection needs an explicit unavailable or empty state.

### 4. The documented plugin boundary does not match first-party practice

`docs/plugin.md` says plugins are separate executables that do not import
`coding_trajectory`, but all three browser plugins declare
`coding-trajectory-core` dependencies and import its runtime or internal
modules. Datahub depends especially deeply on ingestion, discovery, query,
analysis, and service internals.

This redesign does not hide that mismatch behind another adapter. A later
plugin-contract decision should choose and document one of two honest models:

- first-party integrated applications may use a versioned Python SDK while
  external plugins use executable service contracts; or
- every plugin, including Datahub, consumes only process-isolated public
  contracts.

The Datahub consolidation works with either decision. Rewriting the mature
incremental runtime solely to claim process isolation is not part of this
migration.

### 5. Manifest compatibility is too coarse for optional features

Datahub declares every required service method at plugin startup. One missing
method prevents all commands, including unrelated cleanup or web views, from
running. As features consolidate, this all-or-nothing list becomes less useful.

The generic plugin system should eventually support command- or capability-level
requirements. Until then, Datahub should require only contracts needed to
start and report feature-specific unavailable states for optional views.

### 6. Documentation describes historical states

The metrics design still says "Proposed Phase 1" although the plugin exists.
Architecture documentation advertises a Datahub `benchmark` command that is
not registered by the manifest or CLI. These are symptoms of experiments being
documented independently instead of one product owning its current state.

## Similar-Product Survey

This survey covers three adjacent product categories. They solve different
problems, so individual features should not be copied without preserving that
distinction.

### LLM and agent observability

| Product | Relevant verified pattern | Source |
| --- | --- | --- |
| Langfuse | Project → session → trace → observation hierarchy; table-first discovery; filters encoded in URL state; session scores and annotations | [Sessions](https://langfuse.com/docs/observability/features/sessions), [filter-state source](https://github.com/langfuse/langfuse/blob/main/web/src/features/filters/hooks/useFilterState.ts) |
| Arize Phoenix | Project → session → trace → span hierarchy; conversation and trace inspection; session token/cost/latency aggregation; lightweight local startup | [Sessions](https://arize.com/docs/phoenix/tracing/llm-traces/sessions), [repository](https://github.com/Arize-ai/phoenix) |
| Helicone | Request-oriented analytics extended into hierarchical sessions; cost, token, latency, error, and TTFT views; saved and URL-serialized filters | [Sessions](https://docs.helicone.ai/features/sessions), [repository](https://github.com/Helicone/helicone) |
| Braintrust | Production traces, scores, feedback, datasets, and experiments share one model; filtered observations can become evaluation cases | [Observability](https://www.braintrust.dev/docs/observe) |
| LangSmith | Project → thread → trace → run hierarchy; Messages, Turns, and Details views; explicit warnings when missing child metadata makes thread totals incomplete | [Threads](https://docs.langchain.com/langsmith/threads), [filtering](https://github.com/langchain-ai/docs/blob/main/src/langsmith/filter-traces-in-application.mdx) |

The category is converging on:

- session/thread → trace/turn → event/span as the durable hierarchy;
- a searchable, filterable table as the index rather than a chart landing page;
- synchronized conversation/timeline and tree/waterfall detail modes;
- tokens, cost, duration, errors, and evaluation evidence at each aggregation
  level;
- URL-persistent filters and saved views;
- explicit telemetry completeness and freshness;
- OpenTelemetry-compatible import/export as an interoperability boundary;
- polling as an acceptable live transport when freshness and completion state
  are clear.

CodingTrajectory should adopt the hierarchy and investigation patterns, but not
the surrounding prompt-management, gateway, deployment, or evaluation-authoring
suite. Its narrower advantage is source-preserving coding-session evidence.

### Developer activity and engineering intelligence

| Product | Relevant verified pattern | Source |
| --- | --- | --- |
| WakaTime | Personal daily activity, project/file/language breakdown, and drill-down; "code time" is inferred from editor heartbeats and a configurable timeout | [FAQ](https://wakatime.com/faq), [CLI usage](https://github.com/wakatime/wakatime-cli/blob/develop/USAGE.md) |
| ActivityWatch | Local storage, user-controlled collection, timeline exploration, raw-data access, and extensible watchers | [Repository](https://github.com/ActivityWatch/activitywatch) |
| Wakapi | Self-hosted personal activity summaries, broad date presets, project search/detail, and WakaTime-compatible clients | [Repository](https://github.com/muety/wakapi) |
| Swarmia | Team flow metrics with previous-period and benchmark comparisons; public guidance treats benchmarks as directional and rejects individual ranking | [Benchmarks](https://help.swarmia.com/guides/benchmarks-and-comparisons), [metrics](https://help.swarmia.com/features/metrics) |
| DX | Explicit organization, group, and personal dashboards; personal view starts with actionable work while long-range metrics live in broader views | [Dashboard overview](https://docs.getdx.com/dashboard/overview/) |

This category reinforces several boundaries:

- Today should help a person resume and review work, not produce a score.
- Time must be qualified as observed, provider-reported, or inferred; it must
  not imply total work or productivity.
- Every metric needs its definition, exclusions, sample size, evidence source,
  and comparison basis.
- Personal data should remain local and private by default.
- If team views ever exist, they need separate permissions, aggregate defaults,
  and no individual leaderboard.

### Coding-agent products

| Product | Relevant verified pattern | Source |
| --- | --- | --- |
| OpenHands | Conversation control center, event/diff rendering, child-agent support, and conversation-level token/cost metrics; prior event-only reconstruction omitted some costs | [Repository](https://github.com/OpenHands/OpenHands), [metrics guide](https://docs.openhands.dev/sdk/guides/metrics), [metrics issue](https://github.com/OpenHands/OpenHands/issues/7105) |
| Claude Code | Stable resumable sessions, isolated subagent contexts, compaction lifecycle, task panel, context visualization, and layered skills/hooks/MCP/plugins | [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks), [subagents](https://docs.anthropic.com/en/docs/claude-code/sub-agents) |
| OpenAI Codex | Searchable, resumable, forkable threads; transcript preview; explicit context-window usage; inspectable child-agent threads and worktrees | [Repository](https://github.com/openai/codex), [subagents](https://developers.openai.com/codex/subagents.md) |
| Devin | Retrospective session insights with issue/value timelines, task classification, session metadata, and parent/origin relationships | [Session Insights](https://docs.devin.ai/product-guides/session-insights) |
| Cline | Searchable task history with workspace filters, favorites, cost/token/model sorting, and collapsible tool results | [History source](https://github.com/cline/cline/blob/main/apps/vscode/webview-ui/src/components/history/HistoryView.tsx) |

Runtime coding products already own prompting, approvals, steering, terminals,
diff application, and worktrees. CodingTrajectory should integrate their logs
and evidence rather than recreate their control planes. Its session detail
should coordinate:

- a readable conversation and chronological event timeline;
- parent/child agent topology and concurrency;
- context growth, compaction, and source composition;
- artifacts such as edits, commands, checks, commits, and links;
- hierarchical token, cost, model, and runtime accounting;
- derived diagnostics that always link back to source events.

### Product conclusion

The adjacent markets support three plausible wedges:

| Wedge | Decision |
| --- | --- |
| Personal Today/activity tracker | Supporting private triage view; too generic to be the primary differentiation |
| Token/cost metrics dashboard | Supporting Compare lens; useful but commodity and unable to answer what happened or why |
| Cross-agent session observability | Primary product: combines CodingTrajectory's session graphs, context evidence, artifacts, and multi-vendor ingestion |

Avoid opaque efficiency/productivity scores, agent-specific dashboards, flat
lists that expose every child run as a peer, graph-only navigation, expanded raw
tool payloads by default, and totals reconstructed solely by summing visible
events.

## Product Boundary

### Datahub owns

- one browser shell and navigation model;
- recent-work overview;
- cohort token, cost, execution, and model analytics;
- project, session, branch, graph, and conversation-tree exploration;
- context-window and token-efficiency analysis;
- the persistent read model, refresh lifecycle, and browser API;
- read-only evidence and coverage presentation.

### Core owns

- vendor ingestion and canonical session graphs;
- versioned query and measurement contracts;
- normalized token, cost, runtime, and provenance facts;
- evidence semantics that every consumer must interpret identically.

### CLI-only policy owns

- project/session cleanup candidate policy;
- confirmation, deletion/trash behavior, and audit manifests;
- metrics reconciliation used as a development or release quality gate.

Cleanup can remain under `ct plugin datahub` during consolidation to avoid an
unrelated command migration. It must remain absent from the web API.

## Target Information Architecture

```text
Datahub
├── Sessions                      default cross-agent history
│   └── Session workspace
│       ├── Timeline / conversation
│       ├── Agent and conversation topology
│       ├── Context and evidence detail
│       └── Artifacts and accounting
├── Today                         private recent-work triage
└── Compare                       cohort analytics
    ├── Usage
    ├── Cost
    └── Execution
```

Recommended stable routes:

```text
/sessions
/sessions/$sessionId
/today
/compare?view=overview|cost|tokens|time|efficiency
```

`/` should redirect to `/sessions`. The session workspace should eventually use
URL state for selected branch, agent, event, and view; the current `/graph` and
`/tree` routes should redirect to that workspace only after view parity. The
existing `/model-usage` route redirects to `/compare` while preserving validated
search state. Filters and chart modes belong in validated URL search state so links
restore the same cohort.

## Target Architecture

```diagram
┌───────────────────────── datahub plugin ─────────────────────────┐
│                                                                  │
│  ┌──────────────────── React application ─────────────────────┐  │
│  │ Sessions │ Today │ Compare │ Session workspace            │  │
│  └─────────────────────────────┬───────────────────────────────┘  │
│                                │ one versioned browser API        │
│  ┌─────────────────────────────▼───────────────────────────────┐  │
│  │ route handlers → feature queries → retained canonical facts│  │
│  └─────────────────────────────┬───────────────────────────────┘  │
│                                │                                  │
│  ┌─────────────────────────────▼───────────────────────────────┐  │
│  │ incremental runtime + SQLite revision/read-model store      │  │
│  └─────────────────────────────┬───────────────────────────────┘  │
└────────────────────────────────┼──────────────────────────────────┘
                                 │ explicit core boundary
                  ┌──────────────▼──────────────┐
                  │ canonical core facts/models │
                  └─────────────────────────────┘
```

### Backend rules

1. Discovery and graph measurement happen once per source revision.
2. Today and analytics queries derive from retained canonical facts; they do
   not rediscover sessions through a second service client.
3. Query modules may aggregate and paginate but may not reinterpret canonical
   token, cost, or runtime semantics.
4. Every analytics response includes cohort definition, generated revision,
   evidence coverage, warnings, and pagination metadata.
5. Missing values remain missing. They are never converted to zero or replaced
   by demonstration data.
6. Detail hydration remains lazy so aggregate routes do not retain transcript
   bodies unnecessarily.

### Browser API shape

Prefer a small resource-oriented API over one endpoint per chart:

```text
GET  /api/v1/overview?since_days=...
GET  /api/v1/analytics?metric=usage&chart=...&filters...
GET  /api/v1/sessions?filters...&cursor=...
GET  /api/v1/sessions/$sessionId/context
GET  /api/v1/sessions/$sessionId/graph
GET  /api/v1/sessions/$sessionId/tree
GET  /api/v1/changes?after_revision=...
POST /api/v1/refresh
```

Do not redesign every existing endpoint before moving features. Add the
versioned routes as facades over current runtime methods, migrate the frontend,
then remove superseded endpoints once no route consumes them.

### Source layout

Keep the implementation in one plugin and organize by feature, not by a new
package hierarchy:

```text
packages/plugins/datahub/
  datahub.py
  datahub_web.py
  runtime/
    store.py
    refresh.py
    delivery.py
  features/
    overview.py
    analytics.py
    sessions.py
    context.py
  cleanup/
  web/src/
    app/
    features/
      overview/
      analytics/
      sessions/
      context/
    components/ui/
    lib/
```

This is a target ownership map, not a request to move files before behavior is
covered. Avoid mechanical file moves during feature migration.

## Migration Plan

### Phase 0: Stop misleading experimental behavior

- [Done] Remove random sample-data fallbacks from the code-time web page.
- [Done] Show explicit unavailable states until real hourly and daily projections
  exist.
- [Done] Mark the metrics design as implemented/experimental and remove the stale
  Datahub benchmark command from architecture documentation.

### Phase 1: Establish one shell and route contract

- [Done] Make `Sessions`, `Today`, and `Compare` the first-level navigation.
- [Done] Add validated URL state shared by analytics routes.
- [Done] Define response metadata shared by the new overview and analytics APIs:
  `schema_version`, `revision`, `cohort`, `coverage`, and `warnings`.
- [Done] Keep the existing applications available during comparison, then retire
  them after parity validation.

### Phase 2: Move metrics projections

- [Done] Port the metrics cohort and grouping behavior into a Datahub analytics
  query over retained canonical facts.
- [Done] Move token, cost, and execution category pages into Datahub.
- [Done] Preserve reported/estimated/unavailable cost evidence and mixed-model runtime
  attribution rules.
- [Done] Keep metrics reconciliation in the repository quality-gate script
  after deleting the plugin.

### Phase 3: Move the recent-work view

- [Done] Implement observed daily/hourly buckets in the Datahub read model.
- [Done] Add the compact Today route using those projections.
- [Done] Reconcile totals against the same canonical graph usage consumed by the
  existing `ct plugin code-time --output json` report.
- [Done] Remove `code-time web`; retain the text/JSON command as a thin public-contract
  consumer if it still has command-line value.

### Phase 4: Retire duplicate applications

- [Done] Remove the metrics manifest, server, frontend, and package after route and
  validation parity.
- [Done] Remove the code-time server and frontend after Today parity.
- [Done] Remove duplicated frontend dependencies and build instructions.
- [Done] Redirect documented user workflows to `ct plugin datahub web`.

### Phase 5: Clarify the plugin contract

- [Done] Decide whether first-party plugins use a supported in-process SDK or the same
  process-isolated contract as external plugins.
- [Done] Update `docs/plugin.md`, manifests, package dependencies, and compatibility
  checks to match that decision.
- [Done] Keep capability-level requirements deferred; the consolidated product
  does not need a general extension surface.

### Phase 6: Add the session evidence timeline

- [Done] Project requests, assistant responses, tools, compactions, failures,
  and child-agent links from retained canonical facts without synthesized data.
- [Done] Preserve source item/event references and hydrate their verified detail
  only when a user asks to inspect an entry.
- [Done] Add evidence-type, agent/branch, and outcome filters to the canonical
  session workspace while preserving legacy timeline links with redirects.

### Phase 7: Make investigations restorable

- [Done] Encode timeline evidence type, agent/branch, outcome, and selected
  evidence entry in validated URL search state.
- [Done] Restore selected entries beyond the first progressively rendered page
  and report when a retained revision no longer contains a linked entry.
- [Done] Present timeline revision, refresh lag, retained source-reference
  coverage, and source ingestion failures beside the evidence view.

### Phase 8: Add an artifact evidence lens

- [Done] Classify retained file changes, commands, checks, commits, and fetched
  links without inferring artifacts absent from canonical tool evidence.
- [Done] Preserve command, path, and URL summaries plus lazy source detail for
  each artifact entry.
- [Done] Add URL-restorable artifact filtering and artifact badges to the
  evidence timeline.

### Phase 9: Join turn accounting to evidence

- [Done] Join retained `session.usage` turns to timeline turns by canonical
  turn ID at the same read-model revision.
- [Done] Show processed tokens, reported or estimated cost evidence, elapsed
  runtime, model-active runtime, and preceding wait once per turn.
- [Done] Keep unavailable accounting values explicit and avoid attributing
  turn totals to individual tool or message entries.

### Phase 10: Visualize observed concurrency

- [Done] Add a per-agent turn waterfall using only retained start/end timing.
- [Done] Compute peak concurrency from observed intervals and explicitly report
  turns omitted because timing evidence is incomplete.
- [Done] Synchronize waterfall selection with URL-restorable agent filtering,
  evidence selection, and lazy source detail.

## Acceptance Criteria

- One command starts every Datahub browser view.
- Today, usage, cost, execution, sessions, graphs, trees, and context views use
  one shell and one refresh lifecycle.
- A given source revision yields identical graph totals across overview,
  analytics, and session detail.
- No chart displays generated, random, or placeholder values as observations.
- Missing telemetry and partial coverage are explicit.
- URL state restores analytics cohort and chart selection.
- Metrics reconciliation remains available as a quality gate.
- Cleanup remains CLI-only and requires its existing safety controls.
- The duplicate metrics and code-time web servers are removed only after parity.
- Datahub startup, a production frontend build, and representative route
  responses are verified for each migration phase.

## Non-goals

- A plugin marketplace or remote plugin loading protocol.
- Arbitrary third-party React routes mounted into the Datahub process.
- A shared frontend package for unrelated future plugins.
- Moving canonical metric semantics from core into Datahub.
- Rewriting the incremental store before consolidating product behavior.
- Adding web-based destructive cleanup.

## Recommended Boundary Decisions

1. Keep `ct plugin code-time` as a compact CLI after its web view is merged.
2. Allow Datahub, as a trusted and co-released first-party application, to use
   a narrow versioned in-process core SDK. Keep executable JSON/service
   contracts as the ordinary and third-party plugin boundary.
3. Put Datahub's current core imports behind one high-level SDK facade before
   attempting to reorganize the incremental runtime. The facade should expose
   source planning, canonical graph/fact materialization, provenance, and the
   project catalog—not raw ingestion internals.
4. Do not support arbitrary third-party UI route plugins. Third parties can ship
   standalone applications over executable contracts. Reconsider declarative
   read-only panels or sandboxed frames only after concrete demand exists.
5. Make capability checks feature-specific so one unavailable optional
   projection does not block Datahub startup.
