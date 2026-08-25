# Metrics Frontend Plugin Design (Retired)

> Historical design record. The experimental standalone metrics application
> was retired after its token, cost, execution, distribution, cohort metadata,
> and session comparison behavior moved into Datahub's `/compare` route.
> Canonical reconciliation remains in `scripts/validate-metrics-baselines.py`.

## Status

Implemented experimental plugin. Its validated cohort and reconciliation behavior is being retained while browser analytics migrate into Datahub's Compare route; see [`dashboard-plugins-redesign.md`](dashboard-plugins-redesign.md).

## Purpose

Create a separate CodingTrajectory web plugin that dogfoods the project's canonical daily session metrics and presents model, provider, and project comparisons through three first-level categories: Token Usage, Cost, and Execution Time.

The information architecture is inspired by the [Artificial Analysis Coding Agent Benchmarks](https://artificialanalysis.ai/agents/coding-agents?coding-agents-performance-chart=benchmark-score-by-eval&coding-agents-token-usage-chart=token-distribution), especially its stable cohort, first-level metric categories, switchable chart modes, explicit measurement explanations, and drill-down comparisons. CodingTrajectory will use the pattern without copying Artificial Analysis data, branding, benchmark scores, or visual assets.

## Phase 1 Scope

Phase 1 intentionally excludes benchmark tasks, task evaluation, Performance Index, intelligence scoring, and Score by Benchmark.

The plugin answers three questions about real daily coding sessions:

1. How many tokens did each model, provider, project, session graph, and turn process?
2. What reported or estimated API cost is supported by the available evidence?
3. How much active execution time, waiting, and interaction complexity did each session graph require?

## Goals

- Provide a clean analytics product separate from the existing mixed-purpose dashboard.
- Make Token Usage, Cost, and Execution Time first-level navigation categories.
- Apply one explicit cohort and filter state to every visible metric on a page.
- Compare models without hiding provider, agent vendor, mixed-model sessions, missing telemetry, or pricing confidence.
- Preserve core graph, main-session, subagent, and turn boundaries through drill-down.
- Use existing versioned `ct` service contracts as the source of truth.
- Keep cohort aggregation and frontend read models inside the plugin rather than moving presentation semantics into core ingestion.
- Make chart selection and filters shareable through URL state.

## Non-goals

- No benchmark or task-quality score in Phase 1.
- No claim that higher token use, cost, or runtime means better or worse model intelligence.
- No direct import of `coding_trajectory` or `coding_trajectory_cli` from the plugin.
- No replacement or in-place refactor of `packages/plugins/datahub` during the first release.
- No repricing of tokens in TypeScript or the plugin server.
- No attribution of an entire mixed-model session's runtime to its dominant model.
- No copying of Artificial Analysis source code, proprietary datasets, or branded design system.

## Product Vocabulary

The plugin uses the following stable terms:

| Term | Meaning |
| --- | --- |
| Cohort | The session graphs selected by the current date, project, vendor, provider, model, and completeness filters |
| Session graph | The root coding trajectory including the main session and related subagents or sidechains |
| Session | One canonical agent session inside a graph |
| Turn | One user-to-agent interaction unit inside a session |
| Model row | A provider/model usage group observed across a `graph.usage` result |
| Mixed models | A graph or turn containing more than one provider/model group |
| Processed tokens | The canonical normalized processed-token total defined by core accounting |
| Cost evidence | A core-emitted USD value labeled `reported` or `estimated` |
| Active execution | Sum of measurable turn execution durations |
| Wait time | Measurable time between turns, kept separate from active execution |

## Plugin Boundary

The proposed plugin namespace is `metrics`:

```text
ct plugin metrics web
```

Proposed package layout:

```text
packages/plugins/metrics/
  plugin.toml
  pyproject.toml
  metrics.py
  metrics_web.py
  metrics_service.py
  web/
    components.json
    package.json
    index.html
    src/
      api.ts
      main.tsx
      routes/
      components/
      components/ui/
      hooks/
      lib/
      styles.css
```

The plugin follows `docs/plugin.md`:

- it is discovered from `plugin.toml`;
- it runs as a separate executable;
- it declares minimum versions for the `ct` methods it consumes;
- it calls machine-readable `ct api` surfaces instead of importing core packages;
- it owns its local HTTP server, cache, read models, and frontend assets.

Initial manifest requirements:

```toml
[requires]
"project.sessions" = 2
"graph.usage" = 2
```

## Existing Core Fit

The current core measurement layer already provides the required Phase 1 evidence:

| Requirement | Core source |
| --- | --- |
| Graph inventory and project filters | `project.sessions` |
| Graph, session, and turn token usage | `graph.usage` |
| Main and subagent separation | `graph.usage.sessions[]` |
| Provider/model token attribution | `graph.usage.models[]` |
| Turn and graph cost evidence | `graph.usage` |
| Active execution, waiting, and turn counts | `graph.usage` |
| Public contract discovery | `ct api schema <method>` |

The only proposed core metric addition is a canonical cache-hit-rate helper derived from cached and uncached prompt tokens. The provider-normalization rule belongs in core. The frontend plugin consumes the resulting facts and must not reinterpret provider input conventions.

## Data Flow

```text
React route and URL filters
  -> plugin HTTP API
  -> cached cohort read model
  -> ct api call project.sessions
  -> ct api batch graph.usage
  -> core-normalized public JSON
```

The plugin server batches all independent session queries through one `ct api batch` invocation per cohort slice. React never shells out to `ct` and never receives raw provider JSONL.

The server returns pre-aggregated chart series plus a bounded drill-down table. Large cohort aggregation does not run in the browser.

## Cohort Contract

Every page response includes the exact cohort definition used for its metrics:

```text
date range
project set
agent vendor set
provider/model set
session-graph status
mixed-model policy
session graph count
turn count
usage coverage
pricing coverage
runtime coverage
generated timestamp
```

Changing a chart mode must not silently change the cohort. Token, cost, and execution views may exclude rows that lack the specific required telemetry, but they must report that metric's denominator and coverage explicitly.

Missing values are excluded from the corresponding average or distribution rather than converted to zero.

## Global Filters

The application header exposes:

- date range;
- project;
- agent vendor;
- provider/model;
- session-graph status;
- single-model, mixed-model, or all graphs.

Filters live in TanStack Router search state so a copied URL restores the same analysis. The default cohort is the last seven days across all discovered projects.

The filter API returns available values and counts from the current discovery scope. A selected filter remains visible even when the resulting cohort is empty.

## Information Architecture

```text
Metrics
├── Token Usage
│   ├── Usage
│   ├── Distribution
│   ├── Cache Hit Rate
│   └── Input vs Output
├── Cost
│   ├── Cost per Session
│   ├── Distribution
│   └── Total Cost
└── Execution Time
    ├── Active Time
    ├── Distribution
    ├── Active vs Wait
    └── Turns
```

The top-level category appears in the route path. The selected chart mode appears in URL search state.

Proposed URLs:

```text
/tokens?chart=distribution&sinceDays=30&model=...
/cost?chart=total&project=...
/execution?chart=turns&vendor=...
```

## Shared Page Composition

Every category page uses the same reading order:

1. Route title and concise statement of what the category measures.
2. Global cohort filters and coverage summary.
3. Three or four category-specific highlights.
4. Chart-mode `ToggleGroup`.
5. Primary comparison chart.
6. Plain-language metric explanation and caveats.
7. Bounded model/provider comparison table.
8. Session-graph drill-down table.

The chart and table share grouping, sorting, and cohort semantics. A chart selection filters or highlights the corresponding rows rather than opening a disconnected view.

## Token Usage

### Highlights

- processed tokens;
- median processed tokens per graph;
- cache hit rate with coverage;
- output-to-input ratio.

### Usage

Show average processed tokens per session graph grouped by model or agent vendor. The stack uses canonical uncached prompt, cached prompt, cache write, completion, and reasoning buckets where the source exposes them.

### Distribution

Show the distribution of session-graph processed tokens for the selected grouping. The view exposes median, p75, p90, minimum, maximum, and sample count. A histogram or box-style distribution must not hide small sample sizes.

### Cache Hit Rate

Use the core-defined formula:

```text
cached prompt / (cached prompt + uncached prompt)
```

Cache-write tokens are separate. Rows without sufficient provider telemetry are excluded and shown through coverage. Cache hit rate is an observed routing/session result, not an intrinsic model-quality score.

### Input vs Output

Compare prompt-side processing with completion and reasoning output. The chart keeps visible completion separate from reasoning when reasoning telemetry exists.

## Cost

### Highlights

- total supported USD cost;
- median cost per graph;
- reported-cost coverage;
- estimated-cost coverage.

### Cost per Session

Show average and median USD cost per session graph grouped by model, provider, or project. The label must state whether a group contains reported cost, estimated cost, or both.

### Distribution

Show per-graph cost distribution with sample count and pricing coverage. Unpriced graphs remain visible in the accompanying table as `Cost unavailable` but do not enter numeric distribution buckets.

### Total Cost

Show cumulative supported cost over time and grouped totals. A total is accompanied by priced graphs over eligible graphs. Reported and estimated values remain visually and textually distinguishable.

### Pricing Rule

The plugin accepts the core `CostEvidenceFlat` value and confidence. It does not download pricing data, select model aliases, or multiply token totals by a blended price.

## Execution Time

### Highlights

- total active execution time;
- median active execution time per graph;
- median wait time per graph;
- median turns per graph.

### Active Time

Show active turn execution per session graph. It must not be labeled end-to-end task time because the current core metric sums observed turn durations.

### Distribution

Show per-graph active-time distribution with median, p75, p90, and sample count.

### Active vs Wait

Compare active execution and measurable wait time without summing them into an ambiguous runtime label. The explanation notes that missing or interrupted intervals may prevent the two values from reproducing the full first-to-last timestamp span.

### Turns

Show turns per graph, turns per canonical session, tool calls, failed tool calls, and subagent-session count as workflow-complexity indicators. Turns are not presented as time or quality.

## Model Attribution

Tokens and cost can be grouped by provider/model because core exposes model-specific usage observations.

Execution time follows stricter rules:

- a single-model graph may be grouped under that model;
- a graph containing multiple models is grouped as `Mixed models` for graph-level execution comparisons;
- turn-level execution may use the turn's explicit single model when only one model group is present;
- the plugin never assigns the full graph runtime to `dominant_model`.

The table exposes agent vendor and provider/model as separate columns. A model running through different agent harnesses is not silently merged when the vendor dimension is selected.

## Plugin Read Models

The Python plugin server owns Pydantic response models for:

- filter options;
- cohort metadata and coverage;
- highlights;
- grouped chart series;
- distribution statistics;
- model/provider rows;
- session-graph rows;
- typed warning and partial-data states.

Proposed HTTP endpoints:

```text
GET /api/options
GET /api/tokens
GET /api/cost
GET /api/execution
GET /api/sessions
POST /api/refresh
```

Each category endpoint accepts the shared cohort filters and chart mode. It returns only the data needed by that route. The session endpoint provides paginated drill-down rows.

The server cache key includes every cohort filter, category, chart mode, and relevant service-contract version. Manual refresh clears plugin read-model caches but does not mutate core discovery data.

## Frontend Foundation

The new web package follows the current repository frontend foundation:

- Vite;
- React 19;
- TypeScript;
- Tailwind CSS v4;
- TanStack Router;
- TanStack Query;
- TanStack Table;
- shadcn/ui with Radix primitives and the existing `new-york` configuration;
- Recharts through the shadcn `Chart` wrapper;
- Lucide icons.

The plugin initializes its own `components.json` with the same aliases and semantic-token approach. It does not import source files from the datahub plugin or create a shared web package during Phase 1.

Use existing shadcn compositions first:

- `Sidebar` or compact route navigation;
- `Card` for highlights and chart explanations;
- `ToggleGroup` for chart modes;
- `Select` for filters;
- `Chart` for visualizations;
- `Table` for comparison and drill-down;
- `Badge` for evidence and coverage state;
- `Skeleton` for loading;
- `Alert` for partial-data warnings;
- `Empty` when added to the package for empty cohorts.

Styling uses semantic tokens. Chart series may have a dedicated semantic data palette, but provider branding colors are not used as status colors.

## Performance and Loading

- Lazy-load the three category routes.
- Fetch the active category first and defer unrelated category data until navigation or intentional prefetch.
- Aggregate session cohorts on the plugin server rather than the React main thread.
- Paginate session rows and bound model comparison tables.
- Keep prior query data visible while compatible filters refresh.
- Use skeletons that preserve chart and table geometry.
- Consider `content-visibility: auto` only for large below-the-fold sections and pair it with an intrinsic size; do not apply it to highlights or the primary chart.
- Avoid periodic polling. Data refresh is user-triggered or tied to an explicit cache policy.

## Accessibility

- Every chart has an adjacent text summary or accessible table representation.
- Color is not the only means of distinguishing token buckets or evidence confidence.
- Chart-mode controls use `ToggleGroup` with an accessible label.
- Tooltips supplement visible labels rather than containing the only definition.
- Tables retain real headers, sortable state announcements, and keyboard-accessible links.
- Compact number formatting always has an exact value available through a tooltip or table cell label.
- Empty, partial, loading, and error states are distinct.

## Dogfooding Rules

This plugin is a consumer of the same public contracts available to external plugins. It must not use private in-process helpers to make the UI look correct.

Before release, the plugin must demonstrate:

- at least two agent vendors in one cohort when local data permits;
- single-model and mixed-model graph handling;
- exact agreement between graph totals displayed in the UI and `graph.usage` for selected drill-down rows;
- reported, estimated, and unavailable cost states;
- token distribution using canonical processed-token values;
- cache-rate coverage rather than missing-as-zero behavior;
- active and wait time shown as distinct measures;
- shareable URLs that restore category, chart mode, and filters;
- responsive behavior at narrow and wide layouts;
- successful frontend production build and plugin-server static checks.

The metrics-validation quality gate defined in `docs/metrics-validation-quality-gate.md` validates the upstream core facts. Frontend dogfooding validates that the plugin preserves those facts through cohort aggregation and presentation.

## Rollout Plan

### Phase 1: Plugin shell and cohort API

- Scaffold `packages/plugins/metrics` and its manifest.
- Add the Vite/shadcn web package.
- Implement shared filters, URL state, cache boundaries, and cohort metadata.
- Integrate `project.sessions` inventory and batched `graph.usage` calls.

### Phase 2: Token Usage

- Implement usage, distribution, cache hit rate, and input-versus-output modes.
- Validate selected graph rows against `ct session graph usage`.

### Phase 3: Cost

- Implement per-session, distribution, and total modes.
- Preserve reported, estimated, and unavailable evidence states.

### Phase 4: Execution Time

- Implement active time, distribution, active-versus-wait, and turns modes.
- Enforce mixed-model runtime attribution rules.

### Phase 5: Dogfooding release

- Run the plugin against representative daily CodingTrajectory sessions.
- Capture semantic and layout issues as plugin defects rather than patching core facts in the UI.
- Complete accessibility, responsive, build, and static validation.

## Acceptance Criteria

- The plugin is independently discoverable as `ct plugin metrics`.
- Token Usage, Cost, and Execution Time are first-level routes.
- Benchmark and performance scoring are absent from Phase 1.
- Every page exposes its cohort and metric coverage.
- Missing telemetry is excluded rather than treated as zero.
- Token and cost values come from public core contracts.
- Mixed-model execution is not attributed to a dominant model.
- Chart modes and filters round-trip through the URL.
- The React client receives pre-aggregated chart data and bounded table rows.
- The UI uses the repository's shadcn/Tailwind semantic component approach.
- Drill-down rows reconcile with the corresponding `ct` service output.
- The production build and plugin static checks pass without adding unit tests.
