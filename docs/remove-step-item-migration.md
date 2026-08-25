# Remove Step and Adopt Turn Items

## Purpose

This document hands off a complete canonical-model migration:

```text
SessionGraph -> Session -> Turn -> Item
                              \-> Event references
```

Remove `Step` and every step-relative feature directly. Do not keep compatibility
aliases, legacy readers, or deprecated `step_*` fields.

The target follows the Codex app-server core shape: a thread/session contains
turns, and a turn contains items. An item is a unit such as an agent message,
command execution, file change, or tool call.

Official reference:

- https://developers.openai.com/codex/app-server#core-primitives

## Why This Change Is Necessary

The current model declares `Step` as one LLM call/response cycle, but the
projector does not have a reliable source boundary for that claim.

`TranscriptProjector` currently accumulates records in mutable
`current_step_*` state and flushes that state based on `flush_before` and
`flush_after` hints. Codex tool-call, tool-result, and usage records do not
consistently provide those hints. As a result, one step can contain:

- several model generations;
- several token usage observations;
- multiple sequential or parallel tool calls;
- tool results from different generation cycles.

This breaks token and cost attribution. For example:

- Session: `019e8252-dbde-7640-b487-83678cdcda41`
- Turn: `c146e383-3b8f-5190-a3cb-00d35e8f2722`
- Overview activity: `Edit files x4`

All four edit invocations were followed by nonzero Codex `token_count` events,
but the projector combined them with other commands in two broad steps. The
overview then grouped the edit calls across those steps. There was no honest
way to assign the step totals to the grouped edits, so the edit activity
appeared to have no token cost.

This is not merely a renderer problem. The canonical boundary is wrong.

## Design Rules

1. `Event` remains the immutable occurrence/evidence layer.
2. `Item` is the accumulated domain object reconstructed from related events.
3. `Turn` directly owns ordered items.
4. Usage observations remain independent source facts.
5. Presentation groups such as `Edit files x4` are derived views, never
   canonical hierarchy nodes.
6. Never attribute aggregate model usage to an individual item unless the
   source provides an exact relationship.
7. Do not recreate `Step` under another name such as `ModelStep`,
   `ActivityStep`, or a generic item group.

## Target Canonical Model

Replace `StepTextItem`, `StepToolItem`, `StepItem`, and `Step` with a Pydantic
discriminated union rooted at `Item`.

Every item should expose the common identity and evidence fields needed by
query, projection, and graph code:

```python
class ItemBase(BaseModel):
    item_id: UUID
    session_id: UUID
    turn_id: UUID
    sequence: int
    started_at: datetime
    completed_at: datetime | None = None
    status: str | None = None
    event_ids: list[UUID] = Field(default_factory=list)
    vendor_data: dict[str, Any] = Field(default_factory=dict)
```

Use concrete types appropriate to the current vendor evidence. At minimum,
support:

- `AgentMessageItem`
- `ToolCallItem`
- `CommandExecutionItem`
- `FileChangeItem`
- `ReasoningItem`
- `PlanItem`

Do not add speculative types without source signals. It is acceptable to begin
with a smaller exact union and extend it as adapters prove additional kinds.

`Turn` becomes:

```python
class Turn(BaseModel):
    ...
    items: list[Item] = Field(default_factory=list)
```

Tool request and result events should update the same item using
`tool_call_id`. Preserve event references so the original evidence remains
available through `event-detail`.

## Event and Item Boundary

An event is not an item.

Example:

```text
Event: tool.call.requested
Event: tool.call.succeeded
             |
             v
Item: CommandExecution(status=completed)
```

Events keep vendor timestamps and payload evidence. Items provide a stable,
normalized object for navigation and analysis.

## Usage and Cost Boundary

Remove step-level usage attribution.

Codex `token_count` records describe model usage snapshots. They do not report
the independent token cost of each tool invocation. Continue converting
cumulative snapshots into deltas, but scope the resulting observations to the
turn or to an explicit generation/usage span, not to whichever item is
currently open.

If a derived relation is useful, introduce an explicit structure such as:

```python
class UsageSpan(BaseModel):
    usage_observation_id: UUID
    turn_id: UUID
    related_item_ids: list[UUID] = Field(default_factory=list)
    attribution: Literal["exact", "shared", "unknown"]
```

This structure must not imply exclusive per-item billing. For grouped activity,
report one of:

- exact usage, when directly sourced;
- shared generation usage;
- unavailable/not attributable.

Never render unavailable item usage as numeric zero.

## Direct Removal Scope

### Canonical ingestion

Update:

- `packages/core/src/coding_trajectory/ingestion/models.py`
- `packages/core/src/coding_trajectory/ingestion/transcript.py`
- `packages/core/src/coding_trajectory/ingestion/step_items.py`
- all vendor adapters and vendor mechanisms that construct or inspect steps
- public exports in `coding_trajectory/__init__.py` and `ingestion/__init__.py`

Delete or replace:

- `Step`
- `StepTextItem`
- `StepToolItem`
- `StepItem`
- `Turn.steps`
- `current_step_*`
- `_start_step()`
- `_flush_step()`
- `flush_before`
- `flush_after`
- `attach_to_previous_step`

The projector should construct and update items directly.

Rename `step_items.py` to an item-oriented module if its append/update helpers
remain useful.

### Stable IDs

Update deterministic ID stabilization so items receive stable UUID5 IDs.
Remove step ID generation and step ID stabilization.

Validate that ingesting the same source twice produces identical item IDs.

### Query and indexes

Update:

- `packages/core/src/coding_trajectory/query.py`
- `packages/core/src/coding_trajectory/ingestion/indexes.py`
- `packages/core/src/coding_trajectory/discovery.py`

Replace:

- `steps` -> `items`
- `steps_by_id` -> `items_by_id`
- `session_by_step_id` -> `session_by_item_id`
- `get_step()` -> `get_item()`
- `events_for_step()` -> `events_for_item()`
- step-shaped document readers -> item-shaped readers

No old step-shaped JSON compatibility reader should remain.

### Session graph edges

Update:

- `packages/core/src/coding_trajectory/ingestion/graph.py`
- edge lookup helpers in `ingestion/indexes.py`
- multi-agent projections and teammate summaries

Replace:

- `SessionEdge.source_step_id` -> `source_item_id`
- `outgoing_edges_by_source_step` -> `outgoing_edges_by_source_item`
- `target_session_id_for_step()` -> item equivalent
- `_build_step_event_index()` -> item/event index

Spawn and handoff edges should point to the exact initiating tool item when
known. Keep `source_event_id` as the primary evidence reference.

### Analysis and overview

Update:

- `analysis/activity_flow.py`
- `analysis/session_graph_views.py`
- `analysis/request_lineage.py`
- `analysis/teammate_summary.py`
- `analysis/concepts.py`
- `analysis/projections.py`

Delete:

- `analysis/step_details.py`

Add:

- `analysis/item_details.py`

Activity flows should iterate directly over `turn.items`. Tool optimization and
human grouping remain derived. `Edit files x4` should be generated from four
file-change/tool items and should retain item IDs for drill-down.

Rename `StepType` to `ItemType`, `ActivityType`, or another accurate analysis
concept. Do not use "step" in the replacement name.

### Metrics

Update:

- `metrics/analysis.py`
- `metrics/models.py`
- `metrics/context_stats/_common.py`

Remove:

- `StepMetrics`
- `StepMetricsFlat`
- `ToolStepUsageFlat`
- `observed_step_cost`
- `observed_tool_step_cost`
- `tool_step_count`
- `model_steps`
- `tool_steps`
- `response_steps`
- `mixed_steps`
- `other_steps`
- functions that attach context usage observations to steps

Keep session and turn token totals. Replace activity accounting only with
categories that can be derived honestly from items and usage spans. If no exact
item attribution exists, keep the usage at turn scope.

Do not preserve the current `per_tool_cost = not_measured` schema merely to
retain the old command shape. Redesign the output around item evidence and
explicit attribution.

### Service and CLI

Update:

- `packages/core/src/coding_trajectory/service.py`
- `packages/cli/src/coding_trajectory_cli/commands/session.py`
- `packages/cli/src/coding_trajectory_cli/_shared.py`

Remove directly:

```text
ct session step-detail
step.details
step_id
step_ids
```

Add:

```text
ct session item-detail ITEM_ID [...]
item.details
item_id
item_ids
```

`session overview --output json` should expose item IDs for drill-down.
`event-detail` and `event-scan` remain the exact evidence surfaces.

Do not retain a `step-detail` alias.

### Plugins and UI

Update:

- `packages/plugins/review/review.py`
- datahub context-window projections and web UI
- any plugin consuming step categories or step IDs

Replace labels such as:

- `Step N`
- `model steps`
- `tool-step cost share`

Use item, turn, usage-span, or activity terminology according to the actual
data being shown.

Built-in plugins are executable consumers of documented CLI output. Update
their expected payloads rather than importing core migration helpers.

### Documentation

After implementation, update:

- `README.md`
- `docs/architecture.md`
- `docs/cli.md`
- `docs/plugin.md`
- `docs/prd.md`
- `docs/roadmap.md` if it mentions the old hierarchy

Historical generated reports may remain historical. Active specifications and
runtime help must not describe `Step` as canonical.

## Recommended Implementation Order

1. Define item models and replace `Turn.steps` with `Turn.items`.
2. Rewrite transcript projection and vendor adapter integration.
3. Update deterministic IDs, query storage, and indexes.
4. Migrate graph-edge origins from step IDs to item IDs.
5. Rewrite overview, activity, teammate, and lineage projections.
6. Replace `step-detail` with `item-detail`.
7. Redesign metrics around turn usage and explicit usage attribution.
8. Update plugins and datahub labels/payload consumers.
9. Update active documentation.
10. Run repository-wide stale-reference checks and real-session validation.

Keep each stage internally consistent. Avoid a long-lived state where some
paths use steps and others use items.

## Real-Session Validation

Use the source session that exposed the flaw:

```text
Session ID: 019e8252-dbde-7640-b487-83678cdcda41
Turn ID:    c146e383-3b8f-5190-a3cb-00d35e8f2722
```

Expected behavior after migration:

1. The turn exposes four independent edit/file-change items.
2. Each item has stable identity and event references.
3. Tool request and completion evidence resolve through `item-detail` and
   `event-detail`.
4. `Edit files x4` is only an overview grouping over those four items.
5. The grouped activity does not show a false zero token cost.
6. Turn usage still reconciles with the final Codex cumulative token snapshot.
7. No arbitrary item receives all usage merely because it was open when a
   `token_count` record arrived.

Also validate at least one:

- Claude Code session with tool use;
- Pi session with vendor-reported usage cost;
- multi-agent session with a spawn or handoff edge.

## Completion Checks

The implementation is complete only when active source has no old step
contract:

```bash
rg '\bStep\b|StepItem|step_id|step_ids|turn\.steps|step-detail|step\.details|source_step_id|events_for_step|observed_step_cost|observed_tool_step_cost' \
  packages/core/src packages/cli/src packages/plugins README.md docs
```

Review every remaining match. Historical report text may be intentionally
excluded, but active code and specifications should have none.

Verify current CLI help:

```bash
uv run ct --help
uv run ct session --help
uv run ct session item-detail --help
```

Verify ingestion and real output using the live CLI rather than only fixture
objects.

Repository instructions prohibit adding unit tests for this task. Use existing
checks, compile/lint commands, and real-session validation. After completing
the task, create a descriptive git commit. If continuing the same migration
across one session, amend the latest relevant migration commit instead of
creating unnecessary intermediate commits.

## Non-Goals

- Do not retain backward compatibility for step-shaped canonical JSON.
- Do not preserve old CLI aliases.
- Do not estimate exclusive per-tool cost.
- Do not make presentation groups canonical.
- Do not broaden this into unrelated discovery, pricing, or plugin architecture
  work.

## Definition of Done

The canonical hierarchy is `SessionGraph -> Session -> Turn -> Item`; events
remain evidence; usage remains source-accounted; graph edges reference items
and events; CLI drill-down uses item IDs; active code contains no step-relative
feature; and the real edit-files example no longer produces misleading
zero-cost presentation.
